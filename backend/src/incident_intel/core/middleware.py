"""ASGI middleware: request IDs, request-scoped log context, access logging, body limits."""

import re
import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from incident_intel.core.errors import error_body

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

logger = structlog.get_logger("incident_intel.access")


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            candidate = value.decode("latin-1")
            return candidate if _VALID_REQUEST_ID.match(candidate) else None
    return None


class RequestContextMiddleware:
    """Assigns a request ID, binds it to the log context, and logs one line per request.

    Also the last line of defense: an exception no handler recognized is logged with its
    traceback and answered with a 500 in the standard envelope, while the request id is
    still bound (Starlette's own fallback runs outside this middleware and would lose it).

    Implemented as plain ASGI (not BaseHTTPMiddleware) so context variables propagate
    correctly into the endpoint. Only method, path (without query string), status, and
    duration are logged — never headers or bodies.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status_code = 500
        response_started = False
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_started = True
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception as exc:
            logger.exception("unhandled_exception", error_type=type(exc).__name__)
            if response_started:
                raise  # too late to send an error response; let the server close it
            response = JSONResponse(
                error_body("internal_error", "Internal server error."), status_code=500
            )
            await response(scope, receive, send_with_request_id)
        finally:
            logger.info(
                "http_request",
                method=scope["method"],
                path=scope["path"],
                status=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            structlog.contextvars.clear_contextvars()


_TOO_LARGE_MESSAGE = "Request body is too large."


class BodySizeLimitMiddleware:
    """Rejects request bodies larger than ``max_bytes`` with 413.

    A declared ``Content-Length`` over the limit is rejected before any body is read.
    Bodies without a declared length (chunked uploads) are counted as they stream in, and
    reading stops as soon as the limit is crossed, so memory use stays bounded either way.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            response = JSONResponse(
                error_body("payload_too_large", _TOO_LARGE_MESSAGE), status_code=413
            )
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # FastAPI re-raises HTTPException from body reading; the app's handler
                    # renders it in the standard error envelope.
                    raise HTTPException(status_code=413, detail=_TOO_LARGE_MESSAGE)
            return message

        await self.app(scope, limited_receive, send)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None
