"""稳定错误码与统一错误响应。

所有失败响应均为 ``{"error": {"code": <稳定代码>, "message": <人类可读说明>,
"details": <可选补充>}}``，代码字符串即接口契约的一部分，不随实现改动。

任何底层存储错误（含 SQLite 写锁超时）都必须翻译成这里定义的稳定代码，
绝不把 ``database is locked`` 之类的内部错误文本或 500 暴露给客户端：
写争用在有界等待后仍失败时，统一为可识别、可重试的 503
``service_unavailable``（带 ``Retry-After`` 头）。
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("broadcast_cue_api")


class AppError(Exception):
    """业务层抛出的、带稳定代码的错误。"""

    def __init__(self, code: str, message: str, status_code: int = 400,
                 details=None, headers=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        self.headers = headers

    def to_response(self) -> JSONResponse:
        body = {"error": {"code": self.code, "message": self.message}}
        if self.details is not None:
            body["error"]["details"] = self.details
        return JSONResponse(status_code=self.status_code, content=body,
                            headers=self.headers)


# ---- 稳定错误代码 ---------------------------------------------------------

INVALID_TEMPLATE = "invalid_template"        # 模板不满足登记规则，HTTP 400
INVALID_DELAY = "invalid_delay"              # delay 覆盖不合法，HTTP 400
TEMPLATE_NOT_FOUND = "template_not_found"    # 模板 ID 不存在，HTTP 404
RESULT_NOT_FOUND = "result_not_found"        # 结果 ID 不存在，HTTP 404
POSITIVE_CYCLE = "positive_cycle"            # 存在正权环，HTTP 422
DEADLINE_EXCEEDED = "deadline_exceeded"      # 最早时刻超过 latest，HTTP 422
BAD_JSON = "bad_json"                        # 请求体不是合法 JSON，HTTP 400
NOT_FOUND = "not_found"                      # 路径不存在，HTTP 404
METHOD_NOT_ALLOWED = "method_not_allowed"    # HTTP 方法不允许，HTTP 405
SERVICE_UNAVAILABLE = "service_unavailable"  # 短暂写争用等待超时，HTTP 503
INVALID_REQUEST = "invalid_request"          # 请求参数不合法（如 explain 取值），HTTP 400
RESULT_INCONSISTENT = "result_inconsistent"  # 保存结果与冻结模板+delay 重算不一致，HTTP 500


def register_exception_handlers(app) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return exc.to_response()

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error(_: Request,
                                        exc: RequestValidationError):
        # FastAPI 自身的解析/校验失败（如请求体根本不是 JSON 类型）统一归入
        # bad_json，保证对外代码稳定。
        return AppError(
            BAD_JSON, "Request body must be valid JSON of the documented shape.",
            status_code=400, details=exc.errors(),
        ).to_response()

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return AppError(NOT_FOUND, "Resource not found.",
                            status_code=404).to_response()
        if exc.status_code == 405:
            return AppError(METHOD_NOT_ALLOWED,
                            "HTTP method not allowed for this path.",
                            status_code=405).to_response()
        return AppError("http_error", str(exc.detail),
                        status_code=exc.status_code).to_response()

    @app.exception_handler(Exception)
    async def _unhandled_error(request: Request, exc: Exception):
        # 兜底：任何未被翻译成稳定代码的异常都只返回统一结构，记录细节到
        # 服务端日志，不向外泄漏内部实现信息。
        logger.exception("Unhandled error while handling %s %s",
                         request.method, request.url.path)
        return AppError(
            "internal_error",
            "The server encountered an unexpected error.",
            status_code=500,
        ).to_response()
