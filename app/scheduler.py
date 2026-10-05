"""最早可执行时刻推演（差分约束的最长路模型）。

每个提示点 ``p`` 满足：

* ``t[p] >= release[p]``（若提供 delay 覆盖，则 ``t[p] >= delay[p]``）；
* 对每条关系 ``(u -> v, min_gap=g)``，``t[v] >= t[u] + g``。

全部约束均为下界，因此逐点最早时刻等价于以"虚拟源点"（向每个点连权为
基础下界的边）为起点的最长路。最长路可用 Bellman-Ford 式的逐轮松弛求解，
无需枚举任何候选时间：

* 轮次与边的排列顺序无关，只影响收敛快慢，因此同一批规则无论怎样排列，
  得到的时间表完全相同；
* 若经过 ``n`` 轮完整松弛后仍可松弛，则存在源点可达的总权为正的有向环
  （正权环 => 时刻可沿环无限增长），返回 ``positive_cycle``；
* 否则结果即同时满足全部下界的最早时刻，再逐点检查 ``latest`` 上界。

因果解释（可选）
----------------

无正权环时，每个点的最早时刻都可由一条**证据链**精确复现：从某点的基础
下界（原始 release 或本次 delay）出发，沿**实际取紧**的关系
（``times[to] == times[from] + min_gap``）走到目标点，逐边 min_gap 相加
恰好等于目标点的最早时刻。证据链唯一，与规则录入顺序无关：

* 等价链先取**关系数最少**者；
* 再按**完整点 ID 序列的 UTF-8 字节序**取最小者（逐元素比较）。

求法是把每个"基础下界即最终时刻"的点作为起点（0 条关系的链），在取紧边
组成的图上跑以 ``(关系数, 路径字节序)`` 为键的 Dijkstra：键沿边严格增大，
每个点只结算一次，因此零权环上的取紧边**不会**让追溯循环（含环的链
关系数更多，天然落选）。正权环没有有限时刻，不存在证据链，仍只报
``positive_cycle``。

解释本身不落库：读取已保存结果时由冻结模板 + delay 重算，并与保存的
时刻逐点核对（见 :func:`verify_result_explanations`）。
"""

from __future__ import annotations

import heapq

# 推演结果状态码（与 HTTP 响应中的稳定错误码保持一致）
STATUS_OK = "ok"
STATUS_POSITIVE_CYCLE = "positive_cycle"
STATUS_DEADLINE_EXCEEDED = "deadline_exceeded"

# 重算核对的结论码
STATUS_INCONSISTENT = "inconsistent"


def _compute_outcome(points, relations, delay):
    """核心松弛：返回 ``(status, times, violations)``。

    ``times`` 在 ``positive_cycle`` 时为 ``None``（时刻无有限值）；
    ``violations`` 仅在 ``deadline_exceeded`` 时非 ``None``。
    """
    delay = delay or {}
    ids = [p["id"] for p in points]
    n = len(ids)

    # 基础下界：虚拟源点 -> 点 p，权为 release（或 delay 覆盖）。
    times = {}
    for p in points:
        pid = p["id"]
        times[pid] = int(delay.get(pid, p["release"]))

    latest_by_id = {p["id"]: p.get("latest") for p in points}

    # 边以 (u, v, gap) 表示 t[v] >= t[u] + gap。
    edges = [(r["from"], r["to"], r["min_gap"]) for r in relations]

    # 至多 n 轮松弛即可得到无正权环图上的最长路。
    for _ in range(n):
        changed = False
        for u, v, gap in edges:
            candidate = times[u] + gap
            if candidate > times[v]:
                times[v] = candidate
                changed = True
        if not changed:
            break

    # 第 n+1 轮检测：仍能松弛 => 存在总权为正的有向环。
    for u, v, gap in edges:
        if times[u] + gap > times[v]:
            return STATUS_POSITIVE_CYCLE, None, None

    # 无正权环：逐点核对 latest 上界（按输入点序，输出确定）。
    violations = []
    for pid in ids:
        latest = latest_by_id[pid]
        if latest is not None and times[pid] > latest:
            violations.append(
                {"id": pid, "earliest": times[pid], "latest": latest}
            )
    if violations:
        return STATUS_DEADLINE_EXCEEDED, times, violations

    return STATUS_OK, times, None


def earliest_schedule(points, relations, delay=None):
    """求逐点最早可执行时刻。

    :param points: 已校验的提示点列表，元素为
        ``{"id": str, "release": int, "latest": Optional[int]}``。
    :param relations: 已校验的关系列表，元素为
        ``{"from": str, "to": str, "min_gap": int}``。
    :param delay: 可选的 ``{点 ID: 延误覆盖值}``，覆盖值不小于原 release。
    :returns:
        成功::

            {"status": "ok", "times": {id: int, ...}}  # 含全部提示点

        存在总间隔为正的有向环::

            {"status": "positive_cycle"}

        无正权环但有点最早时刻超过 latest::

            {"status": "deadline_exceeded",
             "violations": [{"id": str, "earliest": int, "latest": int}, ...]}
    """
    status, times, violations = _compute_outcome(points, relations, delay)
    if status == STATUS_POSITIVE_CYCLE:
        return {"status": STATUS_POSITIVE_CYCLE}
    if status == STATUS_DEADLINE_EXCEEDED:
        return {"status": STATUS_DEADLINE_EXCEEDED, "violations": violations}
    return {"status": STATUS_OK, "times": times}


def _best_evidence(points, relations, delay, times):
    """为每个点构造唯一最佳证据链（契约见模块 docstring）。

    ``times`` 必须是无正权环时 :func:`_compute_outcome` 给出的不动点。
    返回按点 ID 升序的 ``{点 ID: evidence}``；若某点没有任何证据链
    （无正权环时数学上不会发生，仅作防御）返回 ``None``。
    """
    delay = delay or {}

    # 每个点的基础下界及其来源（release 或本次 delay）。
    base = {}
    for p in points:
        pid = p["id"]
        if pid in delay:
            base[pid] = ("delay", int(delay[pid]))
        else:
            base[pid] = ("release", p["release"])

    # 实际取紧的边：times[u] + gap == times[v]。
    tight = {}
    for r in relations:
        u, v, gap = r["from"], r["to"], r["min_gap"]
        if times[u] + gap == times[v]:
            tight.setdefault(u, []).append((v, gap))

    # Dijkstra：键为 (关系数, 完整点 ID 序列的 UTF-8 字节序)。键沿边严格
    # 增大（关系数 +1），每个点只结算一次；零权环上的取紧边因此不会
    # 让追溯循环——含环的链关系数更多，必然落选。
    # best[pid] = (关系数, 路径字节序, 路径点 ID 元组, 边三元组元组)
    best = {}
    heap = []
    for p in points:
        pid = p["id"]
        if times[pid] == base[pid][1]:
            # 基础下界即最终时刻的点：0 条关系的链起点。
            best[pid] = (0, (pid.encode("utf-8"),), (pid,), ())
            heapq.heappush(heap, (0, best[pid][1], pid))

    settled = set()
    while heap:
        count, path_bytes, pid = heapq.heappop(heap)
        if pid in settled:
            continue
        current = best[pid]
        if current[0] != count or current[1] != path_bytes:
            continue  # 已被更优候选替换的陈旧堆项
        settled.add(pid)
        path_ids = current[2]
        chain_edges = current[3]
        for nxt, gap in tight.get(pid, ()):
            if nxt in settled:
                continue
            candidate = (count + 1,
                         path_bytes + (nxt.encode("utf-8"),),
                         path_ids + (nxt,),
                         chain_edges + ((pid, nxt, gap),))
            existing = best.get(nxt)
            if existing is None or candidate[:2] < existing[:2]:
                best[nxt] = candidate
                heapq.heappush(heap, (candidate[0], candidate[1], nxt))

    if any(p["id"] not in settled for p in points):
        return None  # 防御：无正权环时每个点都应有证据链

    explanations = {}
    for pid in sorted(best):
        _, _, path_ids, edge_triples = best[pid]
        kind, value = base[path_ids[0]]
        explanations[pid] = {
            "target": pid,
            "time": times[pid],
            "start": {"id": path_ids[0], "kind": kind, "value": value},
            "edges": [{"from": u, "to": v, "min_gap": gap}
                      for u, v, gap in edge_triples],
            "path": list(path_ids),
        }
    return explanations


def explain_schedule(points, relations, delay=None):
    """推演并给出因果解释。

    :returns:
        成功::

            {"status": "ok", "times": {...},
             "explanations": {点 ID: evidence, ...}}  # 按点 ID 升序

        截止违约（附 ID 最小违约点的证据；violations 顺序不变）::

            {"status": "deadline_exceeded", "violations": [...],
             "explanation": {"id": str, "evidence": evidence}}

        正权环（没有有限时刻，不存在证据链，只报原状态）::

            {"status": "positive_cycle"}
    """
    delay = delay or {}
    status, times, violations = _compute_outcome(points, relations, delay)
    if status == STATUS_POSITIVE_CYCLE:
        return {"status": STATUS_POSITIVE_CYCLE}

    explanations = _best_evidence(points, relations, delay, times)
    assert explanations is not None  # 无正权环时每个点都有证据链

    if status == STATUS_DEADLINE_EXCEEDED:
        first = min(violations, key=lambda v: v["id"])
        return {
            "status": STATUS_DEADLINE_EXCEEDED,
            "violations": violations,
            "explanation": {"id": first["id"],
                            "evidence": explanations[first["id"]]},
        }
    return {"status": STATUS_OK, "times": times,
            "explanations": explanations}


def verify_result_explanations(points, relations, delay, stored_times):
    """用冻结模板与 delay 重算，核对已保存结果并给出解释。

    * 重算成功、逐点时刻与保存值完全一致、且每条证据逐边相加等于保存的
      最早时刻:: ``{"status": "ok", "explanations": {...}}``
    * 任何不一致（重算失败、时刻不符、证据链对不上）::
      ``{"status": "inconsistent"}``

    本函数只读不改写：不一致时由调用方报错，记录保持原样。
    """
    delay = delay or {}
    try:
        status, times, _ = _compute_outcome(points, relations, delay)
    except (KeyError, TypeError, ValueError):
        # 冻结输入本身已损坏（如模板被外部改写），同样视为不一致。
        return {"status": STATUS_INCONSISTENT}
    if status != STATUS_OK or times != stored_times:
        return {"status": STATUS_INCONSISTENT}

    explanations = _best_evidence(points, relations, delay, times)
    if explanations is None:
        return {"status": STATUS_INCONSISTENT}

    # 防御性核对：每条证据从起点出发逐边相加，必须恰好等于保存的最早时刻。
    for pid, evidence in explanations.items():
        total = evidence["start"]["value"]
        for edge in evidence["edges"]:
            total += edge["min_gap"]
        if (total != stored_times.get(pid)
                or evidence["time"] != stored_times.get(pid)):
            return {"status": STATUS_INCONSISTENT}
    return {"status": STATUS_OK, "explanations": explanations}
