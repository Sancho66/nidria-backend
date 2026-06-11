from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class NidriaError(Exception):
    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


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
        content={"detail": exc.message, "code": exc.code},
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(NidriaError, _nidria_error_handler)
