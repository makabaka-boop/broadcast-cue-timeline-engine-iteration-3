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

可选的"同刻绑定"（两个**不同**提示点 x、y 必须同一时刻播发）就是在图上
**同时**加入两条零间隔边 ``x -> y`` 与 ``y -> x``：``t[x] >= t[y]`` 与
``t[y] >= t[x]``。它与普通关系一样参与松弛，因此延误既会沿绑定**回传**
（较晚的点把较早的点抬起），又会从被抬起的点沿既有关系**再次传播**——
绝不能先算无绑定的旧表、再把两个时刻改成同一个数字，那会漏掉回传之后的
二次传播。若原关系在两点之间任一方向上存在**正间隔**路径，该路径与反向
零边组成正权环，仍按既有优先级报 ``positive_cycle``。

因果解释（可选）
----------------

无正权环时，每个点的最早时刻都可由一条**证据链**精确复现：从某点的基础
下界（原始 release 或本次 delay）出发，沿**实际取紧**的关系或同刻绑定边
（``times[to] == times[from] + min_gap``；绑定边 ``min_gap == 0`` 且带
``binding: true`` 标记，是可辨认的零间隔边）走到目标点，逐边 min_gap
相加恰好等于目标点的最早时刻。证据链唯一，与规则录入顺序无关：

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


def normalize_bind(bind):
    """把同刻绑定规范化为按 ID 升序的二元组；无绑定时为 ``None``。

    无论输入两点的次序（``["y","x"]`` 还是 ``["x","y"]``），规范化结果都
    相同，因此冻结与重算核对不受录入次序影响。调用前应已通过校验层确认
    这是两个存在且互不相同的点 ID。
    """
    if not bind:
        return None
    first, second = bind
    return (first, second) if first <= second else (second, first)


def _build_edges(relations, bind):
    """构造松弛边表。

    普通关系为 ``(u, v, gap, False)``；同刻绑定展开成两条
    ``(u, v, 0, True)`` 零间隔边（正反各一条）。末尾的布尔位表示该边
    是否为绑定边，供因果解释渲染可辨认的零间隔边。
    """
    edges = [(r["from"], r["to"], r["min_gap"], False) for r in relations]
    if bind:
        first, second = bind
        edges.append((first, second, 0, True))
        edges.append((second, first, 0, True))
    return edges


def _compute_outcome(points, relations, delay, bind=None):
    """核心松弛：返回 ``(status, times, violations)``。

    ``times`` 在 ``positive_cycle`` 时为 ``None``（时刻无有限值）；
    ``violations`` 仅在 ``deadline_exceeded`` 时非 ``None``。
    """
    delay = delay or {}
    bind = normalize_bind(bind)
    ids = [p["id"] for p in points]
    n = len(ids)

    # 基础下界：虚拟源点 -> 点 p，权为 release（或 delay 覆盖）。
    times = {}
    for p in points:
        pid = p["id"]
        times[pid] = int(delay.get(pid, p["release"]))

    latest_by_id = {p["id"]: p.get("latest") for p in points}

    # 边以 (u, v, gap, is_binding) 表示 t[v] >= t[u] + gap；同刻绑定是
    # 两条相向的零间隔边，与普通关系一起松弛，回传与二次传播自动发生。
    edges = _build_edges(relations, bind)

    # 至多 n 轮松弛即可得到无正权环图上的最长路。
    for _ in range(n):
        changed = False
        for u, v, gap, _is_binding in edges:
            candidate = times[u] + gap
            if candidate > times[v]:
                times[v] = candidate
                changed = True
        if not changed:
            break

    # 第 n+1 轮检测：仍能松弛 => 存在总权为正的有向环。
    for u, v, gap, _is_binding in edges:
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


def earliest_schedule(points, relations, delay=None, bind=None):
    """求逐点最早可执行时刻。

    :param points: 已校验的提示点列表，元素为
        ``{"id": str, "release": int, "latest": Optional[int]}``。
    :param relations: 已校验的关系列表，元素为
        ``{"from": str, "to": str, "min_gap": int}``。
    :param delay: 可选的 ``{点 ID: 延误覆盖值}``，覆盖值不小于原 release。
    :param bind: 可选的同刻绑定——两个**不同**且存在的点 ID（传入顺序
        任意，内部规范化为 ID 升序），要求两点最早时刻相等。
    :returns:
        成功::

            {"status": "ok", "times": {id: int, ...}}  # 含全部提示点

        存在总间隔为正的有向环（含原关系在绑定两点间形成正间隔路径、
        与绑定零边合环）::

            {"status": "positive_cycle"}

        无正权环但有点最早时刻超过 latest::

            {"status": "deadline_exceeded",
             "violations": [{"id": str, "earliest": int, "latest": int}, ...]}
    """
    status, times, violations = _compute_outcome(points, relations, delay, bind)
    if status == STATUS_POSITIVE_CYCLE:
        return {"status": STATUS_POSITIVE_CYCLE}
    if status == STATUS_DEADLINE_EXCEEDED:
        return {"status": STATUS_DEADLINE_EXCEEDED, "violations": violations}
    return {"status": STATUS_OK, "times": times}


def _best_evidence(points, relations, delay, times, bind=None):
    """为每个点构造唯一最佳证据链（契约见模块 docstring）。

    ``times`` 必须是无正权环时 :func:`_compute_outcome` 给出的不动点。
    返回按点 ID 升序的 ``{点 ID: evidence}``；若某点没有任何证据链
    （无正权环时数学上不会发生，仅作防御）返回 ``None``。
    """
    delay = delay or {}
    bind = normalize_bind(bind)

    # 每个点的基础下界及其来源（release 或本次 delay）。
    base = {}
    for p in points:
        pid = p["id"]
        if pid in delay:
            base[pid] = ("delay", int(delay[pid]))
        else:
            base[pid] = ("release", p["release"])

    # 实际取紧的边：times[u] + gap == times[v]。同刻绑定的两条零边同样
    # 取紧，但渲染时带 binding 标记，是证据里可辨认的零间隔边。
    tight = {}

    def _add_tight(u, v, gap, is_binding):
        if times[u] + gap != times[v]:
            return
        entries = tight.setdefault(u, [])
        for index, (existing_v, _gap, existing_binding) in enumerate(entries):
            if existing_v != v:
                continue
            # 同一 (u,v) 上并列的取紧零边只留一条，让裁决与录入无关：
            # 绑定边优先于普通关系；同为普通关系时保留先出现的一条。
            if is_binding and not existing_binding:
                entries[index] = (v, gap, True)
            return
        entries.append((v, gap, is_binding))

    for r in relations:
        _add_tight(r["from"], r["to"], r["min_gap"], False)
    if bind:
        first, second = bind
        _add_tight(first, second, 0, True)
        _add_tight(second, first, 0, True)

    # Dijkstra：键为 (关系数, 完整点 ID 序列的 UTF-8 字节序)。键沿边严格
    # 增大（关系数 +1），每个点只结算一次；零权环上的取紧边因此不会
    # 让追溯循环——含环的链关系数更多，必然落选。
    # best[pid] = (关系数, 路径字节序, 路径点 ID 元组, 边四元组元组)
    # 绑定的零间隔边同样算作一条边参与计数——键沿任意边都严格 +1，因此
    # 绑定与零权关系组成的零环同样关系数更多而落选，追溯不会循环。
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
        for nxt, gap, is_binding in tight.get(pid, ()):
            if nxt in settled:
                continue
            candidate = (count + 1,
                         path_bytes + (nxt.encode("utf-8"),),
                         path_ids + (nxt,),
                         chain_edges + ((pid, nxt, gap, is_binding),))
            existing = best.get(nxt)
            if existing is None or candidate[:2] < existing[:2]:
                best[nxt] = candidate
                heapq.heappush(heap, (candidate[0], candidate[1], nxt))

    if any(p["id"] not in settled for p in points):
        return None  # 防御：无正权环时每个点都应有证据链

    explanations = {}
    for pid in sorted(best):
        _, _, path_ids, edge_quads = best[pid]
        kind, value = base[path_ids[0]]
        rendered_edges = []
        for u, v, edge_gap, is_binding in edge_quads:
            edge = {"from": u, "to": v, "min_gap": edge_gap}
            if is_binding:
                # 可辨认的同刻零间隔边。
                edge["binding"] = True
            rendered_edges.append(edge)
        explanations[pid] = {
            "target": pid,
            "time": times[pid],
            "start": {"id": path_ids[0], "kind": kind, "value": value},
            "edges": rendered_edges,
            "path": list(path_ids),
        }
    return explanations


def explain_schedule(points, relations, delay=None, bind=None):
    """推演并给出因果解释。

    :param bind: 可选的同刻绑定（两个不同点 ID，次序任意）。
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
    status, times, violations = _compute_outcome(points, relations, delay, bind)
    if status == STATUS_POSITIVE_CYCLE:
        return {"status": STATUS_POSITIVE_CYCLE}

    explanations = _best_evidence(points, relations, delay, times, bind)
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


def verify_result_explanations(points, relations, delay, stored_times,
                               bind=None):
    """用冻结模板、delay 与同刻绑定重算，核对已保存结果并给出解释。

    * 重算成功、逐点时刻与保存值完全一致、且每条证据逐边相加等于保存的
      最早时刻:: ``{"status": "ok", "explanations": {...}}``
    * 任何不一致（重算失败、时刻不符、证据链对不上）::
      ``{"status": "inconsistent"}``

    本函数只读不改写：不一致时由调用方报错，记录保持原样。
    """
    delay = delay or {}
    bind = normalize_bind(bind)
    try:
        status, times, _ = _compute_outcome(points, relations, delay, bind)
    except (KeyError, TypeError, ValueError):
        # 冻结输入本身已损坏（如模板被外部改写），同样视为不一致。
        return {"status": STATUS_INCONSISTENT}
    if status != STATUS_OK or times != stored_times:
        return {"status": STATUS_INCONSISTENT}

    explanations = _best_evidence(points, relations, delay, times, bind)
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
