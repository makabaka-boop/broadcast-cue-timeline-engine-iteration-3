"""可选"同刻绑定"（两个不同提示点同一时刻播发）的延误推演。

绑定语义：在差分约束图上同时加入 ``x -> y`` 与 ``y -> x`` 两条零间隔边，
与普通先后关系一起松弛。因此：

* 延误既沿绑定**回传**（较晚的点把较早的点抬起），又会从被抬起的点沿
  既有关系**再次传播**——绝不是"先算无绑定的旧表、再把两个时刻改成
  同一个数字"（那会漏掉回传后的二次传播）；
* 若原关系在两点间形成**正间隔**路径，该路径与反向零边合为正权环，
  仍按既有优先级报 ``positive_cycle``，且失败不生成结果；
* 绑定零边与零权关系组成的零环合法，但追溯解释不得在环上循环；
* 绑定规范化（两点按 ID 升序）后冻结入库，按 ID 读取时用模板 + delay +
  绑定重算核对；解释里绑定边是带 ``"binding": true`` 的可辨认零间隔边；
* 输入两点次序不影响结果；未传绑定的旧结果与旧响应逐项兼容。
"""

from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from app.main import create_app
from app.scheduler import (STATUS_DEADLINE_EXCEEDED, STATUS_OK,
                           STATUS_POSITIVE_CYCLE, earliest_schedule,
                           explain_schedule, normalize_bind,
                           verify_result_explanations)
from tests.conftest import make_template_payload


def pt(pid, release, latest=None):
    return {"id": pid, "release": release, "latest": latest}


def rel(u, v, gap):
    return {"from": u, "to": v, "min_gap": gap}


def chain_sum(evidence):
    return evidence["start"]["value"] + sum(
        e["min_gap"] for e in evidence["edges"])


# ---- 双向传播：回传之后还必须再次传播 ---------------------------------------

def test_binding_lifts_earlier_point_backward():
    # y.release=20，x.release=0；绑定后 x 必须被 y 抬到同一时刻 20。
    points = [pt("x", 0), pt("y", 20)]
    out = earliest_schedule(points, [], bind=("y", "x"))
    assert out["status"] == STATUS_OK
    assert out["times"] == {"x": 20, "y": 20}


def test_binding_backward_then_repropagates_downstream():
    # 关键反例，说明不能"先算旧表再把两点改成同一个数字"：
    #   无绑定旧表：x=0, z=x+7=7, y=20；把 x 改成 y 的 20 后，
    #   旧做法会留下 z=7——但回传使 x=20，延误必须沿 x->z 再次传播到 27。
    points = [pt("x", 0), pt("z", 0), pt("y", 20)]
    relations = [rel("x", "z", 7)]

    out = earliest_schedule(points, relations, bind=("y", "x"))
    assert out["status"] == STATUS_OK
    assert out["times"] == {"x": 20, "y": 20, "z": 27}

    # 旧表 + 手工改同值确实会给出错误答案，锁定二者差异。
    old = earliest_schedule(points, relations)
    assert old["times"] == {"x": 0, "y": 20, "z": 7}
    naive = dict(old["times"])
    naive["x"] = naive["y"] = max(naive["x"], naive["y"])
    assert naive == {"x": 20, "y": 20, "z": 7}
    assert naive != out["times"]  # z=7 不满足 z >= x+7=27


def test_binding_propagates_forward_when_bound_point_is_later_source():
    # 反方向同样成立：x=30（delay）抬起 y，并继续沿 y->q 顺延。
    points = [pt("x", 0), pt("y", 0), pt("q", 0)]
    relations = [rel("y", "q", 5)]
    out = earliest_schedule(points, relations, delay={"x": 30},
                            bind=("x", "y"))
    assert out["status"] == STATUS_OK
    assert out["times"] == {"x": 30, "y": 30, "q": 35}


def test_binding_takes_max_over_all_incoming_paths():
    # 绑定点之外还有更长的正常路径，汇合仍取最大下界。
    points = [pt("a", 0), pt("b", 0), pt("c", 0)]
    relations = [rel("a", "c", 50)]
    out = earliest_schedule(points, relations, delay={"b": 20},
                            bind=("a", "b"))
    # a=b=20（绑定），c=max(a+50=70)=70。
    assert out["status"] == STATUS_OK
    assert out["times"] == {"a": 20, "b": 20, "c": 70}


# ---- 零间隔环：绑定零边与零权关系合环合法，解释不循环 ------------------------

def test_binding_zero_cycle_is_feasible_and_flattens():
    # 原关系 a->b(0)->c(0)；绑定 c,a 后 c<->a 零边与关系链合成零环，
    # 环上各点拉平到最大基础下界 10。
    points = [pt("a", 1), pt("b", 2), pt("c", 10)]
    relations = [rel("a", "b", 0), rel("b", "c", 0)]
    out = earliest_schedule(points, relations, bind=("c", "a"))
    assert out["status"] == STATUS_OK
    assert out["times"] == {"a": 10, "b": 10, "c": 10}


def test_binding_zero_self_cycle_pair_is_two_zero_edges():
    # 绑定本身就是一对相向零边：a<->b，release 大者拉平对方。
    points = [pt("a", 4), pt("b", 9)]
    out = earliest_schedule(points, [], bind=("a", "b"))
    assert out["status"] == STATUS_OK
    assert out["times"] == {"a": 9, "b": 9}


def test_explanation_through_binding_zero_cycle_does_not_loop():
    points = [pt("a", 1), pt("b", 2), pt("c", 10)]
    relations = [rel("a", "b", 0), rel("b", "c", 0)]
    out = explain_schedule(points, relations, bind=("c", "a"))
    assert out["status"] == STATUS_OK
    explanations = out["explanations"]

    # c 是唯一链起点（release=10）；a 经一条绑定零边到达；b 再穿关系边。
    assert explanations["c"]["path"] == ["c"]
    assert explanations["c"]["edges"] == []
    assert explanations["a"]["path"] == ["c", "a"]
    assert explanations["a"]["edges"] == [
        {"from": "c", "to": "a", "min_gap": 0, "binding": True}]
    assert explanations["b"]["path"] == ["c", "a", "b"]
    assert explanations["b"]["edges"] == [
        {"from": "c", "to": "a", "min_gap": 0, "binding": True},
        {"from": "a", "to": "b", "min_gap": 0}]

    # 所有证据链点不重复（零环未使追溯循环），且逐边相加等于最早时刻。
    for evidence in explanations.values():
        assert len(evidence["path"]) == len(set(evidence["path"]))
        assert chain_sum(evidence) == evidence["time"]


def test_explanation_tight_relation_and_binding_both_recognized():
    # y=20 绑定 x，x->z(7) 再传播：z 的证据穿过绑定零边后接正常关系边。
    points = [pt("x", 0), pt("z", 0), pt("y", 20)]
    relations = [rel("x", "z", 7)]
    out = explain_schedule(points, relations, bind=("y", "x"))
    assert out["status"] == STATUS_OK
    evidence = out["explanations"]["z"]
    assert evidence["path"] == ["y", "x", "z"]
    assert evidence["edges"] == [
        {"from": "y", "to": "x", "min_gap": 0, "binding": True},
        {"from": "x", "to": "z", "min_gap": 7}]
    assert chain_sum(evidence) == 27
    # 被回传抬起的 x 也以绑定边为证据；y 自身是 release 起点。
    assert out["explanations"]["x"]["edges"] == [
        {"from": "y", "to": "x", "min_gap": 0, "binding": True}]
    assert out["explanations"]["y"]["start"]["kind"] == "release"


def test_binding_edge_preferred_over_parallel_zero_relation():
    # 模板已有 a->b(0) 的零关系，又绑定 [a,b]：b 被抬起时证据应走可辨认
    # 的绑定边，而不是外表相同的普通零关系。
    points = [pt("a", 0), pt("b", 8)]
    relations = [rel("a", "b", 0)]
    out = explain_schedule(points, relations, bind=("a", "b"))
    assert out["explanations"]["a"]["edges"] == [
        {"from": "b", "to": "a", "min_gap": 0, "binding": True}]
    assert out["explanations"]["b"]["edges"] == []


# ---- 正间隔路径：与既有先后关系冲突 => positive_cycle -----------------------

def test_positive_gap_path_direct_relation_conflicts_with_binding():
    # a->b 要求 b >= a+5，绑定又要求 a == b：无解 => 正权环。
    points = [pt("a", 0), pt("b", 0)]
    relations = [rel("a", "b", 5)]
    out = earliest_schedule(points, relations, bind=("a", "b"))
    assert out["status"] == STATUS_POSITIVE_CYCLE
    assert "times" not in out
    assert "violations" not in out


def test_positive_gap_path_multi_hop_conflicts_with_binding():
    # 路径 a->x(2)->b(3) 总间隔 5 > 0，绑定 b,a 后合为正权环。
    points = [pt("a", 0), pt("x", 0), pt("b", 0)]
    relations = [rel("a", "x", 2), rel("x", "b", 3)]
    assert earliest_schedule(points, relations, bind=("b", "a")
                             )["status"] == STATUS_POSITIVE_CYCLE


def test_zero_gap_path_between_bound_points_is_feasible():
    # 对照：两点间只有零间隔路径时不冲突，绑定合法。
    points = [pt("a", 0), pt("x", 0), pt("b", 6)]
    relations = [rel("a", "x", 0), rel("x", "b", 0)]
    out = earliest_schedule(points, relations, bind=("b", "a"))
    assert out["status"] == STATUS_OK
    assert out["times"] == {"a": 6, "b": 6, "x": 6}


def test_preexisting_positive_cycle_still_reported_first():
    # 即使绑定本身无害，模板原有的正权环仍优先报 positive_cycle。
    points = [pt("a", 0, latest=0), pt("b", 0), pt("c", 0, latest=0)]
    relations = [rel("a", "b", 1), rel("b", "a", 1)]
    out = earliest_schedule(points, relations, bind=("a", "c"))
    assert out["status"] == STATUS_POSITIVE_CYCLE


def test_binding_positive_cycle_has_no_explanation():
    points = [pt("a", 0), pt("b", 0)]
    assert explain_schedule(points, [rel("a", "b", 1)], bind=("a", "b")) == {
        "status": STATUS_POSITIVE_CYCLE}


# ---- latest 截止：先环后截止，绑定抬升导致违约 ------------------------------

def test_binding_lift_violates_latest():
    # a.latest=5，绑定把 a 抬到 b 的 20 => 截止违约（无正权环）。
    points = [pt("a", 0, 5), pt("b", 20, 100)]
    out = earliest_schedule(points, [], bind=("a", "b"))
    assert out["status"] == STATUS_DEADLINE_EXCEEDED
    assert out["violations"] == [
        {"id": "a", "earliest": 20, "latest": 5}]


def test_binding_deadline_explanation_uses_binding_edge():
    points = [pt("a", 0, 5), pt("b", 20, 100)]
    out = explain_schedule(points, [], bind=("a", "b"))
    assert out["status"] == STATUS_DEADLINE_EXCEEDED
    evidence = out["explanation"]["evidence"]
    assert out["explanation"]["id"] == "a"
    assert evidence["path"] == ["b", "a"]
    assert evidence["edges"] == [
        {"from": "b", "to": "a", "min_gap": 0, "binding": True}]
    assert chain_sum(evidence) == 20


def test_binding_within_latest_succeeds():
    points = [pt("a", 0, 20), pt("b", 0, 20)]
    out = earliest_schedule(points, [rel("a", "b", 20)], bind=("a", "b"))
    # a->b(20) 与绑定冲突，是正权环，而非截止问题——锁定优先级。
    assert out["status"] == STATUS_POSITIVE_CYCLE


# ---- 输入换序：规范化与结果次序无关 -----------------------------------------

def test_normalize_bind_is_order_canonical():
    assert normalize_bind(["a", "c"]) == ("a", "c")
    assert normalize_bind(["c", "a"]) == ("a", "c")
    assert normalize_bind(None) is None
    assert normalize_bind([]) is None


def test_bound_schedule_invariant_under_input_order():
    points = [pt("a", 0), pt("b", 0), pt("c", 0)]
    relations = [rel("a", "c", 4)]
    forward = earliest_schedule(points, relations, bind=("a", "b"))
    reverse = earliest_schedule(list(reversed(points)),
                                list(reversed(relations)), bind=("b", "a"))
    assert forward["status"] == reverse["status"] == STATUS_OK
    assert forward["times"] == reverse["times"] == {
        "a": 0, "b": 0, "c": 4}


def test_conflicting_binding_order_does_not_mask_positive_cycle():
    points = [pt("a", 0), pt("x", 0), pt("b", 0)]
    relations = [rel("a", "x", 2), rel("x", "b", 3)]
    # 无论绑定点录入次序如何，正间隔路径都报 positive_cycle。
    for order in (("a", "b"), ("b", "a")):
        assert earliest_schedule(points, relations, bind=order
                                 )["status"] == STATUS_POSITIVE_CYCLE


# ---- 重算核对：冻结绑定 ------------------------------------------------------

def test_verify_with_binding_consistent():
    points = [pt("x", 0), pt("z", 0), pt("y", 20)]
    relations = [rel("x", "z", 7)]
    times = earliest_schedule(points, relations, bind=("y", "x"))["times"]
    check = verify_result_explanations(points, relations, {}, times,
                                       bind=("y", "x"))
    assert check["status"] == STATUS_OK
    assert check["explanations"]["z"]["path"] == ["y", "x", "z"]
    # 换序的绑定重算结果相同（规范化）。
    swapped = verify_result_explanations(points, relations, {}, times,
                                         bind=("x", "y"))
    assert swapped["status"] == STATUS_OK
    assert swapped["explanations"] == check["explanations"]


def test_verify_detects_missing_or_extra_binding():
    points = [pt("x", 0), pt("z", 0), pt("y", 20)]
    relations = [rel("x", "z", 7)]
    bound_times = {"x": 20, "y": 20, "z": 27}
    plain_times = {"x": 0, "y": 20, "z": 7}
    # 保存的是绑定结果，却按无绑定重算 => 不一致。
    assert verify_result_explanations(
        points, relations, {}, bound_times)["status"] == "inconsistent"
    # 保存的是无绑定结果，却按有绑定重算 => 不一致。
    assert verify_result_explanations(
        points, relations, {}, plain_times, bind=("y", "x")
    )["status"] == "inconsistent"
    # 篡改时刻同样不一致。
    assert verify_result_explanations(
        points, relations, {}, {"x": 20, "y": 20, "z": 28},
        bind=("y", "x"))["status"] == "inconsistent"


# ---- 接口级 ------------------------------------------------------------------

def _register(client, payload=None):
    resp = client.post("/templates", json=payload or make_template_payload())
    assert resp.status_code == 201, resp.text
    return resp.json()["template_id"]


def test_api_binding_propagates_and_echoes_canonical_bind(client):
    # 模板菱形：a->b(5), a->c(20), b->d(3), c->d(0)；绑定 b,c 同刻。
    # b=c=max(5,20)=20（c 抬起 b），d=max(1, b+3=23, c+0=20)=23
    # ——再次传播使 d 从无绑定的 20 推迟到 23。
    template_id = _register(client)
    resp = client.post("/derivations", json={
        "template_id": template_id, "bind": ["c", "b"]})  # 故意逆序
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["bind"] == ["b", "c"]  # 响应回传规范化绑定
    assert body["times"] == {"a": 0, "b": 20, "c": 20, "d": 23}
    assert [row["time"] for row in body["points"]] == [0, 20, 20, 23]


def test_api_binding_input_order_gives_identical_results(client):
    template_id = _register(client)
    r1 = client.post("/derivations",
                     json={"template_id": template_id, "bind": ["b", "c"]})
    r2 = client.post("/derivations",
                     json={"template_id": template_id, "bind": ["c", "b"]})
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["bind"] == r2.json()["bind"] == ["b", "c"]
    assert r1.json()["times"] == r2.json()["times"]


def test_api_binding_with_delay_and_explain_freezes_binding(client, db_path):
    template_id = _register(client)
    created = client.post("/derivations", json={
        "template_id": template_id, "delay": {"a": 30},
        "bind": ["d", "c"], "explain": True})  # 故意逆序 -> 规范化 [c,d]
    # a=30 -> b=35,c=50；c->d(0) 上绑定 c,d，d=max(1,b+3=38,c+0=50)=50。
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["bind"] == ["c", "d"]
    assert body["times"] == {"a": 30, "b": 35, "c": 50, "d": 50}
    result_id = body["result_id"]

    # 重启后按 ID 读取：用冻结模板 + delay + 绑定重算，解释逐字一致。
    restarted_app = create_app(db_path=db_path)
    with TestClient(restarted_app) as restarted:
        got = restarted.get(f"/results/{result_id}?explain=true")
        assert got.status_code == 200, got.text
        read_body = got.json()
        assert read_body["bind"] == ["c", "d"]
        assert read_body["times"] == body["times"]
        assert read_body["explanations"] == body["explanations"]

        # 解释里绑定边可辨认，且每条证据逐边相加等于最早时刻、点不重复。
        for pid, evidence in read_body["explanations"].items():
            assert evidence["time"] == read_body["times"][pid]
            assert chain_sum(evidence) == read_body["times"][pid]
            assert len(evidence["path"]) == len(set(evidence["path"]))
        # d 的证据沿 c->d 绑定零边到达（不是 b->d(3) 的 38 分支）。
        assert read_body["explanations"]["d"]["edges"][-1] == {
            "from": "c", "to": "d", "min_gap": 0, "binding": True}

        # 不带解释的读取同样带冻结绑定。
        plain = restarted.get(f"/results/{result_id}")
        assert plain.status_code == 200
        assert plain.json()["bind"] == ["c", "d"]
        assert plain.json()["times"] == body["times"]


def test_api_positive_gap_binding_conflict_422_and_no_record(client, db_path):
    payload = {
        "points": [{"id": "a", "release": 0}, {"id": "b", "release": 0}],
        "relations": [{"from": "a", "to": "b", "min_gap": 3}],
    }
    template_id = _register(client, payload)
    resp = client.post("/derivations",
                       json={"template_id": template_id, "bind": ["a", "b"],
                             "explain": True})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "positive_cycle"
    assert "explanation" not in err.get("details", {})

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
        stored = conn.execute("SELECT bind_json FROM results").fetchall()
    finally:
        conn.close()
    assert stored == []  # 失败推演不冻结任何绑定、不生成结果


def test_api_binding_deadline_exceeded_422_and_no_record(client, db_path):
    payload = {
        "points": [{"id": "a", "release": 0, "latest": 5},
                   {"id": "b", "release": 20, "latest": 100}],
        "relations": [],
    }
    template_id = _register(client, payload)
    resp = client.post("/derivations", json={
        "template_id": template_id, "bind": ["a", "b"], "explain": True})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "deadline_exceeded"
    assert err["details"]["violations"] == [
        {"id": "a", "earliest": 20, "latest": 5}]
    evidence = err["details"]["explanation"]["evidence"]
    assert evidence["edges"][0]["binding"] is True

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    finally:
        conn.close()


def test_api_invalid_bind_shapes_are_rejected(client):
    template_id = _register(client)
    base = {"template_id": template_id}
    bad_binds = [
        ["a"],                       # 少于两个点
        ["a", "b", "c"],             # 多于两个点
        ["a", "a"],                  # 两个点必须不同
        ["a", "ghost"],             # 引用不存在的点
        ["a", 3],                    # 非字符串
        "ab",                        # 不是数组
        {"a": "b"},                  # 不是数组
        None,                        # 显式 null 不接受（缺省才是无绑定）
    ]
    for bad in bad_binds:
        resp = client.post("/derivations", json={**base, "bind": bad})
        assert resp.status_code == 400, bad
        assert resp.json()["error"]["code"] == "invalid_delay", bad


def test_api_bind_unknown_field_still_rejected(client):
    template_id = _register(client)
    resp = client.post("/derivations", json={
        "template_id": template_id, "bound": ["a", "b"]})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_delay"


# ---- 向后兼容：未传绑定的旧结果与旧响应 -------------------------------------

def test_api_derivation_without_bind_response_unchanged(client):
    template_id = _register(client)
    resp = client.post("/derivations", json={"template_id": template_id})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # 未传绑定时响应逐项不变：没有 bind 字段。
    assert set(body) == {"result_id", "template_id", "created_at", "delay",
                         "times", "points"}
    assert body["times"] == {"a": 0, "b": 5, "c": 20, "d": 20}


def test_api_unbound_result_has_no_bind_after_restart(client, db_path):
    template_id = _register(client)
    result_id = client.post(
        "/derivations", json={"template_id": template_id}
    ).json()["result_id"]

    with TestClient(create_app(db_path=db_path)) as restarted:
        plain = restarted.get(f"/results/{result_id}")
        assert plain.status_code == 200
        assert "bind" not in plain.json()
        explained = restarted.get(f"/results/{result_id}?explain=true")
        assert explained.status_code == 200
        # 无绑定重算核对成功，且解释中不含绑定标记边。
        for evidence in explained.json()["explanations"].values():
            assert not any(e.get("binding") for e in evidence["edges"])


def test_legacy_results_table_without_bind_column_still_reads(tmp_path):
    # 手工建立旧版 results 表（无 bind_json 列）与一条旧结果，验证迁移补列
    # 后旧记录按无绑定读取、重算核对成功。该测试不使用 client 夹具：
    # 夹具本身已初始化过数据库，旧表必须在首次 init 之前存在。
    legacy_db = str(tmp_path / "legacy.db")
    template = {
        "points": [{"id": "a", "release": 0, "latest": None}],
        "relations": [],
    }
    conn = sqlite3.connect(legacy_db)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute(
        "CREATE TABLE templates (id TEXT PRIMARY KEY, body_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT "
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))")
    conn.execute(
        "CREATE TABLE results (id TEXT PRIMARY KEY, template_id TEXT NOT NULL, "
        "delay_json TEXT NOT NULL, times_json TEXT NOT NULL, created_at TEXT "
        "NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))")
    conn.execute("INSERT INTO templates (id, body_json) VALUES (?, ?)",
                 ("tpl-legacy", json.dumps(template)))
    conn.execute(
        "INSERT INTO results (id, template_id, delay_json, times_json) "
        "VALUES (?, ?, ?, ?)",
        ("res-legacy", "tpl-legacy", json.dumps({}), json.dumps({"a": 0})))
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=legacy_db)) as restarted:
        plain = restarted.get("/results/res-legacy")
        assert plain.status_code == 200, plain.text
        assert "bind" not in plain.json()
        assert plain.json()["times"] == {"a": 0}

        explained = restarted.get("/results/res-legacy?explain=true")
        assert explained.status_code == 200, explained.text
        assert explained.json()["explanations"]["a"]["path"] == ["a"]


def test_tampered_bound_result_is_reported_and_record_untouched(client, db_path):
    template_id = _register(client)
    created = client.post("/derivations", json={
        "template_id": template_id, "bind": ["b", "c"]}).json()
    result_id = created["result_id"]
    assert created["times"] == {"a": 0, "b": 20, "c": 20, "d": 23}

    # 外部把绑定记录的时刻篡改成"旧表改同值"式错误答案（漏掉再传播）。
    tampered = {"a": 0, "b": 20, "c": 20, "d": 20}
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE results SET times_json = ? WHERE id = ?",
                     (json.dumps(tampered), result_id))
        conn.commit()
    finally:
        conn.close()

    resp = client.get(f"/results/{result_id}?explain=true")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "result_inconsistent"

    # 记录原样保留：不带解释仍读出篡改值，且冻结绑定未被改动。
    plain = client.get(f"/results/{result_id}")
    assert plain.status_code == 200
    assert plain.json()["times"] == tampered
    assert plain.json()["bind"] == ["b", "c"]
