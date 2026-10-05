# 直播提示点播控服务（broadcast-cue-api）

纯后端服务：登记直播彩排的**提示点模板**，在某些提示点被要求推迟后，
推演同时满足全部下界的**逐点最早可执行时刻**，并把成功结果持久化到挂载卷中的
SQLite。推演对规则的输入顺序不敏感——同一批规则无论怎样排列，只会得到
**同一份**最早提示表；不可实现时给出明确的稳定错误代码，绝不伪装成排期。
可选地，推演还能给出**因果解释**：某个提示点为什么是这个时刻——从哪条
释放时间 / delay 与先后约束链被推迟至此，证据链唯一且可核对。

- 框架：FastAPI（Python 3.11）
- 存储：SQLite（WAL），默认数据库文件 `/data/app.db`（位于 Docker 挂载卷）
- 编排：Docker Compose，HTTP 入口仅绑定本机 `http://127.0.0.1:8000`
- 交互文档：服务启动后访问 `http://127.0.0.1:8000/docs`

### 并发与持久化语义

多个编辑台同时登记/立即生成时，短暂写争用在有界时间内得到确定结果：

- **WAL 只在启动初始化时切换一次**，由进程内锁 + 文件锁串行化并有界重试；
  工作连接不再各自切换 journal mode（该 PRAGMA 不接受 busy 等待，是启动期
  `database is locked` 被放大成 500 的根源）。
- 写入统一用 `BEGIN IMMEDIATE`：建事务即排队申请写锁，锁释放后等待中的请求
  直接成功；每个成功响应**恰好对应一条完整记录**，不会因重试产生难区分的
  重复模板或结果。等待上限可用环境变量 `BROADCAST_DB_BUSY_TIMEOUT`
  （秒，默认 5）调整。
- 超过有界等待仍拿不到锁时，返回稳定、可识别、可重试的
  **503 `service_unavailable`**（带 `Retry-After: 1`），事务回滚、不留
  半成品，响应中绝不出现 SQLite 内部错误文本。
- `created_at` 由应用生成一次后**显式写入并原样返回**：同一记录在创建响应、
  列表/详情读取中逐字一致（UTC、毫秒精度、`...Z`），按时间排序与审计稳定。

## 目录结构

```
app/
  main.py        # FastAPI 入口：/templates、/derivations、/results/{id}
  scheduler.py   # 最早时刻推演（最长路 / Bellman-Ford 式松弛，不枚举候选时间）+ 因果解释（取紧边回溯，(关系数, 路径字节序) 唯一裁决）
  validation.py  # 模板与 delay 覆盖的严格校验 + 规范化
  db.py          # SQLite 持久层（只写入合法模板与成功结果；WAL 单次初始化、BEGIN IMMEDIATE 有界等待）
  errors.py      # 稳定错误码与统一错误响应（含 503 service_unavailable）
tests/           # pytest：分支汇合/乱序/延误传播/零间隔环/正权环 + 因果解释（等价链/零环/截止违约/重算核对/重启读取）+ 接口与持久化 + 真实 SQLite 并发验收
Dockerfile
docker-compose.yml
```

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build -d
curl http://127.0.0.1:8000/health      # {"status":"ok"}
docker compose logs -f api
docker compose down
```

SQLite 数据库、WAL、SHM 文件都保存在宿主目录 `./data/`（容器内 `/data`），
容器重启或重建后模板与成功结果仍然可取回。

### 本地直接运行（无 Docker 时）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
# 无 /data 写权限时自动回退到 ./local-data/app.db；也可用 BROADCAST_DB_PATH 指定
```

### 运行测试

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q
```

## 数据模型与约束（普通 JSON）

### 模板

- `points`：1～300 个提示点。
  - `id`：非空字符串，模板内**唯一**。
  - `release`：整数，0 ≤ release ≤ 10⁹。
  - `latest`：可选；给出时必须是整数且 **latest ≥ release**。
- `relations`：0～3000 条先后关系。
  - `from`、`to`：必须引用模板中存在的点 ID（允许自环）。
  - `min_gap`：整数，0 ≤ min_gap ≤ 10⁶，含义是
    **to 的时刻 ≥ from 的时刻 + min_gap**。

### delay 覆盖

推演时可以为**部分**提示点提供 `delay`，值必须是非负整数且
**不小于该点原 release**；语义是把该点的基础下界从 release 替换为 delay。

## 接口约定

所有请求/响应均为 JSON。失败响应统一为：

```json
{ "error": { "code": "<稳定代码>", "message": "<说明>", "details": { } } }
```

| 方法 & 路径 | 说明 | 成功状态 |
|---|---|---|
| `GET /health` | 健康检查 | 200 |
| `POST /templates` | 校验并登记合法模板（返回模板 ID 与规范化内容） | 201 |
| `POST /derivations` | 对已登记模板执行延误推演，成功才落库；`"explain": true` 时附因果解释 | 201 |
| `GET /results/{result_id}` | 按结果 ID 取回成功结果（重启后仍可查）；`?explain=true` 时附同一份解释 | 200 |

### 稳定错误代码

| code | HTTP | 触发条件 |
|---|---|---|
| `invalid_template` | 400 | 模板违反任一登记规则；**非法模板不落库** |
| `bad_json` | 400 | 请求体不是合法 JSON 或不是 JSON 对象 |
| `invalid_delay` | 400 | delay 引用不存在的点、为负、或小于原 release；`explain` 字段不是布尔 |
| `invalid_request` | 400 | 查询参数不合法（如 `?explain=` 取值不是 `true`/`false`） |
| `template_not_found` | 404 | 推演引用了不存在的模板 ID |
| `result_not_found` | 404 | 查询的结果 ID 不存在（含失败推演——它们从不生成记录） |
| `positive_cycle` | 422 | 规则存在总 min_gap 为正的有向环，不存在有限最早时刻 |
| `deadline_exceeded` | 422 | 无正权环，但至少一点最早时刻 > latest |
| `result_inconsistent` | 500 | 已保存结果与冻结模板 + delay 重算不一致；记录保持原样、绝不改写 |
| `service_unavailable` | 503 | 并发写争用在有界等待（默认 5s）后仍未获得写锁；可重试，带 `Retry-After: 1`，不暴露 SQLite 内部错误 |
| `not_found` / `method_not_allowed` | 404 / 405 | 未知路径 / 方法不允许 |

判定优先级：**先 positive_cycle，再 deadline_exceeded**。
`positive_cycle` / `deadline_exceeded` 都是推演失败：不产生结果记录，
也不改动任何既有数据。

成功结果中的 `times` 与 `points` 一律按提示点 ID **升序**排列。

## 因果解释（可选）：某点为什么是这个时刻

彩排推演出某个提示点晚于预期时，导播需要看见究竟是哪条释放时间与先后
约束链把它推迟，而不是只得到一个时刻数字。推演请求带 `"explain": true`
时，成功响应额外包含 `explanations`（按点 ID 升序，覆盖全部提示点）；
对已保存结果用 `GET /results/{result_id}?explain=true` 可取回**同一份**
解释。每个点对应一条证据：

```json
{
  "target": "d",
  "time": 50,
  "start": {"id": "a", "kind": "delay", "value": 30},
  "edges": [{"from": "a", "to": "c", "min_gap": 20},
            {"from": "c", "to": "d", "min_gap": 0}],
  "path": ["a", "c", "d"]
}
```

契约要点：

- 每条证据从某点的**原始 release**（`"kind": "release"`）或**本次
  delay**（`"kind": "delay"`）出发，沿**实际取紧**的关系
  （`times[to] == times[from] + min_gap`）到达目标点；
- 不变式：`start.value + Σ edges[].min_gap == time == times[target]`——
  逐边时差相加恰好等于返回的最早时刻；
- 等价链裁决：**关系数最少**者优先，再按**完整点 ID 序列的 UTF-8
  字节序**（逐元素比较）取唯一结果——解释确定，与规则录入顺序无关；
- 零间隔环不会使追溯循环：含环的链关系数更多，天然落选，证据路径中
  点不重复；
- 解释**不落库**：读取时由冻结模板 + delay 重算，并与保存的结果逐点
  核对；发现不一致时返回 500 `result_inconsistent`，记录保持原样、
  绝不改写；
- `deadline_exceeded` 且请求解释时，`details.explanation` 给出
  **ID 最小的违约点**及其证据（失败推演仍不落库）；`positive_cycle`
  永远优先报原错误，不附解释；
- 未请求解释时，所有响应与旧版逐项一致（无 `explanations` 字段）。

## 调用示例

### 1. 登记模板

```bash
curl -s http://127.0.0.1:8000/templates \
  -H 'Content-Type: application/json' \
  -d '{
    "points": [
      {"id": "a", "release": 0, "latest": 100},
      {"id": "b", "release": 0, "latest": 100},
      {"id": "c", "release": 0, "latest": 100},
      {"id": "d", "release": 1, "latest": 100}
    ],
    "relations": [
      {"from": "a", "to": "b", "min_gap": 5},
      {"from": "a", "to": "c", "min_gap": 20},
      {"from": "b", "to": "d", "min_gap": 3},
      {"from": "c", "to": "d", "min_gap": 0}
    ]
  }'
```

响应（`points`/`relations` 已规范化排序，省略）：

```json
{
  "template_id": "9f0c2b7e...",
  "created_at": "2026-09-22T10:00:00.000Z",
  "point_count": 4,
  "relation_count": 4,
  "points": [ "...按 id 升序..." ],
  "relations": [ "...按 (from,to,min_gap) 升序..." ]
}
```

### 2. 推演：把提示点 a 推迟到 30

```bash
TID=<上一步返回的 template_id>
curl -s http://127.0.0.1:8000/derivations \
  -H 'Content-Type: application/json' \
  -d "{\"template_id\":\"$TID\",\"delay\":{\"a\":30}}"
```

```json
{
  "result_id": "1b2d4f...",
  "template_id": "9f0c2b7e...",
  "created_at": "2026-09-22T10:01:00.000Z",
  "delay": {"a": 30},
  "times": {"a": 30, "b": 35, "c": 50, "d": 50},
  "points": [
    {"id": "a", "release": 0, "latest": 100, "time": 30},
    {"id": "b", "release": 0, "latest": 100, "time": 35},
    {"id": "c", "release": 0, "latest": 100, "time": 50},
    {"id": "d", "release": 1, "latest": 100, "time": 50}
  ]
}
```

无 delay 的推演：`{"template_id":"<id>"}` 或 `{"template_id":"<id>","delay":{}}`。

### 3. 取回结果（容器重启后仍有效）

```bash
curl -s http://127.0.0.1:8000/results/<result_id>
```

### 4. 不可实现：正权环

```bash
curl -s -i http://127.0.0.1:8000/derivations \
  -H 'Content-Type: application/json' \
  -d '{"template_id":"<带 a->b(1), b->a(1) 的模板 ID>"}'
# HTTP/1.1 422
# {"error":{"code":"positive_cycle",
#   "message":"The rules contain a directed cycle with positive total
#   min_gap; no finite earliest schedule exists."}}
```

### 5. 不可实现：超过 latest

```json
HTTP 422
{
  "error": {
    "code": "deadline_exceeded",
    "message": "At least one point's earliest time exceeds its latest bound.",
    "details": {"violations": [{"id": "b", "earliest": 8, "latest": 5}]}
  }
}
```

### 6. 非法模板（不落库）

```json
HTTP 400
{ "error": { "code": "invalid_template",
             "message": "relations[0].to references unknown point id 'ghost'.",
             "details": {"path": "relations[0].to", "id": "ghost"} } }
```

## 算法说明（为什么结果唯一且不需要枚举候选时间）

每条约束都是一个**下界**：

- `t[p] ≥ release[p]`（有 delay 时为 `t[p] ≥ delay[p]`）；
- `t[v] ≥ t[u] + min_gap(u,v)`。

逐点最早时刻等价于一张含"虚拟源点"的图上的**最长路**：源点向每个点连权为
基础下界的边。求法是 Bellman-Ford 式的逐轮**松弛**——每轮扫描全部边，
`t[v] = max(t[v], t[u]+gap)`，直到一轮内无变化。松弛只做最大值传播，
**不枚举任何候选时间**：

- 复杂度 O(n·m)（n ≤ 300，m ≤ 3000）；
- 最终不动点由约束集合唯一确定，与点和边的扫描顺序无关（顺序只影响收敛轮数），
  所以分支重新汇合时更晚的前驱不会漏算，同一批规则永远得到同一张表；
- n 轮完整松弛后若仍有边可松弛，说明存在总权为正的有向环
  （时刻可沿环无限增长）⇒ `positive_cycle`；
- 否则对每个点核对 `latest`，超限即 `deadline_exceeded`；
- 总权为 0 的环（含 0 权自环）合法：它只会把环上各点拉平到同一最早时刻。
