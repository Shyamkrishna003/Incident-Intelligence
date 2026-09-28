"""Domain errors and their mapping to a consistent HTTP error envelope.

Every error response has the shape ``{"error": {"code", "message", "request_id", ...}}``.
"""

from typing import Any, cast

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = structlog.get_logger(__name__)


class AppError(Exception):
    status_code = 500
    code = "internal_error"
    default_message = "Internal server error."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class InvalidInputError(AppError):
    status_code = 422
    code = "invalid_input"
    default_message = "The request is invalid."


class AuthenticationError(AppError):
    """Credentials are missing or invalid. The public message is deliberately generic."""

    status_code = 401
    code = "unauthenticated"
    default_message = "Missing or invalid credentials."

    def __init__(self, reason: str) -> None:
        # `reason` is for logs only; it is never returned to the client.
        self.reason = reason
        super().__init__()


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    default_message = "Resource not found."


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    default_message = "The resource conflicts with existing state."


_HTTP_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    429: "rate_limited",
}


def _request_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get("request_id")
    return value if isinstance(value, str) else None


def error_body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "request_id": _request_id(), **extra}}


# Starlette types every handler as (Request, Exception); each is registered for exactly one
# exception class below, so the casts only restate that registration.


async def _handle_app_error(_request: Request, raised: Exception) -> JSONResponse:
    exc = cast(AppError, raised)
    headers: dict[str, str] = {}
    if isinstance(exc, AuthenticationError):
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        error_body(exc.code, exc.message), status_code=exc.status_code, headers=headers
    )


async def _handle_validation_error(_request: Request, raised: Exception) -> JSONResponse:
    exc = cast(RequestValidationError, raised)
    # Only location, message, and type: echoing `input` could reflect secrets back.
    details = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
        for err in exc.errors()
    ]
    return JSONResponse(
        error_body("validation_error", "Request validation failed.", details=details),
        status_code=422,
    )


async def _handle_http_exception(_request: Request, raised: Exception) -> JSONResponse:
    exc = cast(StarletteHTTPException, raised)
    code = _HTTP_CODES.get(exc.status_code, "http_error")
    message = exc.detail if isinstance(exc.detail, str) else "HTTP error."
    return JSONResponse(
        error_body(code, message),
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_exception", error_type=type(exc).__name__)
    return JSONResponse(error_body("internal_error", "Internal server error."), status_code=500)


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)
