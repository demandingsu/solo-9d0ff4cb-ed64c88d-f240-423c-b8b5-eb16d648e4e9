"""BoundsIndex 复用结构：同一下限换上限，结果须与逐查询精确实现一致。"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.solver import BoundsIndex, InconsistentCounts, compute_bounds  # noqa: E402
from tests.brute import brute_bounds  # noqa: E402


def test_index_matches_compute_bounds_for_varied_upper_bounds():
    constraints = [(40, 60, 70), (50, 70, 60)]
    index = BoundsIndex(100, constraints, 50)
    # 覆盖：原子内部、补报端点、小于所有补报端点、大于所有端点、远超最后端点
    for hi in [51, 55, 60, 61, 70, 71, 100, 1000]:
        assert index.query(hi) == compute_bounds(100, constraints, 50, hi), hi


def test_index_bounds_not_in_reports():
    # 下限与上限都不出现在补报端点中，也要给出精确上下界
    constraints = [(40, 60, 70), (50, 70, 60)]
    index = BoundsIndex(100, constraints, 45)
    for hi in [46, 50, 55, 60, 65, 70, 80, 333]:
        assert index.query(hi) == compute_bounds(100, constraints, 45, hi), hi


def test_index_no_constraints():
    index = BoundsIndex(50, [], 10)
    assert index.query(20) == (0, 50)
    assert index.query(11) == (0, 50)
    assert index.query(10**9) == (0, 50)


def test_index_query_validates_order():
    index = BoundsIndex(10, [], 5)
    with pytest.raises(ValueError):
        index.query(5)
    with pytest.raises(ValueError):
        index.query(4)


def test_index_inconsistent_raises():
    with pytest.raises(InconsistentCounts):
        BoundsIndex(10, [(0, 10, 5), (0, 10, 6)], 0)


@pytest.mark.parametrize("seed", range(30))
def test_index_matches_compute_bounds_randomized(seed: int):
    rng = random.Random(1000 + seed)
    total = rng.randint(0, 30)
    constraints = []
    for _ in range(rng.randint(0, 5)):
        lo = rng.randint(0, 40)
        hi = lo + rng.randint(1, 30)
        constraints.append((lo, hi, rng.randint(0, total)))
    q_lo = rng.randint(0, 50)
    try:
        index = BoundsIndex(total, constraints, q_lo)
    except InconsistentCounts:
        with pytest.raises(InconsistentCounts):
            compute_bounds(total, constraints, q_lo, q_lo + 1)
        return
    # 同一份索引回答 20 个不同上限，逐个与独立建图的精确实现对比
    for _ in range(20):
        q_hi = q_lo + rng.randint(1, 60)
        assert index.query(q_hi) == compute_bounds(total, constraints, q_lo, q_hi)


@pytest.mark.parametrize("seed", range(20))
def test_index_matches_brute_force(seed: int):
    rng = random.Random(2000 + seed)
    total = rng.randint(0, 5)
    max_thickness = 6
    raw = []
    for _ in range(rng.randint(0, 4)):
        lo = rng.randint(0, max_thickness - 1)
        hi = rng.randint(lo + 1, max_thickness)
        raw.append((lo, hi, rng.randint(0, total)))
    # 与逐片枚举对齐有限厚度域
    bounded = raw + [(0, max_thickness, total)]
    q_lo = rng.randint(0, max_thickness - 1)
    if brute_bounds(total, raw, q_lo, q_lo + 1, max_thickness) is None:
        with pytest.raises(InconsistentCounts):
            BoundsIndex(total, bounded, q_lo)
        return
    index = BoundsIndex(total, bounded, q_lo)
    # 上限覆盖端点、非端点与超出所有端点（逐片枚举域之外无片，结果不变）
    for q_hi in range(q_lo + 1, max_thickness + 3):
        expected = brute_bounds(total, raw, q_lo, q_hi, max_thickness)
        assert index.query(q_hi) == expected, (q_lo, q_hi)
