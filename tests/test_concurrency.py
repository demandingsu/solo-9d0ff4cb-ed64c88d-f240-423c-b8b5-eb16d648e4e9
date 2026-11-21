"""并发补报测试：串行化事务保证不丢失不相容检查。"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402


def test_concurrent_conflicting_reports_only_one_wins(tmp_path):
    app = create_app(str(tmp_path / "concurrent.db"))
    with TestClient(app) as client:
        assert client.post("/inspections", json={"total_pieces": 10}).status_code == 201

        def submit(count: int):
            return client.post(
                "/inspections/1/reports",
                json={"segments": [{"lo": 0, "hi": 10, "count": count}]},
            ).status_code

        # 5 与 6 互相矛盾，只能有一个成功。
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(pool.map(submit, [5, 6]))

        assert statuses == [201, 409]
        detail = client.get("/inspections/1").json()
        assert detail["report_count"] == 1
        assert detail["segment_count"] == 1


def test_concurrent_compatible_reports_all_accepted(tmp_path):
    app = create_app(str(tmp_path / "concurrent2.db"))
    with TestClient(app) as client:
        assert client.post("/inspections", json={"total_pieces": 10}).status_code == 201

        def submit(pair):
            lo, hi = pair
            return client.post(
                "/inspections/1/reports",
                json={"segments": [{"lo": lo, "hi": hi, "count": 2}]},
            ).status_code

        pairs = [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10)]
        with ThreadPoolExecutor(max_workers=5) as pool:
            statuses = list(pool.map(submit, pairs))

        assert statuses == [201] * 5
        detail = client.get("/inspections/1").json()
        assert detail["report_count"] == 5
        bounds = client.get(
            "/inspections/1/bounds", params={"qualified_lo": 0, "qualified_hi": 10}
        ).json()
        assert bounds["min_count"] == 10
        assert bounds["max_count"] == 10


def test_interleaved_reads_and_writes_see_consistent_snapshots(tmp_path):
    """查询与写入交错：每个响应的总片数与上下界须来自同一份已提交数据。

    总片数在 100 与 60 之间来回更正；[0,50) 恒为 60 片。对查询 [50,100)：
    数据版本 A（total=100）应答 (0, 40)，数据版本 B（total=60）应答 (0, 0)。
    任何混装（如 total=100 却答 max=0）都说明响应拼合了不同快照。
    """
    app = create_app(str(tmp_path / "interleave.db"))
    with TestClient(app) as client:
        assert client.post("/inspections", json={"total_pieces": 100}).status_code == 201
        assert (
            client.post(
                "/inspections/1/reports",
                json={"segments": [{"lo": 0, "hi": 50, "count": 60}]},
            ).status_code
            == 201
        )

        stop = threading.Event()
        failures = []

        def writer():
            totals = [60, 100]
            i = 0
            while not stop.is_set():
                resp = client.patch(
                    "/inspections/1", json={"total_pieces": totals[i % 2]}
                )
                if resp.status_code != 200:
                    failures.append(("patch", resp.status_code, resp.text))
                    return
                i += 1

        def reader():
            for _ in range(200):
                resp = client.get(
                    "/inspections/1/bounds",
                    params={"qualified_lo": 50, "qualified_hi": 100},
                )
                if resp.status_code != 200:
                    failures.append(("get", resp.status_code, resp.text))
                    return
                body = resp.json()
                snapshot = (
                    body["total_pieces"],
                    body["min_count"],
                    body["max_count"],
                )
                if snapshot not in ((100, 0, 40), (60, 0, 0)):
                    failures.append(("mixed", body))
                    return

        with ThreadPoolExecutor(max_workers=3) as pool:
            writer_future = pool.submit(writer)
            readers = [pool.submit(reader) for _ in range(2)]
            for future in readers:
                future.result()
            stop.set()
            writer_future.result()

        assert failures == []
