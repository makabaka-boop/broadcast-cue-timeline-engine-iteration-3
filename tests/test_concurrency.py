"""并发验收：真实 SQLite + 同步屏障下的模板登记与结果生成。

覆盖三类争用：

* **连接初始化争用**：多个线程/多个 app 实例同时 init（WAL 切换）必须全部
  成功，不得把本可恢复的争用放大为 500；
* **锁释放前成功**：外部连接持有写锁时，多个登记/生成请求在 busy_timeout
  内等待，锁释放后全部 201，且每个成功响应恰好对应一条完整记录；
* **等待超时失败**：busy_timeout 耗尽后请求必须得到稳定结构的 503
  ``service_unavailable``，不暴露 SQLite 内部错误，不留任何半成品。

并对每个成功 ID 核对：POST 与重复 GET 的完整内容、created_at 逐字一致，
以及 created_at 排序稳定、可重复。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from tests.conftest import make_template_payload

CREATED_AT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)


@pytest.fixture()
def client(db_path):
    app = create_app(db_path=db_path)
    with TestClient(app) as test_client:
        yield test_client


def _raw_connect(db_path, hold_lock=False):
    """外部连接：不切换 WAL（模拟同一文件的其它连接），可选直接持写锁。"""
    conn = sqlite3.connect(db_path, timeout=0)
    if hold_lock:
        conn.execute("BEGIN IMMEDIATE")
    return conn


def _row_counts(db_path):
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        templates = conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
        results = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    finally:
        conn.close()
    return templates, results


def _register_template(client, payload):
    resp = client.post("/templates", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---- 连接初始化争用 --------------------------------------------------------

def test_concurrent_init_db_all_succeed(db_path):
    """多线程同时 init（含 WAL 切换 + 建表）：全部成功，无 500/locked。"""
    n = 10
    barrier = Barrier(n)
    errors: list[Exception] = []

    def init():
        barrier.wait()
        try:
            db.init_db(db_path)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=init) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == []

    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    finally:
        conn.close()
    assert mode == "wal"


def test_concurrent_app_startup_on_same_file(db_path):
    """模拟启动时多个 worker/连接同时对同一文件执行 create_app。"""
    n = 6
    barrier = Barrier(n)
    apps: list = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def startup():
        barrier.wait()
        try:
            app = create_app(db_path=db_path)
            with lock:
                apps.append(app)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=startup) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert errors == []
    assert len(apps) == n

    # 初始化完成后每个实例都能独立完成一次写。
    for app in apps:
        with TestClient(app) as c:
            resp = c.post("/templates",
                          json={"points": [{"id": "x", "release": 0}]})
            assert resp.status_code == 201, resp.text
    assert _row_counts(db_path)[0] == n


def test_init_retries_when_initial_wal_switch_contended(db_path, monkeypatch):
    """init 切换 WAL 的那一刻锁被占用：有界重试后成功（不再放大为 500）。"""
    db.init_db(db_path)  # 先建好库与 WAL
    holder = _raw_connect(db_path, hold_lock=True)
    done = threading.Event()
    errors: list[Exception] = []

    def reinit():
        try:
            db.init_db(db_path)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            done.set()

    t = threading.Thread(target=reinit)
    t.start()
    # 确保 reinit 已进入并撞上锁，再释放。
    time.sleep(0.2)
    holder.rollback()
    holder.close()
    assert done.wait(5.0)
    t.join(5)
    assert errors == []


# ---- 模板登记：锁释放前等待、全部成功、恰好一条记录 ------------------------

def test_concurrent_template_registration_wait_and_succeed(client, db_path):
    n = 8
    barrier = Barrier(n)

    def register(i):
        payload = {"points": [{"id": f"p{i}", "release": i}]}
        barrier.wait()
        return client.post("/templates", json=payload)

    with ThreadPoolExecutor(max_workers=n) as pool:
        responses = list(pool.map(register, range(n)))

    posts = [r.json() for r in responses]
    assert all(r.status_code == 201 for r in responses), \
        [r.status_code for r in responses]
    ids = [p["template_id"] for p in posts]
    assert len(ids) == n and len(set(ids)) == n  # 无重复模板
    for p in posts:
        assert CREATED_AT_RE.match(p["created_at"])

    # 每个成功响应恰好对应一条完整记录，且落库内容与响应逐字段一致。
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, body_json, created_at FROM templates").fetchall()
    finally:
        conn.close()
    assert len(rows) == n
    by_id = {row["id"]: row for row in rows}
    assert set(by_id) == set(ids)
    for post in posts:
        row = by_id[post["template_id"]]
        assert row["created_at"] == post["created_at"]  # 响应与落库逐字一致
        body = json.loads(row["body_json"])
        assert body["points"] == post["points"]
        assert body["relations"] == post["relations"]


def test_template_registration_waits_behind_external_lock(client, db_path):
    """外部连接持锁 0.5s：请求在 busy_timeout 内等待，锁释放后成功。"""
    holder = _raw_connect(db_path, hold_lock=True)
    result = {}

    def register():
        result["resp"] = client.post(
            "/templates", json={"points": [{"id": "w", "release": 0}]})

    t = threading.Thread(target=register)
    t.start()
    time.sleep(0.5)
    holder.rollback()
    holder.close()
    t.join(10)
    assert result["resp"].status_code == 201, result["resp"].text
    assert _row_counts(db_path)[0] == 1


# ---- 结果生成：并发、无重复、POST/GET 完整内容一致 -------------------------

@pytest.fixture()
def template_id(client):
    return _register_template(client, make_template_payload())["template_id"]


def test_concurrent_derivations_wait_and_complete_content_consistent(
        client, db_path, template_id):
    n = 8
    barrier = Barrier(n)

    def derive(i):
        barrier.wait()
        return client.post("/derivations",
                           json={"template_id": template_id,
                                 "delay": {"a": 2 * i}})

    with ThreadPoolExecutor(max_workers=n) as pool:
        responses = list(pool.map(derive, range(n)))

    posts = [r.json() for r in responses]
    assert all(r.status_code == 201 for r in responses), \
        [r.status_code for r in responses]
    result_ids = [p["result_id"] for p in posts]
    assert len(set(result_ids)) == n  # 重试也不产生难以区分的重复结果

    # 行数：每个成功响应恰好一条完整结果记录。
    templates_n, results_n = _row_counts(db_path)
    assert templates_n == 1
    assert results_n == n

    for post in posts:
        assert CREATED_AT_RE.match(post["created_at"])
        rid = post["result_id"]
        # 重复 GET：每次完整内容一致，且与 POST 逐字一致。
        gets = [client.get(f"/results/{rid}").json() for _ in range(3)]
        for got in gets:
            assert got == post  # 含 created_at 与全部字段逐字一致
        statuses = [client.get(f"/results/{rid}").status_code for _ in range(3)]
        assert statuses == [200, 200, 200]

        # 落库列值与响应/GET 逐字一致。
        conn = sqlite3.connect(db_path)
        try:
            stored_at = conn.execute(
                "SELECT created_at FROM results WHERE id = ?", (rid,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert stored_at == post["created_at"]


def test_mixed_registrations_and_derivations_behind_lock(
        client, db_path, template_id):
    """持锁期间混合登记 + 生成同时到达：全部等待，释放后全部成功。"""
    n = 6
    barrier = Barrier(n)

    def mixed(i):
        barrier.wait()
        if i % 2 == 0:
            return ("template", client.post(
                "/templates", json={"points": [{"id": f"m{i}", "release": 0}]}))
        return ("derivation", client.post(
            "/derivations", json={"template_id": template_id}))

    holder = _raw_connect(db_path, hold_lock=True)
    outcomes = []
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = []
        for i in range(n):
            futures.append(pool.submit(mixed, i))
        time.sleep(0.5)  # 确保所有请求都已在等待锁
        holder.rollback()
        holder.close()
        outcomes = [f.result(timeout=10) for f in futures]

    assert all(r.status_code == 201 for _, r in outcomes), \
        [(kind, r.status_code) for kind, r in outcomes]
    templates_n, results_n = _row_counts(db_path)
    assert templates_n == 1 + n // 2
    assert results_n == n - n // 2


def test_created_at_ordering_stable_and_repeatable(client, db_path, template_id):
    """created_at 单调且排序确定可重复；同毫秒并列时按 id 破平。"""
    barrier = Barrier(6)

    def derive(_):
        barrier.wait()
        return client.post("/derivations",
                           json={"template_id": template_id}).json()

    with ThreadPoolExecutor(max_workers=6) as pool:
        posts = list(pool.map(derive, range(6)))

    def ordered():
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT id, created_at FROM results "
                "ORDER BY created_at ASC, id ASC").fetchall()
        finally:
            conn.close()

    first = ordered()
    time.sleep(0.01)
    second = ordered()
    assert first == second  # 排序确定、可重复。

    by_id = {p["result_id"]: p["created_at"] for p in posts}
    expected_order = sorted(by_id, key=lambda rid: (by_id[rid], rid))
    assert [row[0] for row in first] == expected_order
    # created_at 本身单调（允许同毫秒并列，故用 <=）。
    stamps = [row[1] for row in first]
    assert stamps == sorted(stamps)


# ---- 等待超时：稳定 503、不泄漏内部错误、不留半成品 ------------------------

@pytest.fixture()
def short_timeout(monkeypatch):
    monkeypatch.setenv("BROADCAST_DB_BUSY_TIMEOUT", "0.3")
    yield


def test_derivation_write_timeout_stable_503(db_path, short_timeout):
    """busy_timeout 耗尽：503 + 统一错误结构 + Retry-After，无半成品。"""
    # 在短超时配置下建库/登记模板。
    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        ok = client.post(
            "/templates", json=make_template_payload())
        assert ok.status_code == 201, ok.text
        template_id = ok.json()["template_id"]

        holder = _raw_connect(db_path, hold_lock=True)
        try:
            resp = client.post(
                "/derivations", json={"template_id": template_id})
        finally:
            holder.rollback()
            holder.close()

    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert set(body) == {"error"}
    err = body["error"]
    assert err["code"] == "service_unavailable"
    assert isinstance(err["message"], str) and err["message"]
    assert err["details"] == {"retryable": True}
    # 绝不暴露 SQLite 内部错误文本。
    assert "locked" not in resp.text.lower()
    assert "sqlite" not in resp.text.lower()
    assert resp.headers.get("retry-after") == "1"

    # 失败请求不留半成品：结果表仍为空。
    assert _row_counts(db_path) == (1, 0)


def test_concurrent_writers_all_timeout_then_recovery(
        client, db_path, short_timeout, template_id):
    n = 6
    barrier = Barrier(n)
    holder = _raw_connect(db_path, hold_lock=True)

    def derive(_):
        barrier.wait()
        return client.post("/derivations",
                           json={"template_id": template_id})

    try:
        with ThreadPoolExecutor(max_workers=n) as pool:
            responses = list(pool.map(derive, range(n)))
    finally:
        holder.rollback()
        holder.close()

    assert all(r.status_code == 503 for r in responses), \
        [r.status_code for r in responses]
    for r in responses:
        err = r.json()["error"]
        assert err["code"] == "service_unavailable"
        assert "locked" not in r.text.lower()
        assert "sqlite" not in r.text.lower()
        assert r.headers.get("retry-after") == "1"
    assert _row_counts(db_path) == (1, 0)  # 无半成品

    # 锁释放、恢复后新的请求立即可成功——503 是暂态而非损坏。
    recovered = client.post("/derivations",
                            json={"template_id": template_id})
    assert recovered.status_code == 201, recovered.text
    assert _row_counts(db_path) == (1, 1)


def test_barrier_sync_proves_real_concurrency(client, db_path):
    """同步屏障保证所有线程在同一刻发起：无串行化取巧时仍全部成功。"""
    n = 10
    barrier = Barrier(n, timeout=10)

    def register(i):
        try:
            barrier.wait()
        except BrokenBarrierError:
            pytest.fail("workers did not reach the barrier together")
        return client.post("/templates",
                           json={"points": [{"id": f"s{i}", "release": 0}]})

    with ThreadPoolExecutor(max_workers=n) as pool:
        responses = list(pool.map(register, range(n)))
    assert all(r.status_code == 201 for r in responses)
    assert _row_counts(db_path)[0] == n


# ---- 与既有数据库文件兼容 --------------------------------------------------

def test_existing_rollback_journal_file_is_migrated_and_data_kept(db_path):
    """旧文件（DELETE 日志、含旧记录）经 init 后切 WAL，数据完整、新行时间一致。"""
    # 手工建立一个 rollback-journal 旧库，含旧行（created_at 用列默认值）。
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute(
        "CREATE TABLE templates ("
        "id TEXT PRIMARY KEY, body_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT "
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))")
    conn.execute(
        "INSERT INTO templates (id, body_json) VALUES (?, ?)",
        ("legacy-id", json.dumps({"points": [{"id": "a", "release": 0,
                                              "latest": None}],
                                  "relations": []})))
    conn.commit()
    conn.close()

    # init 必须安全切到 WAL 且不丢数据。
    db.init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        legacy = conn.execute(
            "SELECT body_json FROM templates WHERE id = 'legacy-id'").fetchone()
        assert legacy is not None
    finally:
        conn.close()

    # 应用在迁移后的文件上正常工作，新时间戳仍逐字一致。
    app = create_app(db_path=db_path)
    with TestClient(app) as c:
        resp = c.post("/templates",
                      json={"points": [{"id": "n", "release": 0}]})
        assert resp.status_code == 201, resp.text
        post = resp.json()
        conn = sqlite3.connect(db_path)
        try:
            stored = conn.execute(
                "SELECT created_at FROM templates WHERE id = ?",
                (post["template_id"],)).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
        finally:
            conn.close()
        assert stored == post["created_at"]
        assert total == 2  # 旧行保留 + 新行
