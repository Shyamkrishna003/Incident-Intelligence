"""Domain errors and their mapping to a consistent HTTP error envelope.

Every error response has the shape ``{"error": {"code", "message", "request_id", ...}}``.
"""

from typing import Any, cast

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = structlog.get_logger(__name__)


class AppError(Exception):
    status_code = 500
    code = "internal_error"
    default_message = "Internal server error."

    def __init__(
        self, message: str | None = None, *, details: list[dict[str, Any]] | None = None
    ) -> None:
        self.message = message or self.default_message
        # Structured, client-safe specifics (for example which batch item failed).
        self.details = details
        super().__init__(self.message)

    @property
    def headers(self) -> dict[str, str]:
        return {}


class InvalidInputError(AppError):
    status_code = 422
    # Same code as request-schema failures: clients handle one kind of 422.
    code = "validation_error"
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

    @property
    def headers(self) -> dict[str, str]:
        return {"WWW-Authenticate": "Bearer"}


class PermissionDeniedError(AppError):
    status_code = 403
    code = "forbidden"
    default_message = "The credentials do not permit this operation."


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    default_message = "Resource not found."


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    default_message = "The resource conflicts with existing state."


class IdempotencyKeyReusedError(ConflictError):
    code = "idempotency_conflict"
    default_message = "This Idempotency-Key was already used with different data."


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"
    default_message = "Too many requests. Retry later."

    def __init__(self, *, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__()

    @property
    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after_seconds)}


class ServiceUnavailableError(AppError):
    """A dependency is temporarily unavailable; the client should retry later."""

    status_code = 503
    code = "service_unavailable"
    default_message = "Temporarily unavailable. Retry later."

    def __init__(self, message: str | None = None, *, retry_after_seconds: int = 5) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)

    @property
    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after_seconds)}


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
    extra = {"details": exc.details} if exc.details is not None else {}
    return JSONResponse(
        error_body(exc.code, exc.message, **extra),
        status_code=exc.status_code,
        headers=exc.headers,
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


# Failures to reach a backing service (PostgreSQL): network errors surface as raw OSError
# (refused, DNS, timeout) at connect time, and as SQLAlchemy errors on a broken connection.
DEPENDENCY_ERRORS: tuple[type[Exception], ...] = (OSError, OperationalError, InterfaceError)


async def _handle_dependency_unavailable(_request: Request, exc: Exception) -> JSONResponse:
    logger.warning("dependency_unavailable", error_type=type(exc).__name__)
    unavailable = ServiceUnavailableError()
    return JSONResponse(
        error_body(unavailable.code, unavailable.message),
        status_code=unavailable.status_code,
        headers=unavailable.headers,
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Register handlers for expected errors.

    Anything else is caught by ``RequestContextMiddleware``, which answers 500 with the
    request id and logs the traceback.
    """
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    for error_type in DEPENDENCY_ERRORS:
        app.add_exception_handler(error_type, _handle_dependency_unavailable)
