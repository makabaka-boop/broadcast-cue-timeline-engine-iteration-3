"""同刻绑定（bind）：延误双向传播、零间隔环、正间隔路径冲突、截止违约、
输入换序，以及重启后解释一致性。

同刻绑定的语义：在松弛之前加入 ``t[a] == t[b]``（一对双向 0 权边），
延误因此能沿绑定回传并再次传播；不能先算旧表再把两点改成同一个数字。

* 绑定与输入两点的次序无关——内部规范化为按 ID 升序的无序点对；
* 原先后关系在两点间存在总间隔为正的路径时，沿既有优先级报
  ``positive_cycle``；否则继续判定 ``latest``；失败不生成结果记录；
* 成功结果冻结规范化绑定，按 ID 读取时用模板、delay 与绑定重算核对；
* 因果解释把同刻约束序列化为可辨认的零间隔边
  ``{"from", "to", "min_gap": 0, "bound": true}``，零间隔环不使追溯循环；
* 未传绑定的旧结果与响应逐项保持兼容（无 ``bind`` 字段、无 ``bound`` 边）。
"""

from __future__ import annotations

import random
import sqlite3

from fastapi.testclient import TestClient

from app.main import create_app
from app.scheduler import (STATUS_DEADLINE_EXCEEDED, STATUS_OK,
                           STATUS_POSITIVE_CYCLE, earliest_schedule,
                           explain_schedule, verify_result_explanations)
from tests.conftest import make_template_payload


def pt(pid, release, latest=None):
    return {"id": pid, "release": release, "latest": latest}


def rel(u, v, gap):
    return {"from": u, "to": v, "min_gap": gap}


def bound_edge(u, v):
    return {"from": u, "to": v, "min_gap": 0, "bound": True}


def chain_sum(evidence):
    return evidence["start"]["value"] + sum(
        e["min_gap"] for e in evidence["edges"])


# ---- 双向传播：不能先算旧表再抹平 ------------------------------------------

def test_bind_propagates_delay_backward_through_relations():
    # b 自身 release=10，且 b -> c(10)；把 a delay 到 40 后绑定 a 与 b：
    #   旧表是 a=40, b=10, c=20；事后抹平会错成 a=b=40, c=20。
    #   正确做法是把绑定当双向 0 边先加入再松弛：a=b=40，c 随之 50。
    points = [pt("a", 0), pt("b", 10), pt("c", 0)]
    relations = [rel("b", "c", 10)]
    out = earliest_schedule(points, relations, delay={"a": 40},
                            bind=["a", "b"])
    assert out["status"] == STATUS_OK
    assert out["times"] == {"a": 40, "b": 40, "c": 50}


def test_bind_lifts_both_sides_from_either_floor():
    # 没有 delay：绑定把 release 更高的一侧（b=12）回传给 a，
    # 再经 a -> d(8) 下推；绑定造成的抬升必须沿关系再次传播。
    points = [pt("a", 0), pt("b", 12), pt("d", 0)]
    relations = [rel("a", "d", 8)]
    out = earliest_schedule(points, relations, bind=["a", "b"])
    assert out["status"] == STATUS_OK
    assert out["times"] == {"a": 12, "b": 12, "d": 20}


def test_bind_does_not_lower_any_existing_floor():
    # a=5, b=3，绑定后只能取 max(5,3)=5；任何一点都不得被拉低。
    points = [pt("a", 5), pt("b", 3)]
    out = earliest_schedule(points, [], bind=["a", "b"])
    assert out["status"] == STATUS_OK
    assert out["times"] == {"a": 5, "b": 5}


def test_bind_propagates_delay_backward_through_zero_gap_relation():
    # 延误加在被绑定的点上，沿"绑定 + 0 权回传关系"一路回传：
    #   a <-> b <-> c 均为 0 权（同刻提示段），绑定 c 与 d，delay d=70。
    #   70 先沿绑定 c==d 回传给 c，再沿 0 权先后关系回传 b、a；
    #   先算旧表（a=b=c=0, d=70）再抹平 c,d 会错误地漏掉 a、b 的抬升。
    points = [pt("a", 0), pt("b", 0), pt("c", 0), pt("d", 0)]
    relations = [rel("a", "b", 0), rel("b", "a", 0),
                 rel("b", "c", 0), rel("c", "b", 0)]
    out = earliest_schedule(points, relations, delay={"d": 70},
                            bind=["c", "d"])
    assert out["status"] == STATUS_OK
    assert out["times"] == {"a": 70, "b": 70, "c": 70, "d": 70}
    # 解释同样沿绑定边与 0 权关系回传：a 的证据起于 delay 的 d。
    explained = explain_schedule(points, relations, delay={"d": 70},
                                 bind=["d", "c"])
    ev_a = explained["explanations"]["a"]
    assert ev_a["start"] == {"id": "d", "kind": "delay", "value": 70}
    assert ev_a["path"] == ["d", "c", "b", "a"]
    assert ev_a["edges"] == [bound_edge("d", "c"),
                             {"from": "c", "to": "b", "min_gap": 0},
                             {"from": "b", "to": "a", "min_gap": 0}]
    assert chain_sum(ev_a) == 70
    # 零间隔环不使追溯循环：所有证据路径点不重复。
    for evidence in explained["explanations"].values():
        assert len(evidence["path"]) == len(set(evidence["path"]))


# ---- 零间隔环：绑定 + 零权关系合法，追溯不得循环 -----------------------------

def test_bind_zero_gap_cycle_is_feasible():
    # a->b(0)->c(0) 再加 c==a 的绑定：三者构成 0 权环，
    # 环上各点拉平到 max(release)=3。
    points = [pt("a", 1), pt("b", 2), pt("c", 3)]
    relations = [rel("a", "b", 0), rel("b", "c", 0)]
    out = earliest_schedule(points, relations, bind=["c", "a"])
    assert out["status"] == STATUS_OK
    assert out["times"] == {"a": 3, "b": 3, "c": 3}


def test_bind_zero_gap_cycle_explanation_has_no_repeated_points():
    points = [pt("a", 1), pt("b", 2), pt("c", 3)]
    relations = [rel("a", "b", 0), rel("b", "c", 0)]
    out = explain_schedule(points, relations, bind=["a", "c"])
    assert out["status"] == STATUS_OK
    # 唯一 0 链起点是 c（release=3）；证据穿绑定边一次即达，不含环。
    assert out["explanations"]["c"]["path"] == ["c"]
    assert out["explanations"]["a"]["path"] == ["c", "a"]
    assert out["explanations"]["a"]["edges"] == [bound_edge("c", "a")]
    assert out["explanations"]["b"]["path"] == ["c", "a", "b"]
    assert out["explanations"]["b"]["edges"] == [
        bound_edge("c", "a"),
        {"from": "a", "to": "b", "min_gap": 0}]
    for evidence in out["explanations"].values():
        assert len(evidence["path"]) == len(set(evidence["path"]))
        assert chain_sum(evidence) == evidence["time"] == 3


def test_bind_bidirectional_zero_edges_are_tight_but_do_not_loop():
    # a.release=2、b.release=0 且直接绑定：唯一 0 关系链起点是 a，
    # b 必须穿过一条绑定边到达；a<->b 两条绑定边同时取紧，
    # 但追溯不会在它们之间打转（含环的链关系数更多，天然落选）。
    points = [pt("a", 2), pt("b", 0)]
    out = explain_schedule(points, [], bind=["a", "b"])
    assert out["status"] == STATUS_OK
    assert out["times"] == {"a": 2, "b": 2}
    assert out["explanations"]["a"]["path"] == ["a"]
    assert out["explanations"]["b"]["path"] == ["a", "b"]
    assert out["explanations"]["b"]["edges"] == [bound_edge("a", "b")]


# ---- 正间隔路径：与既有先后关系冲突时按优先级报 positive_cycle --------------

def test_bind_positive_gap_path_either_direction_is_positive_cycle():
    points = [pt("a", 0), pt("b", 0)]
    # a -> b 总间隔 5：要求 t[b] >= t[a] + 5 与 t[a] == t[b] 矛盾。
    out = earliest_schedule(points, [rel("a", "b", 5)], bind=["a", "b"])
    assert out["status"] == STATUS_POSITIVE_CYCLE
    assert "times" not in out
    # 路径方向反过来（b -> a 为正）同样是正权环。
    out = earliest_schedule(points, [rel("b", "a", 5)], bind=["a", "b"])
    assert out["status"] == STATUS_POSITIVE_CYCLE


def test_bind_positive_path_through_intermediate_detected():
    # 正间隔路径不必是一条边：x -> m(4) -> y(4)，绑定 x 与 y 即冲突。
    points = [pt("x", 0), pt("m", 0), pt("y", 0)]
    relations = [rel("x", "m", 4), rel("m", "y", 4)]
    assert (earliest_schedule(points, relations, bind=["x", "y"])["status"]
            == STATUS_POSITIVE_CYCLE)
    # 间隔总和恰为 0（0+0）时不冲突。
    zero_relations = [rel("x", "m", 0), rel("m", "y", 0)]
    ok = earliest_schedule(points, zero_relations, bind=["x", "y"])
    assert ok["status"] == STATUS_OK


def test_bind_positive_cycle_takes_priority_over_deadline():
    # 正间隔路径与 latest 违约同时存在：永远先报 positive_cycle。
    points = [pt("a", 0, latest=0), pt("b", 0, latest=0)]
    relations = [rel("a", "b", 1)]
    out = earliest_schedule(points, relations, bind=["a", "b"])
    assert out["status"] == STATUS_POSITIVE_CYCLE
    assert explain_schedule(points, relations, bind=["a", "b"]) == {
        "status": STATUS_POSITIVE_CYCLE}


def test_bind_does_not_mask_preexisting_positive_cycle():
    # 绑定本身不碰环上点，但原模板的正权环仍要按原优先级报出。
    points = [pt("a", 0), pt("b", 0), pt("c", 0), pt("d", 0)]
    relations = [rel("a", "b", 1), rel("b", "a", 1)]
    out = earliest_schedule(points, relations, bind=["c", "d"])
    assert out["status"] == STATUS_POSITIVE_CYCLE


# ---- 截止违约：无冲突绑定抬升越过 latest 时报 deadline_exceeded -------------

def test_bind_deadline_exceeded_after_lift():
    # a delay 到 9 并绑定 b（latest=5）：b 被抬到 9，违约由绑定传播造成。
    points = [pt("a", 0), pt("b", 0, latest=5)]
    out = earliest_schedule(points, [], delay={"a": 9}, bind=["a", "b"])
    assert out["status"] == STATUS_DEADLINE_EXCEEDED
    assert out["violations"] == [
        {"id": "b", "earliest": 9, "latest": 5}]


def test_bind_deadline_explanation_identifies_zero_gap_edge():
    points = [pt("a", 0), pt("b", 0, latest=5)]
    out = explain_schedule(points, [], delay={"a": 9}, bind=["a", "b"])
    assert out["status"] == STATUS_DEADLINE_EXCEEDED
    assert out["explanation"]["id"] == "b"
    evidence = out["explanation"]["evidence"]
    assert evidence["path"] == ["a", "b"]
    assert evidence["start"] == {"id": "a", "kind": "delay", "value": 9}
    assert evidence["edges"] == [bound_edge("a", "b")]
    assert chain_sum(evidence) == evidence["time"] == 9


# ---- 输入换序：规范化绑定 + 规则乱序得到同一份结果 ---------------------------

def test_bind_order_independent_swap_pair_and_shuffle():
    # 绑定 a 与 b，b->c(10)->d 是环外下游，绑定造成的抬升沿链下传：
    # delay a=40 => a=b=40, c=50, d=50。独立点 z 不受影响。
    points = [pt("a", 0), pt("b", 10), pt("c", 0), pt("d", 0), pt("z", 7)]
    relations = [rel("b", "c", 10), rel("c", "d", 0)]
    rng = random.Random(20261005)

    reference = earliest_schedule(points, relations, delay={"a": 40},
                                  bind=["a", "b"])
    assert reference["status"] == STATUS_OK
    assert reference["times"] == {
        "a": 40, "b": 40, "c": 50, "d": 50, "z": 7}
    for trial in range(20):
        shuffled_points = points[:]
        shuffled_relations = relations[:]
        rng.shuffle(shuffled_points)
        rng.shuffle(shuffled_relations)
        # 一半试验把绑定两点次序对调。
        bind = ["b", "a"] if trial % 2 == 0 else ["a", "b"]
        out = earliest_schedule(shuffled_points, shuffled_relations,
                                delay={"a": 40}, bind=bind)
        assert out["status"] == STATUS_OK, trial
        assert out["times"] == reference["times"], trial

    explained = explain_schedule(points, relations, delay={"a": 40},
                                 bind=["b", "a"])
    swapped = explain_schedule(points, relations, delay={"a": 40},
                               bind=["a", "b"])
    assert explained["explanations"] == swapped["explanations"]


# ---- 重算核对：绑定随结果冻结 ------------------------------------------------

def test_verify_with_bind_consistent_and_detects_missing_bind():
    points = [pt("a", 0), pt("b", 10), pt("c", 0)]
    relations = [rel("b", "c", 10)]
    times = earliest_schedule(points, relations, delay={"a": 40},
                              bind=["b", "a"])["times"]
    # 用规范化点对（次序无关）重算核对通过。
    check = verify_result_explanations(
        points, relations, {"a": 40}, times, ["a", "b"])
    assert check["status"] == STATUS_OK
    assert check["explanations"]["c"]["edges"] == [
        bound_edge("a", "b"),
        {"from": "b", "to": "c", "min_gap": 10}]
    # 丢掉绑定重算必然不一致：旧表给不出被绑定回传抬起的时刻。
    assert verify_result_explanations(
        points, relations, {"a": 40}, times)["status"] == "inconsistent"
    # 错绑到别的点同样不一致。
    assert verify_result_explanations(
        points, relations, {"a": 40}, times, ["a", "c"]
    )["status"] == "inconsistent"


# ---- 接口级 -----------------------------------------------------------------

def _register(client, payload=None):
    resp = client.post("/templates", json=payload or make_template_payload())
    assert resp.status_code == 201, resp.text
    return resp.json()["template_id"]


def test_api_bind_success_frozen_and_echoed(client, db_path):
    # b.release=10 且 b -> c(10)；delay a=40 后绑定 a 与 b：
    # 绑定把 a=40 回传抬起 b（不能先算旧表 b=10 再抹平），c 随之 50。
    payload = {
        "points": [{"id": "a", "release": 0, "latest": 100},
                   {"id": "b", "release": 10, "latest": 100},
                   {"id": "c", "release": 0, "latest": 100}],
        "relations": [{"from": "b", "to": "c", "min_gap": 10}],
    }
    template_id = _register(client, payload)
    resp = client.post("/derivations", json={
        "template_id": template_id, "delay": {"a": 40}, "bind": ["b", "a"]})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # 绑定按 ID 升序规范化并回显、冻结（与录入次序无关）。
    assert body["bind"] == ["a", "b"]
    assert body["times"] == {"a": 40, "b": 40, "c": 50}

    # 模拟重启：按 ID 读取，用冻结的模板 + delay + 绑定重算核对。
    with TestClient(create_app(db_path=db_path)) as restarted:
        got = restarted.get(f"/results/{body['result_id']}")
        assert got.status_code == 200, got.text
        assert got.json()["bind"] == ["a", "b"]
        assert got.json()["times"] == body["times"]


def test_api_bind_explanation_survives_restart(client, db_path):
    payload = {
        "points": [{"id": "a", "release": 0, "latest": 100},
                   {"id": "b", "release": 10, "latest": 100},
                   {"id": "c", "release": 0, "latest": 100}],
        "relations": [{"from": "b", "to": "c", "min_gap": 10}],
    }
    template_id = _register(client, payload)
    created = client.post("/derivations", json={
        "template_id": template_id, "delay": {"a": 40},
        "bind": ["b", "a"], "explain": True})
    assert created.status_code == 201, created.text
    created_body = created.json()
    explanations = created_body["explanations"]
    # b 的证据穿过可辨认的绑定零边：起点是 delay 的 a，a==b（bound）。
    assert explanations["b"]["edges"] == [bound_edge("a", "b")]
    assert explanations["b"]["start"] == {"id": "a", "kind": "delay",
                                         "value": 40}
    # c 的证据先穿绑定边、再沿 b->c(10)：绑定造成的抬升继续下传。
    assert explanations["c"]["path"] == ["a", "b", "c"]
    assert explanations["c"]["edges"] == [
        bound_edge("a", "b"),
        {"from": "b", "to": "c", "min_gap": 10}]
    assert explanations["c"]["time"] == 50
    # 每条证据的边都可区分是否为同刻约束，且逐边相加等于时刻。
    for pid, evidence in explanations.items():
        assert evidence["time"] == created_body["times"][pid]
        assert chain_sum(evidence) == evidence["time"]
        for edge in evidence["edges"]:
            if edge.get("bound"):
                assert edge["min_gap"] == 0
                assert set(edge) == {"from", "to", "min_gap", "bound"}
            else:
                assert "bound" not in edge

    # 重启后取回的解释与创建响应逐字一致（确定性证据，由冻结输入重算）。
    with TestClient(create_app(db_path=db_path)) as restarted:
        got = restarted.get(
            f"/results/{created_body['result_id']}?explain=true")
        assert got.status_code == 200, got.text
        assert got.json()["explanations"] == created_body["explanations"]
        assert got.json()["bind"] == ["a", "b"]


def test_api_bind_swapped_order_gives_identical_result(client):
    payload = {
        "points": [{"id": "a", "release": 0},
                   {"id": "b", "release": 10},
                   {"id": "c", "release": 0}],
        "relations": [{"from": "b", "to": "c", "min_gap": 10}],
    }
    template_id = _register(client, payload)
    one = client.post("/derivations", json={
        "template_id": template_id, "delay": {"b": 40}, "bind": ["a", "b"]})
    two = client.post("/derivations", json={
        "template_id": template_id, "delay": {"b": 40}, "bind": ["b", "a"]})
    assert one.status_code == two.status_code == 201
    assert one.json()["times"] == two.json()["times"] == {
        "a": 40, "b": 40, "c": 50}
    assert one.json()["bind"] == two.json()["bind"] == ["a", "b"]


def test_api_bind_positive_gap_path_is_422_and_no_record(client, db_path):
    payload = {
        "points": [{"id": "a", "release": 0}, {"id": "b", "release": 0}],
        "relations": [{"from": "a", "to": "b", "min_gap": 5}],
    }
    template_id = _register(client, payload)
    resp = client.post("/derivations",
                       json={"template_id": template_id, "bind": ["a", "b"]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "positive_cycle"

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    finally:
        conn.close()


def test_api_bind_deadline_violation_is_422_and_no_record(client, db_path):
    payload = {
        "points": [{"id": "a", "release": 0, "latest": 10},
                   {"id": "b", "release": 0, "latest": 4}],
        "relations": [],
    }
    template_id = _register(client, payload)
    resp = client.post("/derivations", json={
        "template_id": template_id, "delay": {"a": 9}, "bind": ["a", "b"]})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "deadline_exceeded"
    assert err["details"]["violations"] == [
        {"id": "b", "earliest": 9, "latest": 4}]
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    finally:
        conn.close()


def test_api_invalid_bind_shapes_rejected(client):
    template_id = _register(client)
    base = {"template_id": template_id}
    bad_binds = [
        ["a"],                       # 少于两个
        ["a", "b", "c"],             # 多于两个
        ["a", "a"],                  # 必须是两个不同的点
        ["a", "ghost"],              # 引用不存在的点
        ["ghost", "a"],
        "a,b",                        # 必须是数组
        {"a": "b"},                  # 必须是数组
        [1, 2],                      # 元素必须是字符串
    ]
    for bad in bad_binds:
        resp = client.post("/derivations", json={**base, "bind": bad})
        assert resp.status_code == 400, bad
        assert resp.json()["error"]["code"] == "invalid_delay", bad
    # 非法绑定不落库、也不推演。
    resp = client.post("/derivations", json={**base, "bind": ["a"]})
    assert resp.status_code == 400


def test_api_legacy_results_without_bind_keep_old_shape(client, db_path):
    template_id = _register(client)
    resp = client.post("/derivations", json={
        "template_id": template_id, "delay": {"a": 10}})
    assert resp.status_code == 201, resp.text
    post_body = resp.json()
    # 未传绑定：响应逐项与旧版一致，无 bind 字段。
    assert set(post_body) == {"result_id", "template_id", "created_at",
                              "delay", "times", "points"}

    with TestClient(create_app(db_path=db_path)) as restarted:
        plain = restarted.get(f"/results/{post_body['result_id']}")
        assert plain.status_code == 200
        assert set(plain.json()) == {"result_id", "template_id", "created_at",
                                     "delay", "times", "points"}
        # 带解释读取同样没有绑定边标记。
        explained = restarted.get(
            f"/results/{post_body['result_id']}?explain=true")
        assert explained.status_code == 200
        for evidence in explained.json()["explanations"].values():
            assert all("bound" not in edge for edge in evidence["edges"])


def test_api_bind_without_delay_is_allowed(client):
    template_id = _register(client)
    resp = client.post("/derivations",
                       json={"template_id": template_id, "bind": ["b", "c"]})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["bind"] == ["b", "c"]
    # 无 delay 时 b=5, c=20；绑定后两者拉平到 20，d 仍 max(1, 20+3, 20)=23。
    assert body["times"] == {"a": 0, "b": 20, "c": 20, "d": 23}
