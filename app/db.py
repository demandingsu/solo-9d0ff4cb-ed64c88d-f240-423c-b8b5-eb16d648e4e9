"""SQLite 持久化层。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS inspections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    total_pieces  INTEGER NOT NULL CHECK (total_pieces >= 0),
    note          TEXT,
    revision      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS count_reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    inspection_id  INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id      INTEGER NOT NULL REFERENCES count_reports(id) ON DELETE CASCADE,
    inspection_id  INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    lo             INTEGER NOT NULL CHECK (lo >= 0),
    hi             INTEGER NOT NULL CHECK (hi > lo),
    count          INTEGER NOT NULL CHECK (count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_reports_inspection
    ON count_reports(inspection_id);
CREATE INDEX IF NOT EXISTS idx_segments_inspection
    ON segments(inspection_id);
CREATE INDEX IF NOT EXISTS idx_segments_report
    ON segments(report_id);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开一个请求级连接。

    ``isolation_level=None`` 让我们显式使用 ``BEGIN IMMEDIATE`` 控制写事务，
    从而把“读取现有计数 + 校验 + 写入”放在同一个串行化事务里。

    ``check_same_thread=False``：每条连接只服务一个请求，但依赖清理与
    端点可能落在不同的线程池线程上；连接不会跨请求共享。
    """
    connection = sqlite3.connect(
        path, timeout=30.0, isolation_level=None, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
        _migrate(connection)
    finally:
        connection.close()


def _migrate(connection: sqlite3.Connection) -> None:
    """为既有数据库补齐后加的列（CREATE TABLE IF NOT EXISTS 不会改旧表）。

    ``revision`` 是界限计算所依赖数据的版本号：只随已提交的、影响上下界
    的写入（接收补报、更正总片数）递增；写入被拒回滚时不变。
    """
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(inspections)")
    }
    if "revision" not in columns:
        connection.execute(
            "ALTER TABLE inspections ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
        )


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_inspection(
    connection: sqlite3.Connection, total_pieces: int, note: str | None
) -> int:
    cursor = connection.execute(
        "INSERT INTO inspections (total_pieces, note, created_at) VALUES (?, ?, ?)",
        (total_pieces, note, now()),
    )
    return int(cursor.lastrowid)


def get_inspection(connection: sqlite3.Connection, inspection_id: int) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT i.id, i.total_pieces, i.note, i.created_at,
               (SELECT COUNT(*) FROM count_reports r WHERE r.inspection_id = i.id)
                   AS report_count,
               (SELECT COUNT(*) FROM segments s WHERE s.inspection_id = i.id)
                   AS segment_count
        FROM inspections i
        WHERE i.id = ?
        """,
        (inspection_id,),
    ).fetchone()


def list_inspections(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT i.id, i.total_pieces, i.note, i.created_at,
               (SELECT COUNT(*) FROM count_reports r WHERE r.inspection_id = i.id)
                   AS report_count,
               (SELECT COUNT(*) FROM segments s WHERE s.inspection_id = i.id)
                   AS segment_count
        FROM inspections i
        ORDER BY i.id
        """
    ).fetchall()


def update_inspection(
    connection: sqlite3.Connection,
    inspection_id: int,
    total_pieces: int,
    note: str | None,
    bump_revision: bool,
) -> None:
    """更正登记；``bump_revision`` 为真时同时递增数据版本号。

    总片数变化会影响界限计算结果，调用方据此决定是否递增版本；
    仅改备注不影响上下界，不必作废旧计算。
    """
    if bump_revision:
        connection.execute(
            "UPDATE inspections SET total_pieces = ?, note = ?, "
            "revision = revision + 1 WHERE id = ?",
            (total_pieces, note, inspection_id),
        )
    else:
        connection.execute(
            "UPDATE inspections SET total_pieces = ?, note = ? WHERE id = ?",
            (total_pieces, note, inspection_id),
        )


def get_data_state(
    connection: sqlite3.Connection, inspection_id: int
) -> sqlite3.Row | None:
    """读取界限查询所需的数据状态：总片数 + 数据版本号。

    供 /bounds 在同一个读事务里确定缓存键，保证响应中的总片数与上下界
    来自同一份已提交数据。
    """
    return connection.execute(
        "SELECT id, total_pieces, revision FROM inspections WHERE id = ?",
        (inspection_id,),
    ).fetchone()


def load_constraints(
    connection: sqlite3.Connection, inspection_id: int
) -> list[tuple[int, int, int]]:
    rows = connection.execute(
        "SELECT lo, hi, count FROM segments WHERE inspection_id = ? ORDER BY id",
        (inspection_id,),
    ).fetchall()
    return [(int(row["lo"]), int(row["hi"]), int(row["count"])) for row in rows]


def insert_report(
    connection: sqlite3.Connection,
    inspection_id: int,
    segments: Sequence[tuple[int, int, int]],
) -> int:
    cursor = connection.execute(
        "INSERT INTO count_reports (inspection_id, created_at) VALUES (?, ?)",
        (inspection_id, now()),
    )
    report_id = int(cursor.lastrowid)
    connection.executemany(
        """
        INSERT INTO segments (report_id, inspection_id, lo, hi, count)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(report_id, inspection_id, lo, hi, count) for lo, hi, count in segments],
    )
    # 接收补报会改变界限计算所依赖的数据，递增版本号（与写入同一事务，
    # 要么一起提交，要么一起回滚）。
    connection.execute(
        "UPDATE inspections SET revision = revision + 1 WHERE id = ?",
        (inspection_id,),
    )
    return report_id


def get_reports(connection: sqlite3.Connection, inspection_id: int) -> list[dict]:
    reports = connection.execute(
        "SELECT id, inspection_id, created_at FROM count_reports "
        "WHERE inspection_id = ? ORDER BY id",
        (inspection_id,),
    ).fetchall()
    segment_rows = connection.execute(
        "SELECT id, report_id, lo, hi, count FROM segments "
        "WHERE inspection_id = ? ORDER BY id",
        (inspection_id,),
    ).fetchall()

    segments_by_report: dict[int, list[dict]] = {int(r["id"]): [] for r in reports}
    for row in segment_rows:
        segments_by_report[int(row["report_id"])].append(
            {
                "id": int(row["id"]),
                "lo": int(row["lo"]),
                "hi": int(row["hi"]),
                "count": int(row["count"]),
            }
        )

    return [
        {
            "id": int(report["id"]),
            "inspection_id": int(report["inspection_id"]),
            "created_at": report["created_at"],
            "segments": segments_by_report[int(report["id"])],
        }
        for report in reports
    ]
