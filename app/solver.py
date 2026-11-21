"""不逐片展开的合格片数上下界计算。

模型
----
把所有补报区间的端点（以及查询用的合格区间端点）排序，把厚度轴切成
若干“原子区间”。同一个原子区间内的整数厚度，在所有计数约束看来完全
等价，因此只需用一个非负整数变量记录落在该原子内的片数。

设原子数为 m+1，前 k 个原子的片数前缀为 S_k，则：

* 任意补报区间 [lo, hi) 对应 S_j - S_i = c（i、j 为该区间在端点序列中
  的前缀编号）；
* 区间总数满足 S_{m+1} - S_0 = N；
* 原子片数非负等价于 S_{k+1} - S_k >= 0。

这些约束写成差分约束后，目标“合格区间片数”就是两个前缀变量之差。
最大差值等于两点间最短路，最小差值等于反向最短路取负。约束矩阵是连续
一矩阵，全幺模，因此最短路给出的整数解就是原整数问题的精确最优解。

算法不枚举片，也不展开原子内部的具体厚度分布；复杂度只与端点数量有关，
与总片数无关。
"""

from __future__ import annotations

from bisect import bisect_left
from collections import deque
from typing import Iterable, Sequence

INF = 10**30

Constraint = tuple[int, int, int]  # (lo, hi, count)，区间含 lo 不含 hi


class InconsistentCounts(Exception):
    """给定计数约束无解（互相矛盾，或与总片数矛盾）。"""


def _build_graph(
    total_count: int,
    constraints: Sequence[Constraint],
    extra_endpoints: Iterable[int] = (),
) -> tuple[dict[int, int], int, list[list[tuple[int, int]]]]:
    """构造差分约束图。

    节点 0..m+1 表示 m+1 个原子区间之间的前缀位置：
    S_0 在第一个端点之前，S_{m+1} 在最后一个端点之后。
    边 u -> v 权重 w 表示 S_v <= S_u + w。
    """
    endpoint_set = set(extra_endpoints)
    endpoint_set.update({0})
    for lo, hi, _ in constraints:
        endpoint_set.add(lo)
        endpoint_set.add(hi)

    endpoints = sorted(endpoint_set)
    if endpoints[0] != 0:
        raise ValueError("厚度端点必须非负")
    index = {value: i for i, value in enumerate(endpoints)}
    m = len(endpoints)
    node_count = m + 2
    adj: list[list[tuple[int, int]]] = [[] for _ in range(node_count)]

    def add_edge(u: int, v: int, w: int) -> None:
        adj[u].append((v, w))

    # 总片数：S_{m+1} - S_0 = total_count
    add_edge(0, m + 1, total_count)
    add_edge(m + 1, 0, -total_count)

    # 厚度非负：最靠左的原子区间 (-inf, 0) 必须为空，即 S_1 = S_0
    add_edge(0, 1, 0)
    add_edge(1, 0, 0)

    # 单调性：S_k <= S_{k+1}，等价于每个原子片数非负
    for k in range(m + 1):
        add_edge(k + 1, k, 0)
    # 每个补报区间：S_j - S_i = count
    for lo, hi, count in constraints:
        i = index[lo] + 1
        j = index[hi] + 1
        add_edge(i, j, count)
        add_edge(j, i, -count)

    return index, node_count, adj


def _bellman_ford(
    node_count: int, adj: Sequence[Sequence[tuple[int, int]]], source: int
) -> list[int]:
    """单源最短路（Bellman-Ford），负环时抛 :class:`InconsistentCounts`。

    作为 SPFA 的兜底与最终判定：图强连通，第 ``node_count`` 轮仍能松弛
    即存在负环。
    """
    dist = [INF] * node_count
    dist[source] = 0

    for _ in range(node_count):
        changed = False
        for u in range(node_count):
            du = dist[u]
            if du == INF:
                continue
            for v, weight in adj[u]:
                candidate = du + weight
                if candidate < dist[v]:
                    dist[v] = candidate
                    changed = True
        if not changed:
            return dist

    raise InconsistentCounts("计数约束互相矛盾")


def _shortest_paths(
    node_count: int, adj: Sequence[Sequence[tuple[int, int]]], source: int
) -> list[int]:
    """单源最短路（SPFA），负环时抛 :class:`InconsistentCounts`。

    SPFA 维护“当前最短路径的边数”，该值达到顶点数即高度疑似负环。
    由于路径边数在松弛过程中并非严格不变量，触发时再用 Bellman-Ford
    复核，避免误判；正常（相容）数据只走 SPFA，复杂度接近 O(E)。
    """
    dist = [INF] * node_count
    path_edges = [0] * node_count
    in_queue = [False] * node_count

    dist[source] = 0
    in_queue[source] = True
    queue: deque[int] = deque([source])

    while queue:
        u = queue.popleft()
        in_queue[u] = False
        du = dist[u]
        for v, weight in adj[u]:
            candidate = du + weight
            if candidate < dist[v]:
                dist[v] = candidate
                path_edges[v] = path_edges[u] + 1
                if path_edges[v] >= node_count:
                    # 复核：确为负环则抛 InconsistentCounts，否则取权威距离。
                    return _bellman_ford(node_count, adj, source)
                if not in_queue[v]:
                    in_queue[v] = True
                    queue.append(v)

    return dist


def check_consistent(total_count: int, constraints: Sequence[Constraint]) -> None:
    """检查整组计数是否存在可行分布，不计算合格片数上下界。

    无解时抛出 :class:`InconsistentCounts`。
    """
    _, node_count, adj = _build_graph(total_count, constraints)
    _shortest_paths(node_count, adj, 0)


def compute_bounds(
    total_count: int,
    constraints: Sequence[Constraint],
    qualified_lo: int,
    qualified_hi: int,
) -> tuple[int, int]:
    """返回同时满足全部计数时，合格区间 [qualified_lo, qualified_hi) 内
    片数可同时达到的最小值和最大值。

    约束无解时抛出 :class:`InconsistentCounts`。
    """
    if qualified_hi <= qualified_lo:
        raise ValueError("合格区间必须满足 qualified_hi > qualified_lo")

    index, node_count, adj = _build_graph(
        total_count, constraints, (qualified_lo, qualified_hi)
    )

    lo_node = index[qualified_lo] + 1
    hi_node = index[qualified_hi] + 1

    # 两趟最短路。图强连通，若存在负环，任一源点出发都会触发判定。
    from_lo = _shortest_paths(node_count, adj, lo_node)
    from_hi = _shortest_paths(node_count, adj, hi_node)

    # max(S_hi - S_lo) = dist(lo -> hi)
    # min(S_hi - S_lo) = -dist(hi -> lo)
    max_qualified = from_lo[hi_node]
    min_qualified = -from_hi[lo_node]
    if max_qualified >= INF or min_qualified <= -INF:
        # 图强连通且已排除负环，正常情况下不会发生。
        raise RuntimeError("最短路不可达，约束图构造异常")
    return int(min_qualified), int(max_qualified)


def _reverse_adjacency(
    node_count: int, adj: Sequence[Sequence[tuple[int, int]]]
) -> list[list[tuple[int, int]]]:
    """反向邻接表：在反向图上从 v 出发求最短路，即得原图上各点到 v 的最短路。"""
    rev: list[list[tuple[int, int]]] = [[] for _ in range(node_count)]
    for u in range(node_count):
        for v, weight in adj[u]:
            rev[v].append((u, weight))
    return rev


class BoundsIndex:
    """同一批已提交数据（总片数 + 全部计数）、同一合格下限的复用计算结构。

    构造时只做一次建图和两趟最短路（正向、反向各一趟）；之后对任意合格
    上限 ``qualified_hi`` 用二分查找定位原子区间即可给出精确上下界，
    不再回读补报、不再建图、不再跑最短路。上限是否出现在补报端点中
    不影响精确性。

    原理：合格下限固定，对应前缀变量 S_lo。对任意上限 x，记 f_max(x) =
    max(S_x - S_lo)、f_min(x) = min(S_x - S_lo)。同一原子区间 (a, b) 内
    的整数厚度在所有计数约束下等价，且原子内的片可以整体压到 x 之下或
    抬到 x 及其之上，因此：

    * x 落在原子区间 (a, b) 内时，``f_max(x) = max(S_b - S_lo)``，
      ``f_min(x) = min(S_a - S_lo)``（b 不存在时取“最后端点之后”的
      虚拟前缀，即全部片数）；
    * x 恰好是端点时直接取该端点的前缀差极值。

    端点处的前缀差极值由两趟最短路一次算清：正向最短路给出
    ``max(S_v - S_lo)``，反向图上的最短路给出 ``max(S_lo - S_v)``，
    取负即 ``min(S_v - S_lo)``。
    """

    __slots__ = ("total_count", "qualified_lo", "endpoints", "_max_from_lo", "_max_to_lo")

    def __init__(
        self,
        total_count: int,
        constraints: Sequence[Constraint],
        qualified_lo: int,
    ) -> None:
        if qualified_lo < 0:
            raise ValueError("合格区间起点必须非负")
        index, node_count, adj = _build_graph(
            total_count, constraints, (qualified_lo,)
        )
        lo_node = index[qualified_lo] + 1
        # 端点值 endpoints[j] 对应节点 j+1；节点 m+1 是“最后端点之后”的前缀。
        self.endpoints = sorted(index)
        # dist(lo -> v) = max(S_v - S_lo)
        self._max_from_lo = _shortest_paths(node_count, adj, lo_node)
        # dist(v -> lo) = max(S_lo - S_v)，在反向图上从 lo 出发求得
        rev = _reverse_adjacency(node_count, adj)
        self._max_to_lo = _shortest_paths(node_count, rev, lo_node)
        self.total_count = total_count
        self.qualified_lo = qualified_lo

    def query(self, qualified_hi: int) -> tuple[int, int]:
        """返回合格区间 [qualified_lo, qualified_hi) 片数的最小/最大可达到值。"""
        if qualified_hi <= self.qualified_lo:
            raise ValueError("合格区间必须满足 qualified_hi > qualified_lo")
        endpoints = self.endpoints
        pos = bisect_left(endpoints, qualified_hi)
        if pos < len(endpoints) and endpoints[pos] == qualified_hi:
            node = pos + 1
            max_count = self._max_from_lo[node]
            min_count = -self._max_to_lo[node]
        else:
            # qualified_hi 落在原子区间 (a, b) 内，或超过最后一个端点。
            # hi > qualified_lo >= 0 = endpoints[0]，故 pos >= 1。
            a_node = pos  # endpoints[pos-1] 的节点号
            b_node = pos + 1  # endpoints[pos] 的节点号，或最后的虚拟前缀节点
            max_count = self._max_from_lo[b_node]
            min_count = -self._max_to_lo[a_node]
        if max_count >= INF or min_count <= -INF:
            # 图强连通且建索引时已排除负环，正常情况下不会发生。
            raise RuntimeError("最短路不可达，约束图构造异常")
        return int(min_count), int(max_count)
