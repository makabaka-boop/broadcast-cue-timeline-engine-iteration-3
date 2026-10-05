"""SQLite 持久层：合法模板与成功推演结果。

数据库文件位于挂载卷内（默认 ``/data/app.db``，可用环境变量
``BROADCAST_DB_PATH`` 覆盖）。每次操作使用短连接，开启外键约束。

只有两种写操作：

* 登记一个**已经通过校验**的模板；
* 保存一次**已经成功**的推演结果。

非法模板与失败推演根本不会进入本模块，因此失败请求既不会产生记录，
也不会改动既有数据。

并发语义（短暂写争用必须得到确定结果）：

* WAL 是文件级持久属性，**只在** :func:`init_db` 里切换一次。切换 journal
  mode 需要排他锁，而 SQLite 的 ``PRAGMA journal_mode`` **不接受 busy
  handler**——锁被占用时会立即抛 ``database is locked``，即使连接设置了
  busy_timeout 也无济于事。因此初始化用进程内锁 + ``fcntl`` 文件锁串行化，
  并在有界次数内重试，应用启动时多个连接/进程同时初始化也不会放大成 500。
* 工作连接（含只读连接）**绝不再切换 journal mode**，只设置外键与
  busy_timeout；journal mode 由初始化一次性保证。
* 所有写入都在 ``BEGIN IMMEDIATE`` 事务内完成：建事务时即获取 RESERVED
  写锁，多个写者由 SQLite 的 busy handler 在
  ``BROADCAST_DB_BUSY_TIMEOUT``（默认 5 秒）内有序等待，锁释放后等待者
  直接成功。超时仍拿不到锁时，:class:`AppError` ``service_unavailable``
  （HTTP 503）作为稳定、可识别、可重试的响应返回，事务回滚、不留半成品，
  也绝不把 SQLite 内部错误文本暴露给客户端。
* ``created_at`` 由应用生成一次并显式写列，插入接口把同一字符串原样返回；
  POST 响应、列表/详情读取到的是同一列里的同一值，天然逐字一致。列上的
  ``DEFAULT (strftime(...))`` 仅为兼容既有数据库文件而保留。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

from .errors import AppError, SERVICE_UNAVAILABLE

DEFAULT_DB_PATH = "/data/app.db"
LOCAL_FALLBACK_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "local-data", "app.db")

# WAL 初始化（journal mode 切换）时的有界重试参数。
_INIT_ATTEMPTS = 10
_INIT_RETRY_INTERVAL = 0.05

_SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id          TEXT PRIMARY KEY,
    body_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS results (
    id           TEXT PRIMARY KEY,
    template_id  TEXT NOT NULL,
    delay_json   TEXT NOT NULL,
    times_json   TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (template_id) REFERENCES templates(id)
);

CREATE INDEX IF NOT EXISTS idx_results_template_id
    ON results(template_id);
"""

# 进程内 init 串行化：同一进程内多个连接（如测试、多 worker）同时初始化时，
# fcntl 对同进程的不同文件描述符不互斥，必须再配一把线程锁。
_init_locks_guard = threading.Lock()
_init_locks: dict[str, threading.Lock] = {}


def resolve_db_path() -> str:
    configured = os.environ.get("BROADCAST_DB_PATH")
    if configured:
        return configured
    # 容器内 /data 由 Compose 挂载且可写；本地直接运行（无挂载、无权限）时
    # 回退到仓库内的 local-data 目录（已被 .gitignore 排除）。
    data_dir = os.path.dirname(DEFAULT_DB_PATH)
    if os.path.isdir(data_dir) and os.access(data_dir, os.W_OK):
        return DEFAULT_DB_PATH
    return LOCAL_FALLBACK_DB_PATH


def busy_timeout_ms() -> int:
    """工作连接的 busy_timeout（毫秒），可用 BROADCAST_DB_BUSY_TIMEOUT 覆盖。"""
    raw = os.environ.get("BROADCAST_DB_BUSY_TIMEOUT")
    if raw is None:
        return 5000
    try:
        return max(0, int(float(raw) * 1000))
    except ValueError:
        return 5000


def utc_now() -> str:
    """UTC 当前时刻，毫秒精度、``...Z`` 形式。

    同一记录的创建时间只生成这一次：显式写列并在响应中原样返回，因此
    POST 与后续 GET 读到的 ``created_at`` 逐字相同。
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def _connect(db_path: str | None = None, *, set_wal: bool = False
             ) -> sqlite3.Connection:
    """打开一个短连接。

    autocommit 模式（``isolation_level=None``）让事务边界完全显式：写入统一
    用 ``BEGIN IMMEDIATE``，避免隐式延迟事务在读过之后才尝试加锁。

    :param set_wal: 仅初始化时传 True。工作连接不允许在此切换 journal
        mode——该 PRAGMA 不接受 busy handler，在写锁占用时会立即失败。
    """
    path = db_path or resolve_db_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=busy_timeout_ms() / 1000.0,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    if set_wal:
        # 初始化通道：配合 init_db 的文件锁与有界重试。
        conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms()};")
    return conn


@contextlib.contextmanager
def _process_init_lock(db_path: str):
    """同一数据库文件的跨线程 + 跨进程初始化锁。"""
    real_path = os.path.abspath(db_path)
    with _init_locks_guard:
        thread_lock = _init_locks.setdefault(real_path, threading.Lock())
    os.makedirs(os.path.dirname(real_path), exist_ok=True)
    with thread_lock:
        # 文件锁覆盖其它进程；单独的 .init.lock 文件避免与 DB/WAL 文件冲突。
        lock_fd = os.open(real_path + ".init.lock",
                          os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def init_db(db_path: str | None = None) -> None:
    """创建表结构并确保 WAL 模式（幂等）。

    并发安全：journal mode 切换需要排他锁且不受 busy_timeout 保护，因此
    整个初始化在跨线程/跨进程锁内执行，并对 ``database is locked`` 做
    **有界**重试。最终仍失败时抛 :class:`AppError` ``service_unavailable``，
    不泄漏 SQLite 内部错误。
    """
    path = db_path or resolve_db_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with _process_init_lock(path):
        last_error: Exception | None = None
        for attempt in range(_INIT_ATTEMPTS):
            conn = None
            try:
                conn = _connect(path, set_wal=True)
                conn.executescript(_SCHEMA)
                return
            except sqlite3.OperationalError as exc:
                last_error = exc
                if not _is_busy_error(exc) or attempt == _INIT_ATTEMPTS - 1:
                    break
                time.sleep(_INIT_RETRY_INTERVAL)
            finally:
                if conn is not None:
                    conn.close()
    raise AppError(
        SERVICE_UNAVAILABLE,
        "The database is temporarily unavailable; please retry shortly.",
        status_code=503,
        details={"retryable": True},
        headers={"Retry-After": "1"},
    ) from last_error


def new_id() -> str:
    return uuid.uuid4().hex


def _is_busy_error(exc: sqlite3.Error) -> bool:
    return "locked" in str(exc).lower() or "busy" in str(exc).lower()


def _write(path: str, sql: str, params: tuple) -> None:
    """在 BEGIN IMMEDIATE 事务内执行单条写入并提交。

    争用语义：建事务时即申请写锁，SQLite busy handler 会在 busy_timeout 内
    等待持锁者提交。超时则回滚（本来也没有任何写入生效）并映射为稳定的
    503 ``service_unavailable``，不暴露内部错误文本。
    """
    conn = _connect(path)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(sql, params)
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            if _is_busy_error(exc):
                raise AppError(
                    SERVICE_UNAVAILABLE,
                    "The database is temporarily unavailable due to "
                    "concurrent writes; please retry shortly.",
                    status_code=503,
                    details={"retryable": True},
                    headers={"Retry-After": "1"},
                ) from exc
            # 非锁类底层错误也统一包装，不向外泄漏 SQLite 内部信息。
            raise AppError(
                "internal_error",
                "The request could not be persisted due to a storage error.",
                status_code=500,
            ) from exc
    finally:
        conn.close()


def insert_template(template: dict, db_path: str | None = None
                    ) -> tuple[str, str]:
    """插入已校验模板，返回 (模板 ID, 创建时间)。

    创建时间在拿写锁前一刻生成一次并显式写列；返回值与列值为同一字符串。
    """
    path = db_path or resolve_db_path()
    template_id = new_id()
    created_at = utc_now()
    _write(
        path,
        "INSERT INTO templates (id, body_json, created_at) "
        "VALUES (?, ?, ?)",
        (template_id,
         json.dumps(template, ensure_ascii=False, sort_keys=True),
         created_at),
    )
    return template_id, created_at


def get_template(template_id: str, db_path: str | None = None):
    path = db_path or resolve_db_path()
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT body_json FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return json.loads(row["body_json"])


def insert_result(template_id: str, delay: dict, times: dict,
                  db_path: str | None = None) -> tuple[str, str]:
    """插入一次成功推演的结果，返回 (结果 ID, 创建时间)。

    创建时间在拿写锁前一刻生成一次并显式写列；返回值与列值为同一字符串。
    """
    path = db_path or resolve_db_path()
    result_id = new_id()
    created_at = utc_now()
    _write(
        path,
        "INSERT INTO results (id, template_id, delay_json, times_json, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        (result_id, template_id,
         json.dumps(delay, ensure_ascii=False, sort_keys=True),
         json.dumps(times, ensure_ascii=False, sort_keys=True),
         created_at),
    )
    return result_id, created_at


def get_result(result_id: str, db_path: str | None = None):
    path = db_path or resolve_db_path()
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT template_id, delay_json, times_json, created_at "
            "FROM results WHERE id = ?",
            (result_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "id": result_id,
        "template_id": row["template_id"],
        "delay": json.loads(row["delay_json"]),
        "times": json.loads(row["times_json"]),
        "created_at": row["created_at"],
    }
