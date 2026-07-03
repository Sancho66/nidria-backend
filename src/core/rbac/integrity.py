from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.rbac import ProtectedResource

# Infra routes whitelisted IN CODE (Q2 decision): no binding required,
# enforce lets them through, the boot check ignores them. Product
# public routes (login, activate…) are NOT here — they get real
# audience=PUBLIC rows in `protected_resource`.
INFRA_WHITELIST = {
    "/ping",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/docs/oauth2-redirect",
}


class StartupError(RuntimeError):
    """Refuse to boot: a declared route has no protected_resource
    binding. Deny-by-default would silently 403 it — better to fail
    loud at startup than ship a dead (or worse, forgotten) route."""


async def assert_all_routes_bound(app: FastAPI, db: AsyncSession) -> None:
    declared: set[tuple[str, str]] = set()
    for route in app.routes:
        # Docs/openapi are plain Starlette Routes, not APIRoutes — the
        # global enforce dependency never applies to them anyway.
        if not isinstance(route, APIRoute):
            continue
        if route.path in INFRA_WHITELIST:
            continue
        for method in route.methods or set():
            # Explicit HEAD shares its GET binding (enforce normalizes
            # HEAD→GET); OPTIONS is CORS-middleware land.
            if method in {"HEAD", "OPTIONS"}:
                continue
            declared.add((method, route.path))

    rows = (await db.execute(select(ProtectedResource.method, ProtectedResource.route))).all()
    bound = {(method, route) for method, route in rows}

    missing = declared - bound
    if missing:
        raise StartupError(
            f"Routes without protected_resource binding (deny by default): {sorted(missing)}"
        )


def _declared_routes(app: FastAPI) -> set[tuple[str, str]]:
    declared: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or set():
            declared.add((method, route.path))
    return declared


def assert_impersonation_denylist_declared(app: FastAPI) -> None:
    """The enforcement route constants are security boundaries: a typo'd
    denylist entry protects nothing (silently), a typo'd allowlist or
    consent-exempt entry breaks its flow (silently). Every entry of the
    three constants must match a declared route of the REAL app —
    separate from assert_all_routes_bound so synthetic test apps can
    keep exercising the binding check alone."""
    from src.core.rbac.enforcement import (
        CONSENT_EXEMPT,
        IMPERSONATION_AGENT_DENIED,
        IMPERSONATION_WRITE_ALLOWLIST,
    )

    unknown = (
        IMPERSONATION_AGENT_DENIED | IMPERSONATION_WRITE_ALLOWLIST | CONSENT_EXEMPT
    ) - _declared_routes(app)
    if unknown:
        raise StartupError(
            f"Enforcement route entries matching no declared route: {sorted(unknown)}"
        )
