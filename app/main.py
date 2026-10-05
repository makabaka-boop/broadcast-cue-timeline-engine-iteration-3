"""FastAPI 入口：模板登记、延误推演、结果查询。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool

from . import db
from .errors import (AppError, DEADLINE_EXCEEDED, INVALID_REQUEST,
                     POSITIVE_CYCLE, RESULT_INCONSISTENT, RESULT_NOT_FOUND,
                     TEMPLATE_NOT_FOUND, register_exception_handlers)
from .scheduler import (STATUS_DEADLINE_EXCEEDED, STATUS_OK,
                        STATUS_POSITIVE_CYCLE, earliest_schedule,
                        explain_schedule, verify_result_explanations)
from .validation import validate_delay, validate_template


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(
        title="直播提示点播控服务",
        version="1.0.0",
        description="登记合法提示点模板、推演逐点最早时刻并持久化成功结果。",
    )
    app.state.db_path = db_path
    register_exception_handlers(app)
    db.init_db(db_path)

    def _db_path(request: Request) -> str | None:
        return request.app.state.db_path

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/templates", status_code=201)
    async def register_template(request: Request):
        payload = await _load_json_object(request)
        template = validate_template(payload)
        # created_at 由持久层生成并显式写列；响应只回传同一字符串，
        # 保证 POST 与后续 GET 逐字一致。所有 SQLite 调用走线程池，避免
        # busy_timeout 等待阻塞事件循环，并让并发请求真正争用同一写锁。
        template_id, created_at = await run_in_threadpool(
            db.insert_template, template, _db_path(request))
        return {
            "template_id": template_id,
            "created_at": created_at,
            "point_count": len(template["points"]),
            "relation_count": len(template["relations"]),
            "points": template["points"],
            "relations": template["relations"],
        }

    @app.post("/derivations", status_code=201)
    async def run_derivation(request: Request):
        payload = await _load_json_object(request)

        template_id = payload.get("template_id")
        if not isinstance(template_id, str) or not template_id:
            raise AppError(
                "invalid_delay",
                "Field 'template_id' is required and must be a non-empty string.",
                status_code=400, details={"path": "template_id"})

        template = await run_in_threadpool(
            db.get_template, template_id, _db_path(request))
        if template is None:
            raise AppError(
                TEMPLATE_NOT_FOUND,
                f"Template {template_id!r} does not exist.",
                status_code=404, details={"template_id": template_id})

        delay = validate_delay(payload, template)
        # explain 已在 validate_delay 中校验为布尔（缺省 False）。
        want_explain = payload.get("explain", False)

        if want_explain:
            outcome = explain_schedule(template["points"],
                                       template["relations"], delay)
        else:
            outcome = earliest_schedule(template["points"],
                                        template["relations"], delay)

        if outcome["status"] == STATUS_POSITIVE_CYCLE:
            # 正权环永远优先报原错误：不落库、不附解释、不触碰既有数据。
            raise AppError(
                POSITIVE_CYCLE,
                "The rules contain a directed cycle with positive total "
                "min_gap; no finite earliest schedule exists.",
                status_code=422)

        if outcome["status"] == STATUS_DEADLINE_EXCEEDED:
            details = {"violations": outcome["violations"]}
            if want_explain:
                # ID 最小的违约点及其证据链；失败推演同样不落库。
                details["explanation"] = outcome["explanation"]
            raise AppError(
                DEADLINE_EXCEEDED,
                "At least one point's earliest time exceeds its latest bound.",
                status_code=422,
                details=details)

        assert outcome["status"] == STATUS_OK
        times = outcome["times"]
        result_id, created_at = await run_in_threadpool(
            db.insert_result, template_id, delay, times, _db_path(request))
        body = {
            "result_id": result_id,
            "template_id": template_id,
            "created_at": created_at,
            "delay": _sorted_dict(delay),
            "times": _sorted_times(template["points"], times),
            "points": _schedule_points(template["points"], times),
        }
        if want_explain:
            # 解释只随响应给出，不落库；读取时按冻结模板 + delay 重算。
            body["explanations"] = outcome["explanations"]
        return body

    @app.get("/results/{result_id}")
    async def get_result(result_id: str, request: Request):
        want_explain = _parse_explain_query(request)
        record = await run_in_threadpool(
            db.get_result, result_id, _db_path(request))
        if record is None:
            raise AppError(
                RESULT_NOT_FOUND,
                f"Result {result_id!r} does not exist.",
                status_code=404, details={"result_id": result_id})

        template = await run_in_threadpool(
            db.get_template, record["template_id"], _db_path(request))
        # results.template_id 有外键约束，模板必然存在。
        points = template["points"]
        body = {
            "result_id": record["id"],
            "template_id": record["template_id"],
            "created_at": record["created_at"],
            "delay": _sorted_dict(record["delay"]),
            "times": _sorted_times(points, record["times"]),
            "points": _schedule_points(points, record["times"]),
        }
        if want_explain:
            # 解释不落库：由冻结模板 + delay 重算，并与保存的结果逐点核对；
            # 不一致时明确报错，记录保持原样、绝不改写。
            check = verify_result_explanations(
                points, template["relations"], record["delay"],
                record["times"])
            if check["status"] != STATUS_OK:
                raise AppError(
                    RESULT_INCONSISTENT,
                    "The stored result is inconsistent with its template "
                    "and delay; the record was left unchanged.",
                    status_code=500, details={"result_id": result_id})
            body["explanations"] = check["explanations"]
        return body

    return app


def _parse_explain_query(request: Request) -> bool:
    """解析 ``?explain=`` 查询参数：仅接受 true/false，其余明确报错。"""
    raw = request.query_params.get("explain")
    if raw is None:
        return False
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise AppError(
        INVALID_REQUEST,
        "Query parameter 'explain' must be 'true' or 'false'.",
        status_code=400, details={"param": "explain", "value": raw})


async def _load_json_object(request: Request) -> dict:
    """读取 JSON 请求体；语法错误或非对象统一报 bad_json。"""
    try:
        payload = await request.json()
    except Exception:  # JSONDecodeError 等
        raise AppError(
            "bad_json",
            "Request body is not valid JSON.",
            status_code=400)
    if not isinstance(payload, dict):
        raise AppError(
            "bad_json",
            "Request body must be a JSON object.",
            status_code=400)
    return payload


def _sorted_dict(values: dict) -> dict:
    return {key: values[key] for key in sorted(values)}


def _sorted_times(points, times: dict) -> dict:
    """按提示点 ID 升序输出 {id: 最早时刻}。

    落库模板的 points 已按 ID 升序规范化；模板缺失（理论上不会发生，结果有
    外键约束）时退化为按 times 自身键排序。
    """
    ordered_ids = [p["id"] for p in points] if points else sorted(times)
    return {pid: times[pid] for pid in ordered_ids}


def _schedule_points(points, times: dict) -> list:
    """按提示点 ID 升序输出明细行。"""
    rows = []
    for point in points:
        pid = point["id"]
        rows.append({
            "id": pid,
            "release": point["release"],
            "latest": point["latest"],
            "time": times[pid],
        })
    return rows


app = create_app()
