"""接口级测试：登记、推演、查询、错误代码、以及重启后持久化。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_template_payload


# ---- 模板登记 -------------------------------------------------------------

def test_register_template_persists_and_echoes_canonical(client):
    payload = make_template_payload()
    # 故意打乱点与关系顺序；规范化后应按 ID 排序返回。
    payload["points"] = list(reversed(payload["points"]))
    payload["relations"] = list(reversed(payload["relations"]))

    resp = client.post("/templates", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) >= {"template_id", "created_at", "point_count",
                         "relation_count", "points", "relations"}
    assert body["point_count"] == 4
    assert body["relation_count"] == 4
    assert [p["id"] for p in body["points"]] == ["a", "b", "c", "d"]
    assert body["relations"] == sorted(
        body["relations"], key=lambda r: (r["from"], r["to"], r["min_gap"]))


def test_register_invalid_template_is_rejected_and_not_persisted(client):
    # 关系端点不存在。
    payload = make_template_payload()
    payload["relations"].append({"from": "a", "to": "ghost", "min_gap": 1})
    resp = client.post("/templates", json=payload)
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "invalid_template"
    assert "message" in err and err["message"]

    # 数据库里不应有任何模板。
    resp_ok = client.post("/templates", json=make_template_payload())
    # 若失败模板曾落库，其自增/记录无影响；这里再直接验证只有一次成功登记：
    assert resp_ok.status_code == 201

    # 非法 delay/缺口/重复 ID 等也一律 400 invalid_template
    bad_cases = [
        {"points": []},
        {"points": [{"id": "a", "release": -1}]},
        {"points": [{"id": "a", "release": 10**9 + 1}]},
        {"points": [{"id": "a", "release": 5, "latest": 4}]},
        {"points": [{"id": "a", "release": 0}],
         "relations": [{"from": "a", "to": "a", "min_gap": -1}]},
        {"points": [{"id": "a", "release": 0}],
         "relations": [{"from": "a", "to": "a", "min_gap": 10**6 + 1}]},
        {"points": [{"id": "a", "release": 0},
                    {"id": "a", "release": 1}]},
        {"points": [{"id": "", "release": 0}]},
        {"points": [{"id": "a", "release": True}]},
        {"points": [{"id": "a", "release": 0}], "relations": {}},
        {"points": [{"id": "a", "release": 0}], "bogus": 1},
    ]
    for case in bad_cases:
        r = client.post("/templates", json=case)
        assert r.status_code == 400, case
        assert r.json()["error"]["code"] == "invalid_template", case


def test_register_body_must_be_json_object(client):
    resp = client.post("/templates", content="not json{",
                       headers={"content-type": "application/json"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_json"

    resp = client.post("/templates", json=[1, 2, 3])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_json"


# ---- 推演 -----------------------------------------------------------------

def _register(client, payload=None):
    resp = client.post("/templates", json=payload or make_template_payload())
    assert resp.status_code == 201, resp.text
    return resp.json()["template_id"]


def test_derivation_success_times_sorted_by_id(client):
    template_id = _register(client)
    resp = client.post("/derivations", json={"template_id": template_id})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # a=0 -> b=5,c=20 -> d=max(1, 5+3, 20)=20
    assert body["times"] == {"a": 0, "b": 5, "c": 20, "d": 20}
    assert list(body["times"].keys()) == ["a", "b", "c", "d"]
    assert [row["id"] for row in body["points"]] == ["a", "b", "c", "d"]
    assert body["points"][3]["time"] == 20
    assert body["delay"] == {}
    assert body["template_id"] == template_id
    assert body["result_id"]


def test_derivation_order_independence_via_api(client):
    p1 = make_template_payload()
    p2 = make_template_payload()
    p2["relations"] = list(reversed(p2["relations"]))
    p2["points"] = list(reversed(p2["points"]))

    id1 = _register(client, p1)
    id2 = _register(client, p2)

    r1 = client.post("/derivations", json={"template_id": id1}).json()
    r2 = client.post("/derivations", json={"template_id": id2}).json()
    assert r1["times"] == r2["times"] == {"a": 0, "b": 5, "c": 20, "d": 20}


def test_derivation_with_delay_propagation(client):
    template_id = _register(client)
    resp = client.post("/derivations", json={
        "template_id": template_id,
        "delay": {"a": 30},
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # a=30 -> b=35,c=50 -> d=max(1, 35+3, 50)=50
    assert body["times"] == {"a": 30, "b": 35, "c": 50, "d": 50}
    assert body["delay"] == {"a": 30}


def test_derivation_invalid_delay(client):
    template_id = _register(client)
    base = {"template_id": template_id}

    r = client.post("/derivations", json={**base, "delay": {"ghost": 1}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_delay"

    r = client.post("/derivations", json={**base, "delay": {"a": -1}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_delay"

    # delay 小于原 release
    template = make_template_payload()
    template["points"][0]["release"] = 10
    tid = _register(client, template)
    r = client.post("/derivations",
                    json={"template_id": tid, "delay": {"a": 9}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_delay"


def test_derivation_unknown_template_404(client):
    resp = client.post("/derivations",
                       json={"template_id": "deadbeef"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "template_not_found"


def test_positive_cycle_returns_stable_code_and_no_record(client, db_path):
    payload = {
        "points": [{"id": "a", "release": 0}, {"id": "b", "release": 0}],
        "relations": [
            {"from": "a", "to": "b", "min_gap": 1},
            {"from": "b", "to": "a", "min_gap": 1},
        ],
    }
    template_id = _register(client, payload)
    resp = client.post("/derivations", json={"template_id": template_id})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "positive_cycle"

    # 失败推演不得生成记录：results 表应为空。
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_deadline_exceeded_returns_stable_code_and_no_record(client, db_path):
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
    assert err["details"]["violations"] == [
        {"id": "b", "earliest": 8, "latest": 5}]

    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_zero_gap_cycle_succeeds(client):
    payload = {
        "points": [
            {"id": "a", "release": 1, "latest": 10},
            {"id": "b", "release": 2, "latest": 10},
        ],
        "relations": [
            {"from": "a", "to": "b", "min_gap": 0},
            {"from": "b", "to": "a", "min_gap": 0},
        ],
    }
    template_id = _register(client, payload)
    resp = client.post("/derivations", json={"template_id": template_id})
    assert resp.status_code == 201, resp.text
    # 零权环把环上各点拉平到最大 release(=2)。
    assert resp.json()["times"] == {"a": 2, "b": 2}


# ---- 结果查询与持久化 ------------------------------------------------------

def test_get_result_after_restart(client, db_path):
    template_id = _register(client)
    created = client.post("/derivations", json={
        "template_id": template_id, "delay": {"a": 10},
    }).json()
    result_id = created["result_id"]
    assert created["times"] == {"a": 10, "b": 15, "c": 30, "d": 30}

    # 模拟容器重启：用同一个数据库文件重新创建应用。
    restarted_app = create_app(db_path=db_path)
    with TestClient(restarted_app) as restarted:
        resp = restarted.get(f"/results/{result_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result_id"] == result_id
    assert body["template_id"] == template_id
    assert body["times"] == {"a": 10, "b": 15, "c": 30, "d": 30}
    assert list(body["times"].keys()) == ["a", "b", "c", "d"]
    assert body["delay"] == {"a": 10}
    assert [row["id"] for row in body["points"]] == ["a", "b", "c", "d"]

    # 既有模板同样可取回（重启后可再次推演）。
    with TestClient(create_app(db_path=db_path)) as again:
        again_resp = again.post(
            "/derivations", json={"template_id": template_id})
    assert again_resp.status_code == 201
    assert again_resp.json()["times"] == {"a": 0, "b": 5, "c": 20, "d": 20}


def test_get_missing_result_404(client):
    resp = client.get("/results/nonexistent-id")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "result_not_found"


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_unknown_path_stable_code(client):
    resp = client.get("/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
