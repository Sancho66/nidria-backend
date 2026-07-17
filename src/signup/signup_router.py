"""Self-serve signup — the product's first PUBLIC write routes. Real
PUBLIC rows in protected_resource (the RBAC doctrine: auditable lines,
never code-side holes), plus a minimal in-process per-IP rate limit."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.auth_schema import TokenPairResponse
from src.core import ratelimit
from src.core.database import get_db
from src.core.enums import Audience
from src.core.exceptions import BadRequestError, TooManyRequestsError
from src.core.rbac.baseline import RouteBinding
from src.signup.signup_manager import SignupManager, verify_turnstile
from src.signup.signup_schema import (
    SignupAccepted,
    SignupCompleteRequest,
    SignupRequest,
    SignupVerifyRequest,
    SignupVerifyResponse,
)

router = APIRouter(tags=["signup"])

BINDINGS = [
    RouteBinding("POST", "/signup", Audience.PUBLIC),
    RouteBinding("POST", "/signup/verify", Audience.PUBLIC),
    RouteBinding("POST", "/signup/complete", Audience.PUBLIC),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]

# Per-IP windows (in-process; the module is the seam if we go multi-machine).
_REQUEST_LIMIT = (10, 3600.0)  # 10 codes / hour / IP
_VERIFY_LIMIT = (15, 900.0)  # 15 tries / 15 min / IP (5 per code anyway)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _limit(request: Request, bucket: str, limit: tuple[int, float]) -> None:
    if not ratelimit.allow(
        f"{bucket}:{_client_ip(request)}", limit=limit[0], window_seconds=limit[1]
    ):
        raise TooManyRequestsError("Too many attempts; retry later.")


@router.post("/signup", response_model=SignupAccepted)
async def request_signup(payload: SignupRequest, request: Request, db: DbDep) -> SignupAccepted:
    """200 ALWAYS (known email, honeypot, whatever): the EMAIL differs,
    never the response — no enumeration."""
    _limit(request, "signup", _REQUEST_LIMIT)
    if not await verify_turnstile(payload.turnstile_token):
        raise BadRequestError("Verification failed.", code="signup.turnstile_failed")
    await SignupManager(db).request_code(payload)
    return SignupAccepted()


@router.post("/signup/verify", response_model=SignupVerifyResponse)
async def verify_signup(
    payload: SignupVerifyRequest, request: Request, db: DbDep
) -> SignupVerifyResponse:
    _limit(request, "signup-verify", _VERIFY_LIMIT)
    token = await SignupManager(db).verify_code(payload)
    return SignupVerifyResponse(completion_token=token)


@router.post("/signup/complete", response_model=TokenPairResponse)
async def complete_signup(payload: SignupCompleteRequest, db: DbDep) -> TokenPairResponse:
    """The single-transaction creation through the shared writer, then
    AUTO-LOGIN: the response is a live token pair — welcome screen next."""
    return await SignupManager(db).complete(payload)
