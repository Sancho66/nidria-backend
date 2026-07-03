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
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


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


class ValidationError(NidriaError):
    status_code = 422
    code = "validation_error"


async def _nidria_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, NidriaError)
    headers = {"WWW-Authenticate": "Bearer"} if isinstance(exc, UnauthorizedError) else None
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message, "code": exc.code, "params": exc.params},
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(NidriaError, _nidria_error_handler)
