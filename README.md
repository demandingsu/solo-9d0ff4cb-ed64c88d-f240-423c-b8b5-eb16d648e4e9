# 纸样厚度计数核对 API

登记一批纸样的总片数，分次提交互可重叠的厚度区间计数（整数微米，区间
`[lo, hi)`，含起点不含终点）。给定合格区间，返回**同时满足全部已接收计数**
时合格片数可达到的最小值和最大值。计算按端点做整数规划，不逐片展开。

补报与已有计数不相容时**整份拒收**（HTTP 409），已有结果保持不变、仍可查询。

质检时常用同一个厚度下限、只换上限反复核算。`/bounds` 为此做了复用：
同一批已提交数据、同一下限的首查会建立一份索引（建图一次、正/反向
最短路各一趟），之后只换上限的查询直接命中索引，不再回读历史补报、
不再建图、不再重跑最短路；响应头 `X-Bounds-Cache: hit|miss` 标示是否
命中。上下限即使不出现在补报端点中，索引同样给出精确上下界。

## 快速开始

```bash
docker compose up --build
```

打开 <http://localhost:8000/docs> 录入与查询。SQLite 数据保存在命名卷
`inspection-data`（容器内 `/data/inspection.db`）中，重启容器不丢失。

本地不用 Docker 时：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/inspections` | 登记总片数（`total_pieces`，可选 `note`） |
| `GET` | `/inspections` | 列出全部登记 |
| `GET` | `/inspections/{id}` | 查看登记及已接收的分次补报 |
| `PATCH` | `/inspections/{id}` | 更正总片数/备注；新总片数与已有计数冲突则拒收 |
| `POST` | `/inspections/{id}/reports` | 提交一批区间计数 `{"segments":[{"lo","hi","count"}]}` |
| `GET` | `/inspections/{id}/reports` | 查看补报历史 |
| `GET` | `/inspections/{id}/bounds?qualified_lo=&qualified_hi=` | 查询合格片数上下界；同下限换上限复用索引，响应头 `X-Bounds-Cache` 标示命中 |
| `GET` | `/health` | 健康检查 |

### 示例

```bash
curl -X POST localhost:8000/inspections \
  -H 'content-type: application/json' -d '{"total_pieces":100}'

curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":40,"hi":60,"count":70}]}'

curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":50,"hi":70,"count":60}]}'

curl 'localhost:8000/inspections/1/bounds?qualified_lo=50&qualified_hi=60'
# {"inspection_id":1,"total_pieces":100,"qualified_lo":50,"qualified_hi":60,
#  "min_count":30,"max_count":60}

# 冲突补报：整份 409 拒收，上面的结果不受影响
curl -X POST localhost:8000/inspections/1/reports \
  -H 'content-type: application/json' -d '{"segments":[{"lo":40,"hi":60,"count":71}]}'
# {"detail":"补报与已有计数（或本报告内部）不相容，整份拒收；已有结果保持不变"}
```

## 计算方法

把所有区间端点（含查询端点与厚度零点）排序，将厚度轴切成原子区间。落在同一
原子区间内的整数厚度在所有计数约束下等价，因此每个原子只需一个非负整数变量
（片数）。设前缀和为 `S_k`：

* 区间 `[lo, hi)` 计数为 `c` ⇔ `S_j - S_i = c`；
* 总片数 `N` ⇔ `S_last - S_0 = N`；
* 原子片数非负 ⇔ `S_k <= S_{k+1}`。

目标“合格区间片数”为 `S_hi - S_lo`。其最大值等于差分约束图上 `lo → hi` 的
最短路，最小值等于 `hi → lo` 最短路取负。约束矩阵为连续一矩阵（全幺模），
最短路给出的整数解即原整数规划的精确最优解。负环 ⇔ 计数互相矛盾。

复杂度只取决于端点数量与图规模，与总片数无关；实现使用 SPFA 求最短路，
负环触发时用 Bellman-Ford 复核，避免误判。

## 换上限复用与一致性

* **索引复用**：下限固定时，`max(合格片数)` 与 `min(合格片数)` 作为上限的
  函数都是端点间的分段常值函数（原子区间内的片可整体压到上限之下或抬到
  上限及其之上）。因此首查建索引时算一次“下限到各端点”的正/反向最短路，
  之后任意上限只需二分定位原子区间即可给出精确值，无需上限出现在补报中。
* **数据版本**：`inspections.revision` 只随影响上下界的已提交写入（接收
  补报、更正总片数）递增，且与写在同一事务提交。缓存键为
  `(登记 id, 数据版本, 下限)`：写入提交后新查询自然未命中、用新数据重建；
  写入被拒回滚时版本不变，旧索引继续命中；仅改备注不作废索引。
* **内存有界**：上限不在缓存键里，换上限不增加条目；缓存为 LRU，容量
  上限由 `BOUNDS_CACHE_CAPACITY` 控制（默认 32），超出按最近最少使用淘汰，
  内存不随查过的上限/下限数量持续增长。
* **读写交错**：`/bounds` 在单个读事务里读取版本号、总片数与（未命中时
  的）全部计数，响应中的总片数与上下界始终来自同一份已提交快照。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

* `tests/test_solver.py`：重叠区间、相邻不重叠区间、零计数、冲突检测、
  大数量不展开，以及 40 组与逐片枚举对拍。
* `tests/test_index.py`：复用索引与逐查询精确实现、逐片枚举对拍；上限
  覆盖补报端点、非端点与超出所有端点。
* `tests/test_api.py`：登记、补报、跨批次重叠、跨批次冲突整份拒收、
  单份报告内部冲突拒收、拒收后原结果可查、`PATCH` 冲突拒收、参数校验、`/docs`。
* `tests/test_bounds_cache.py`：换上限命中复用（不再回读/建图/跑最短路）、
  补报与总片数更正后失效重建、被拒写入后旧计算仍可复用、缓存容量有界。
* `tests/test_concurrency.py`：并发冲突补报只有一个成功；并发相容补报全部接收；
  读写交错时每个响应的总片数与上下界来自同一份已提交数据。
