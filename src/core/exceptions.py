"""Domain exceptions + the single error envelope.

Every error returned to a client goes through NidriaError — never a bare
HTTPException — and serializes as:

    {"detail": <english message>, "code": <stable code>, "params": {...}}

`detail` is the human-readable ENGLISH message: authoritative for logs and
kept stable, displayed by the frontend only as a fallback. It is NOT the
i18n surface.

`code` + `params` are the i18n surface (point 9): the frontend resolves
`code` against its locale catalogs and interpolates `params`.

Naming convention:
- code = "<domain>.<identifier>", snake_case. The domain is the product
  domain of the ENTITY, singular ("case", "journey", "import", "role"…);
  the identifier names the precise failure ("import.mapping_invalid",
  "journey.template_in_use"). The same concept raised from several places
  reuses ONE code ("journey.template_not_found" wherever a template
  lookup fails, including outside src/journeys).
- params are NAMED and JSON-serializable, values kept simple (str, int,
  list[str]): exactly what a translation needs to interpolate, nothing
  more — debug-only context (wrapped exception text…) stays in `detail`.
- A raise without an explicit code falls back to its class CATEGORY
  ("not_found", "conflict", "validation_error"…): the pre-i18n
  behaviour, migrated domain by domain (wave 1: imports, journeys,
  cases; the rest keeps the category default until its wave).

EVERY subclass declares its OWN `code`. That is not a style rule, it is a
guard: on 2026-07-17, `TooManyRequestsError` was inserted BETWEEN
`ValidationError`'s `status_code` and its `code` line, and silently adopted
it — for 28 days a 422 answered "internal_error" while a 429 answered
"validation_error". Nothing broke visibly (no consumer reads a dotless
code), which is exactly why it survived. `test_error_codes` now refuses a
subclass that inherits the internal_error default, and refuses two
subclasses sharing a code.
"""

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class NidriaError(Exception):
    status_code: int = 500
    code: str = "internal_error"  # category default; a raise may pass a specific code

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code  # instance attribute shadows the class category
        self.params: dict[str, Any] = params or {}


class BadRequestError(NidriaError):
    status_code = 400
    code = "bad_request"


class UnauthorizedError(NidriaError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(NidriaError):
    status_code = 403
    code = "forbidden"


class NotFoundError(NidriaError):
    status_code = 404
    code = "not_found"


class ConflictError(NidriaError):
    status_code = 409
    code = "conflict"


class PayloadTooLargeError(NidriaError):
    status_code = 413
    code = "payload_too_large"


class UnsupportedMediaTypeError(NidriaError):
    status_code = 415
    code = "unsupported_media_type"


class ValidationError(NidriaError):
    status_code = 422
    code = "validation_error"


class TooManyRequestsError(NidriaError):
    status_code = 429
    code = "too_many_requests"


class UpstreamError(NidriaError):
    """A dependent external service failed or is unconfigured (AI...)."""

    status_code = 502
    code = "upstream_error"


async def _nidria_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, NidriaError)
    headers = {"WWW-Authenticate": "Bearer"} if isinstance(exc, UnauthorizedError) else None
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message, "code": exc.code, "params": exc.params},
        headers=headers,
    )


def request_id_of(request: Request) -> str:
    """The id a client (or Fly's edge, `Fly-Request-Id`) can quote back to
    us; minted here when nobody sent one, so a 500 is always traceable."""
    return (
        request.headers.get("fly-request-id")
        or request.headers.get("x-request-id")
        or uuid.uuid4().hex
    )


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """The LAST net (incident 15/09/2026, Fly-Request-Id 01M2K3SC…): an
    unhandled exception used to leave the app as Starlette's bare
    `500 Internal Server Error` — text/plain, empty for the browser, and the
    traceback only in the server log with nothing to join it to the client's
    report. Now: the SAME JSON envelope as every other error, the stable
    category code `internal_error`, the request id in params AND in the
    `X-Request-Id` header, and the full traceback logged server-side with
    that id, the method and the path — so a report « 500 at 18:03, id X »
    lands on one log line."""
    request_id = request_id_of(request)
    logger.exception(
        "unhandled error request_id=%s %s %s",
        request_id,
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error.",
            "code": "internal_error",
            "params": {"request_id": request_id},
        },
        headers={"X-Request-Id": request_id},
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(NidriaError, _nidria_error_handler)
    # Starlette routes a bare `Exception` handler through its
    # ServerErrorMiddleware: our JSON goes out, then the exception is
    # re-raised for the server/test client to see — both facts, never a
    # silent swallow.
    app.add_exception_handler(Exception, _unhandled_error_handler)
