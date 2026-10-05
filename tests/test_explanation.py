"""因果解释：证据链构造、等价链裁决、零环、截止违约、重算核对与重启读取。

契约要点：

* 每条证据从某点的原始 release 或本次 delay 出发，沿实际取紧的关系到达
  目标点，逐边 min_gap 相加恰好等于返回的最早时刻；
* 等价链先取关系数最少者，再按完整点 ID 序列字节序取唯一结果；
* 零间隔环不得使追溯循环；
* 正权环仍优先报原错误；截止违约给出 ID 最小的违约点及其证据，失败结果
  不落库；
* 解释不落库：读取时由冻结模板 + delay 重算核对，不一致明确报错且不改写
  记录；未请求解释的旧响应逐项不变。
"""

from __future__ import annotations

import json
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


def chain_sum(evidence):
    return evidence["start"]["value"] + sum(
        e["min_gap"] for e in evidence["edges"])


# ---- 分支汇合：证据沿实际取紧的边 -------------------------------------------

def test_explanation_follows_tight_branch_at_reconvergence():
    # 菱形汇合：d 的时刻由 a->c->d 决定（50）；a->b->d 只有 38，不取紧。
    points = [pt("a", 0), pt("b", 0), pt("c", 0), pt("d", 1)]
    relations = [rel("a", "b", 5), rel("a", "c", 20),
                 rel("b", "d", 3), rel("c", "d", 0)]
    out = explain_schedule(points, relations, delay={"a": 30})
    assert out["status"] == STATUS_OK
    assert out["times"] == {"a": 30, "b": 35, "c": 50, "d": 50}

    ev = out["explanations"]["d"]
    assert ev["target"] == "d"
    assert ev["start"] == {"id": "a", "kind": "delay", "value": 30}
    assert ev["edges"] == [{"from": "a", "to": "c", "min_gap": 20},
                           {"from": "c", "to": "d", "min_gap": 0}]
    assert ev["path"] == ["a", "c", "d"]
    assert ev["time"] == 50
    assert chain_sum(ev) == ev["time"]

    # b 沿 a->b；a 自身是 delay 起点（0 条关系）。
    assert out["explanations"]["b"]["path"] == ["a", "b"]
    assert out["explanations"]["a"]["edges"] == []
    assert out["explanations"]["a"]["start"]["kind"] == "delay"


def test_explanation_start_kind_release_vs_delay():
    out = explain_schedule([pt("a", 5), pt("b", 8)], [], delay={"a": 5})
    assert out["explanations"]["a"]["start"] == {
        "id": "a", "kind": "delay", "value": 5}
    assert out["explanations"]["b"]["start"] == {
        "id": "b", "kind": "release", "value": 8}


# ---- 等价链裁决：先关系数最少，再按完整点 ID 序列字节序 ----------------------

def test_equivalent_chains_prefer_fewest_relations():
    # z 有两条等价证据链：
    #   ("m","z")      1 条关系：m.release=10, m->z(0)
    #   ("a","b","z")  2 条关系：a.release=6, a->b(2), b->z(2)
    # 尽管 ("a","b","z") 字节序更小，关系数最少者优先。
    points = [pt("a", 6), pt("b", 0), pt("m", 10), pt("z", 0)]
    relations = [rel("a", "b", 2), rel("b", "z", 2), rel("m", "z", 0)]
    out = explain_schedule(points, relations)
    ev = out["explanations"]["z"]
    assert ev["path"] == ["m", "z"]
    assert ev["edges"] == [{"from": "m", "to": "z", "min_gap": 0}]
    assert ev["time"] == 10
    assert chain_sum(ev) == 10


def test_equivalent_chains_byte_order_tiebreak():
    # 两条链关系数相同（各 1 条）：按完整点 ID 序列字节序取 ("a1","z")。
    points = [pt("b1", 10), pt("a1", 10), pt("z", 0)]
    relations = [rel("b1", "z", 0), rel("a1", "z", 0)]
    out = explain_schedule(points, relations)
    ev = out["explanations"]["z"]
    assert ev["path"] == ["a1", "z"]
    assert ev["start"] == {"id": "a1", "kind": "release", "value": 10}
    # 输入顺序不影响裁决结果。
    out2 = explain_schedule(list(reversed(points)), list(reversed(relations)))
    assert out2["explanations"]["z"] == ev


def test_explanations_order_independent_under_shuffling():
    points = [pt("a", 2), pt("b", 0), pt("c", 7), pt("d", 1)]
    relations = [rel("a", "b", 3), rel("b", "c", 4), rel("a", "c", 1)]
    base = explain_schedule(points, relations)["explanations"]
    rng = random.Random(20260926)
    for _ in range(20):
        shuffled_points = points[:]
        shuffled_relations = relations[:]
        rng.shuffle(shuffled_points)
        rng.shuffle(shuffled_relations)
        out = explain_schedule(shuffled_points, shuffled_relations)
        assert out["explanations"] == base


# ---- 零间隔环：追溯不得循环 -------------------------------------------------

def test_zero_gap_cycle_explanation_terminates():
    # 零权环 a->b->c->a：环上各点拉平到 3，唯一链起点是 c（release=3）。
    points = [pt("a", 1), pt("b", 2), pt("c", 3)]
    relations = [rel("a", "b", 0), rel("b", "c", 0), rel("c", "a", 0)]
    out = explain_schedule(points, relations)
    assert out["status"] == STATUS_OK
    assert out["explanations"]["c"]["path"] == ["c"]
    assert out["explanations"]["c"]["edges"] == []
    assert out["explanations"]["a"]["path"] == ["c", "a"]
    assert out["explanations"]["b"]["path"] == ["c", "a", "b"]
    # 每条证据的边都在环上取紧，但链本身不含环（点不重复）。
    for ev in out["explanations"].values():
        assert len(ev["path"]) == len(set(ev["path"]))
        assert chain_sum(ev) == ev["time"]


def test_zero_gap_self_loop_explanation_is_empty_chain():
    out = explain_schedule([pt("a", 4)], [rel("a", "a", 0)])
    assert out["status"] == STATUS_OK
    assert out["explanations"]["a"]["path"] == ["a"]
    assert out["explanations"]["a"]["edges"] == []


def test_zero_cycle_with_external_entry_chain():
    # 零权环 a<->b 由外部 d->a(12) 抬起：b 的链穿环一次即达，不循环。
    points = [pt("a", 0), pt("b", 0), pt("d", 0)]
    relations = [rel("a", "b", 0), rel("b", "a", 0), rel("d", "a", 12)]
    out = explain_schedule(points, relations)
    assert out["status"] == STATUS_OK
    assert out["explanations"]["a"]["path"] == ["d", "a"]
    assert out["explanations"]["b"]["path"] == ["d", "a", "b"]
    assert chain_sum(out["explanations"]["b"]) == 12


# ---- 截止违约与正权环 -------------------------------------------------------

def test_deadline_explanation_targets_smallest_violation_id():
    # q 与 p 都违约；ID 最小者是 p（注意输入顺序故意颠倒）。
    points = [pt("q", 9, latest=3), pt("p", 5, latest=2)]
    out = explain_schedule(points, [])
    assert out["status"] == STATUS_DEADLINE_EXCEEDED
    assert out["explanation"]["id"] == "p"
    ev = out["explanation"]["evidence"]
    assert ev["target"] == "p"
    assert ev["path"] == ["p"]
    assert ev["edges"] == []
    assert ev["start"] == {"id": "p", "kind": "release", "value": 5}
    assert ev["time"] == 5


def test_positive_cycle_has_no_explanation():
    points = [pt("a", 0), pt("b", 0)]
    relations = [rel("a", "b", 1), rel("b", "a", 1)]
    assert explain_schedule(points, relations) == {
        "status": STATUS_POSITIVE_CYCLE}


# ---- 重算核对 ----------------------------------------------------------------

def test_verify_result_explanations_consistent():
    points = [pt("a", 0), pt("b", 0)]
    relations = [rel("a", "b", 5)]
    times = earliest_schedule(points, relations, delay={"a": 7})["times"]
    check = verify_result_explanations(points, relations, {"a": 7}, times)
    assert check["status"] == STATUS_OK
    assert check["explanations"]["b"]["path"] == ["a", "b"]
    assert chain_sum(check["explanations"]["b"]) == times["b"]


def test_verify_result_explanations_detects_inconsistency():
    points = [pt("a", 0), pt("b", 0)]
    relations = [rel("a", "b", 5)]
    # 时刻被篡改。
    assert verify_result_explanations(
        points, relations, {}, {"a": 0, "b": 999})["status"] == "inconsistent"
    # 键缺失。
    assert verify_result_explanations(
        points, relations, {}, {"a": 0})["status"] == "inconsistent"
    # 键多余。
    assert verify_result_explanations(
        points, relations, {},
        {"a": 0, "b": 5, "ghost": 1})["status"] == "inconsistent"
    # delay 与保存结果不匹配。
    assert verify_result_explanations(
        points, relations, {"a": 3},
        {"a": 0, "b": 5})["status"] == "inconsistent"


# ---- 接口级 ------------------------------------------------------------------

def _register(client, payload=None):
    resp = client.post("/templates", json=payload or make_template_payload())
    assert resp.status_code == 201, resp.text
    return resp.json()["template_id"]


def test_derivation_with_explain_returns_evidence(client):
    template_id = _register(client)
    resp = client.post("/derivations", json={
        "template_id": template_id, "delay": {"a": 30}, "explain": True})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["times"] == {"a": 30, "b": 35, "c": 50, "d": 50}

    explanations = body["explanations"]
    assert list(explanations) == ["a", "b", "c", "d"]  # 按点 ID 升序
    ev = explanations["d"]
    assert ev["path"] == ["a", "c", "d"]
    assert ev["start"] == {"id": "a", "kind": "delay", "value": 30}
    assert ev["edges"] == [{"from": "a", "to": "c", "min_gap": 20},
                           {"from": "c", "to": "d", "min_gap": 0}]
    assert ev["time"] == 50
    # 每条证据逐边相加恰好等于返回的最早时刻。
    for pid, evidence in explanations.items():
        assert evidence["target"] == pid
        assert evidence["time"] == body["times"][pid]
        assert chain_sum(evidence) == body["times"][pid]


def test_derivation_without_explain_response_unchanged(client):
    template_id = _register(client)
    resp = client.post("/derivations", json={
        "template_id": template_id, "delay": {"a": 30}})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # 旧响应逐项不变：没有 explanations 字段。
    assert set(body) == {"result_id", "template_id", "created_at", "delay",
                         "times", "points"}
    assert body["times"] == {"a": 30, "b": 35, "c": 50, "d": 50}

    # explain=false 显式等价于未请求。
    resp = client.post("/derivations", json={
        "template_id": template_id, "explain": False})
    assert resp.status_code == 201, resp.text
    assert "explanations" not in resp.json()


def test_explanation_read_by_id_after_restart(client, db_path):
    template_id = _register(client)
    created = client.post("/derivations", json={
        "template_id": template_id, "delay": {"a": 10},
        "explain": True}).json()
    result_id = created["result_id"]

    # 模拟容器重启：用同一个数据库文件重新创建应用。
    restarted_app = create_app(db_path=db_path)
    with TestClient(restarted_app) as restarted:
        resp = restarted.get(f"/results/{result_id}?explain=true")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # 同一解释：与创建响应逐字一致。
        assert body["explanations"] == created["explanations"]
        assert body["explanations"]["d"]["path"] == ["a", "c", "d"]

        # 未请求解释的读取保持旧形状，且与带解释读取的公共部分一致。
        plain = restarted.get(f"/results/{result_id}")
        assert plain.status_code == 200, plain.text
        assert set(plain.json()) == {"result_id", "template_id", "created_at",
                                     "delay", "times", "points"}
        assert plain.json() == {k: v for k, v in body.items()
                                if k != "explanations"}


def test_explanation_available_even_if_not_requested_at_creation(
        client, db_path):
    # 创建时未请求解释，事后仍可按 ID 读取同一解释（由冻结输入重算）。
    template_id = _register(client)
    created = client.post("/derivations", json={
        "template_id": template_id, "delay": {"a": 10}}).json()
    assert "explanations" not in created

    resp = client.get(f"/results/{created['result_id']}?explain=true")
    assert resp.status_code == 200, resp.text
    explanations = resp.json()["explanations"]
    assert explanations["d"]["path"] == ["a", "c", "d"]
    assert explanations["d"]["start"] == {"id": "a", "kind": "delay",
                                          "value": 10}


def test_deadline_exceeded_with_explain_gives_smallest_id_evidence(
        client, db_path):
    payload = {
        "points": [
            {"id": "a", "release": 0, "latest": 10},
            {"id": "b", "release": 0, "latest": 5},
            {"id": "c", "release": 0, "latest": 6},
        ],
        "relations": [{"from": "a", "to": "b", "min_gap": 8},
                      {"from": "b", "to": "c", "min_gap": 1}],
    }
    # times: a=0, b=8, c=9 → b(>5) 与 c(>6) 都违约；ID 最小违约点是 b。
    template_id = _register(client, payload)
    resp = client.post("/derivations", json={
        "template_id": template_id, "explain": True})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "deadline_exceeded"
    assert err["details"]["violations"] == [
        {"id": "b", "earliest": 8, "latest": 5},
        {"id": "c", "earliest": 9, "latest": 6}]
    explanation = err["details"]["explanation"]
    assert explanation["id"] == "b"
    ev = explanation["evidence"]
    assert ev["path"] == ["a", "b"]
    assert ev["start"] == {"id": "a", "kind": "release", "value": 0}
    assert ev["time"] == 8
    assert chain_sum(ev) == 8

    # 失败推演不落库。
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    finally:
        conn.close()


def test_deadline_exceeded_without_explain_response_unchanged(client):
    payload = {
        "points": [
            {"id": "a", "release": 0, "latest": 10},
            {"id": "b", "release": 0, "latest": 5},
        ],
        "relations": [{"from": "a", "to": "b", "min_gap": 8}],
    }
    template_id = _register(client, payload)
    resp = client.post("/derivations", json={"template_id": template_id})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "deadline_exceeded"
    # 旧响应逐项不变：details 只有 violations。
    assert set(err["details"]) == {"violations"}


def test_positive_cycle_with_explain_still_reports_original_error(client):
    payload = {
        "points": [{"id": "a", "release": 0}, {"id": "b", "release": 0}],
        "relations": [{"from": "a", "to": "b", "min_gap": 1},
                      {"from": "b", "to": "a", "min_gap": 1}],
    }
    template_id = _register(client, payload)
    resp = client.post("/derivations", json={
        "template_id": template_id, "explain": True})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "positive_cycle"
    assert "explanation" not in err.get("details", {})


def test_stored_result_inconsistency_is_reported_and_record_untouched(
        client, db_path):
    template_id = _register(client)
    created = client.post("/derivations", json={
        "template_id": template_id, "delay": {"a": 10}}).json()
    result_id = created["result_id"]

    # 外部篡改保存的时刻（与冻结模板 + delay 不再一致）。
    tampered = {"a": 10, "b": 15, "c": 30, "d": 999}
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE results SET times_json = ? WHERE id = ?",
                     (json.dumps(tampered), result_id))
        conn.commit()
    finally:
        conn.close()

    resp = client.get(f"/results/{result_id}?explain=true")
    assert resp.status_code == 500
    err = resp.json()["error"]
    assert err["code"] == "result_inconsistent"
    assert err["details"] == {"result_id": result_id}

    # 记录未被改写：未请求解释的读取行为不变，仍返回保存值。
    plain = client.get(f"/results/{result_id}")
    assert plain.status_code == 200
    assert plain.json()["times"] == tampered
    conn = sqlite3.connect(db_path)
    try:
        stored = conn.execute(
            "SELECT times_json FROM results WHERE id = ?",
            (result_id,)).fetchone()[0]
    finally:
        conn.close()
    assert json.loads(stored) == tampered

    # 再次请求解释仍是明确报错（记录没有被"修复"）。
    again = client.get(f"/results/{result_id}?explain=true")
    assert again.status_code == 500
    assert again.json()["error"]["code"] == "result_inconsistent"


def test_explain_field_must_be_boolean(client):
    template_id = _register(client)
    resp = client.post("/derivations", json={
        "template_id": template_id, "explain": "yes"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_delay"


def test_explain_query_param_must_be_true_or_false(client):
    template_id = _register(client)
    result_id = client.post(
        "/derivations", json={"template_id": template_id}).json()["result_id"]

    resp = client.get(f"/results/{result_id}?explain=maybe")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"

    resp = client.get(f"/results/{result_id}?explain=false")
    assert resp.status_code == 200
    assert "explanations" not in resp.json()
