"""换上限复用、数据版本失效、拒写复用、容量有界、响应头标示。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, solver  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client


def setup_batch(client):
    """总 100 片；[40,60) 70 片；[50,70) 60 片。"""
    assert client.post("/inspections", json={"total_pieces": 100}).status_code == 201
    assert (
        client.post(
            "/inspections/1/reports",
            json={"segments": [{"lo": 40, "hi": 60, "count": 70}]},
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/inspections/1/reports",
            json={"segments": [{"lo": 50, "hi": 70, "count": 60}]},
        ).status_code
        == 201
    )


def get_bounds(client, lo, hi):
    response = client.get(
        "/inspections/1/bounds", params={"qualified_lo": lo, "qualified_hi": hi}
    )
    assert response.status_code == 200, response.text
    return response


@pytest.fixture()
def counters(monkeypatch):
    """统计回读补报次数与最短路趟数。"""
    counts = {"loads": 0, "spfa": 0}
    real_load = db.load_constraints
    real_spfa = solver._shortest_paths

    def counting_load(connection, inspection_id):
        counts["loads"] += 1
        return real_load(connection, inspection_id)

    def counting_spfa(*args):
        counts["spfa"] += 1
        return real_spfa(*args)

    monkeypatch.setattr(db, "load_constraints", counting_load)
    monkeypatch.setattr(solver, "_shortest_paths", counting_spfa)
    return counts


def test_changing_upper_bound_reuses_computation(client, counters):
    setup_batch(client)
    counters["loads"] = counters["spfa"] = 0  # 不计写入路径的一致性检查

    first = get_bounds(client, 50, 60)
    assert first.headers["x-bounds-cache"] == "miss"
    assert first.json()["min_count"] == 30
    assert first.json()["max_count"] == 60
    # 首查：回读一次补报，建图一次，正/反向最短路各一趟
    assert counters == {"loads": 1, "spfa": 2}

    # 换上限（含非补报端点、超出所有端点）：全部命中，不再回读/建图/跑最短路
    expected = {55: (0, 60), 60: (30, 60), 65: (30, 60), 70: (60, 60),
                71: (60, 90), 1000: (60, 90)}
    for hi, (lo_exp, hi_exp) in expected.items():
        response = get_bounds(client, 50, hi)
        assert response.headers["x-bounds-cache"] == "hit"
        body = response.json()
        assert body["total_pieces"] == 100
        assert (body["min_count"], body["max_count"]) == (lo_exp, hi_exp), hi
    assert counters == {"loads": 1, "spfa": 2}


def test_bounds_exact_when_endpoints_not_reported(client):
    setup_batch(client)
    # 下限 45、上限 55 都不在补报端点中，仍须给出精确上下界
    body = get_bounds(client, 45, 55).json()
    assert (body["min_count"], body["max_count"]) == (0, 70)


def test_new_report_invalidates_cache(client, counters):
    setup_batch(client)
    counters["loads"] = counters["spfa"] = 0
    assert get_bounds(client, 50, 60).json()["min_count"] == 30
    assert counters["loads"] == 1

    # 接收新补报后，新查询必须用新数据
    assert (
        client.post(
            "/inspections/1/reports",
            json={"segments": [{"lo": 50, "hi": 60, "count": 45}]},
        ).status_code
        == 201
    )
    before = dict(counters)
    response = get_bounds(client, 50, 60)
    assert response.headers["x-bounds-cache"] == "miss"
    body = response.json()
    assert (body["min_count"], body["max_count"]) == (45, 45)
    # 本次查询因数据版本变化而未命中，重新回读了一次补报
    assert counters["loads"] == before["loads"] + 1
    # 再次换上限又复用新索引
    assert get_bounds(client, 50, 55).headers["x-bounds-cache"] == "hit"


def test_patch_total_invalidates_cache(client, counters):
    setup_batch(client)
    counters["loads"] = counters["spfa"] = 0
    assert get_bounds(client, 50, 60).headers["x-bounds-cache"] == "miss"
    assert counters["loads"] == 1

    assert client.patch("/inspections/1", json={"total_pieces": 110}).status_code == 200
    before = dict(counters)
    response = get_bounds(client, 50, 60)
    assert response.headers["x-bounds-cache"] == "miss"
    body = response.json()
    assert body["total_pieces"] == 110  # 响应里的总片数来自新数据
    assert (body["min_count"], body["max_count"]) == (20, 60)  # 70+60-110=20
    # 本次查询因数据版本变化而未命中，重新回读了一次补报
    assert counters["loads"] == before["loads"] + 1


def test_rejected_writes_keep_cache_usable(client, counters):
    setup_batch(client)
    counters["loads"] = counters["spfa"] = 0
    assert get_bounds(client, 50, 60).headers["x-bounds-cache"] == "miss"
    assert counters == {"loads": 1, "spfa": 2}

    # 冲突补报被拒（409）
    conflict = client.post(
        "/inspections/1/reports",
        json={"segments": [{"lo": 40, "hi": 60, "count": 71}]},
    )
    assert conflict.status_code == 409
    # 冲突的总片数更正被拒（409）
    assert client.patch("/inspections/1", json={"total_pieces": 50}).status_code == 409

    # 旧计算仍可复用：命中、结果不变；查询自身不再回读/建图/跑最短路
    # （被拒写入自身的一致性检查不计入）
    before = dict(counters)
    response = get_bounds(client, 50, 65)
    assert response.headers["x-bounds-cache"] == "hit"
    body = response.json()
    assert body["total_pieces"] == 100
    assert (body["min_count"], body["max_count"]) == (30, 60)
    assert counters == before


def test_note_only_patch_keeps_cache(client, counters):
    setup_batch(client)
    counters["loads"] = counters["spfa"] = 0
    assert get_bounds(client, 50, 60).headers["x-bounds-cache"] == "miss"
    assert counters["loads"] == 1

    # 仅改备注不影响上下界，旧索引继续可用
    assert client.patch("/inspections/1", json={"note": "已复核"}).status_code == 200
    assert get_bounds(client, 50, 70).headers["x-bounds-cache"] == "hit"
    assert counters["loads"] == 1


def test_memory_does_not_grow_with_upper_bound_count(tmp_path):
    app = create_app(str(tmp_path / "many_hi.db"))
    with TestClient(app) as client:
        setup_batch(client)
        for hi in range(51, 551):  # 500 个不同上限，同一下限
            get_bounds(client, 50, hi)
        assert len(app.state.bounds_cache) == 1


def test_cache_capacity_bounds_memory(tmp_path):
    app = create_app(str(tmp_path / "many_lo.db"), cache_capacity=4)
    with TestClient(app) as client:
        setup_batch(client)
        for lo in range(10):  # 10 个不同下限，超出容量
            get_bounds(client, lo, lo + 1)
        assert len(app.state.bounds_cache) <= 4
        # 缓存被截断后结果仍然正确（未命中即重建）
        body = get_bounds(client, 50, 60).json()
        assert (body["min_count"], body["max_count"]) == (30, 60)
