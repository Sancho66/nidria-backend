"""Shared RBAC seed logic — the single source for the initial RBAC data.

Consumed by BOTH the test harness (conftest fixture) and
`scripts/seed.py` (step 14). The default matrix here is SEED DATA, not
enforcement: once in the DB it is editable per agency without any
deploy — the engine itself never names a role or a permission.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import ProtectedResource, Role, RolePermission
from src.core.enums import Audience
from src.core.rbac.permissions import Permission, sync_permissions


@dataclass(frozen=True)
class RouteBinding:
    """Declared by each domain router in a module-level BINDINGS list
    (step 6+); aggregated by `collect_bindings` and upserted to
    `protected_resource`."""

    method: str
    path: str
    audience: Audience
    permission: Permission | None = None


# Default matrix of the 4 system roles. `member` holds REMINDER_APPROVE
# on purpose: approval is a human-in-the-loop gate before sending, not
# a hierarchy gate — the agent managing the case approves their own
# reminders (an agency wanting it manager-only edits the matrix in DB).
# NOTE_VIEW_CONFIDENTIAL is admin-only (Eloïse's spec); the structure
# permissions (agency/agent/role.manage) are admin-only too.
# external.* permissions belong ONLY to external roles — never to an
# internal role (admin/case_manager included). An internal actor holding
# one would otherwise reach the /external portal; excluding them here is
# the matrix-level half of the structural barrier (the enforce() guard +
# get_case_for_external being the others).
EXTERNAL_PERMISSIONS: tuple[Permission, ...] = (
    Permission.EXTERNAL_CASE_VIEW,
    Permission.EXTERNAL_DOCUMENT_UPLOAD,
    Permission.EXTERNAL_CASE_COMMENT,
    Permission.EXTERNAL_STEP_VALIDATE,
)
_EXTERNAL_SET = set(EXTERNAL_PERMISSIONS)

# PLATFORM-scope permissions: held ONLY by the `superadmin` system role,
# never by an agency role. Same matrix-level barrier as _EXTERNAL_SET —
# excluded from admin/case_manager below so a NEW platform permission can
# never silently land on an agency admin (deny-by-default at the matrix).
# These gate platform endpoints (agency creation); they convey NO
# cross-agency data access — the agency_id scoping in repositories and
# enforce()'s agency-blindness are both untouched.
PLATFORM_PERMISSIONS: tuple[Permission, ...] = (Permission.AGENCY_CREATE,)
_PLATFORM_SET = set(PLATFORM_PERMISSIONS)

SYSTEM_ROLE_MATRIX: dict[str, tuple[Permission, ...]] = {
    # admin = everything EXCEPT the external-provider AND platform-scope
    # permissions (the former belong only to external roles, the latter only
    # to superadmin — both structural barriers).
    "admin": tuple(p for p in Permission if p not in _EXTERNAL_SET and p not in _PLATFORM_SET),
    "case_manager": tuple(
        p
        for p in Permission
        if p
        not in {
            Permission.AGENCY_MANAGE,
            Permission.AGENT_MANAGE,
            # Built by exclusion: every new structure permission MUST be
            # listed here or case_manager silently inherits it.
            Permission.AGENT_IMPERSONATE,
            Permission.ROLE_MANAGE,
            Permission.JOB_MANAGE,
            # Configuring custom fields is admin config, not case work —
            # case_manager fills values (case.edit), it doesn't define.
            Permission.FIELD_MANAGE,
            # NB: IMPORT_MANAGE is deliberately NOT excluded — case_manager
            # imports dossiers (bulk onboarding is case work). An agency can
            # still revoke it via the matrix (data, no deploy).
            Permission.NOTE_VIEW_CONFIDENTIAL,
            *_EXTERNAL_SET,
            # agency.create is platform-only — case_manager never creates
            # agencies (see _PLATFORM_SET; mirrors the admin exclusion).
            *_PLATFORM_SET,
        }
    ),
    "member": (
        Permission.CASE_VIEW,
        Permission.CASE_EDIT,
        Permission.CASE_COMMENT,
        Permission.STEP_COMPLETE,
        Permission.REMINDER_CREATE,
        Permission.REMINDER_APPROVE,
        Permission.DOCUMENT_VALIDATE,
    ),
    # viewer reads the dossier (and comment threads) but cannot post.
    "viewer": (Permission.CASE_VIEW,),
    # superadmin is a PLATFORM operator, not an agency actor: it holds
    # EXACTLY agency.create and nothing else — no case.view, no agency-data
    # permission at all. Login / profile / logout need NO permission (those
    # routes are AGENT-audience with permission=None), so this single grant
    # suffices. BLOC 1 guarantee: it has NO cross-agency access — enforce()
    # still ignores agency_id and the repositories' WHERE agency_id filters
    # are untouched (cross-agency read is the separate Phase 2). agency_id
    # stays NOT NULL, so a superadmin agent still belongs to a home agency;
    # assign it a dedicated empty one (see scripts/seed.py docstring).
    "superadmin": (Permission.AGENCY_CREATE,),
}

# The 6 fixed EXTERNAL system roles (providers). Wave B: they hold the
# 3 external.* permissions — which only gate the /external portal, every
# route there scoped by assignment (permission ∧ scoping, never a
# permission alone reaching an unassigned case).
EXTERNAL_ROLE_NAMES: tuple[str, ...] = (
    "external_lawyer",
    "external_notary",
    "external_bank",
    "external_accountant",
    "external_translator",
    "external_other",
)


def collect_bindings() -> list[RouteBinding]:
    """Aggregate the BINDINGS lists of all domain routers.

    Function-level imports on purpose: routers import RouteBinding from
    this module, a module-level import here would be circular. Each
    step appends its router as endpoints land.
    """
    from src.activity.activity_router import BINDINGS as activity_bindings
    from src.agencies.agencies_router import BINDINGS as agencies_bindings
    from src.auth.auth_router import BINDINGS as auth_bindings
    from src.cases.cases_router import BINDINGS as cases_bindings
    from src.comments.comments_router import BINDINGS as comments_bindings
    from src.custom_fields.custom_fields_router import BINDINGS as custom_fields_bindings
    from src.dashboard.dashboard_router import BINDINGS as dashboard_bindings
    from src.documents.documents_router import BINDINGS as documents_bindings
    from src.expat.expat_router import BINDINGS as expat_bindings
    from src.external.external_router import BINDINGS as external_bindings
    from src.impersonation.impersonation_router import BINDINGS as impersonation_bindings
    from src.imports.imports_router import BINDINGS as imports_bindings
    from src.jobs.jobs_router import BINDINGS as jobs_bindings
    from src.journeys.journeys_router import BINDINGS as journeys_bindings
    from src.progress.progress_router import BINDINGS as progress_bindings
    from src.reminders.reminders_router import BINDINGS as reminders_bindings
    from src.roles.roles_router import BINDINGS as roles_bindings
    from src.views.views_router import BINDINGS as views_bindings

    return [
        *auth_bindings,
        *agencies_bindings,
        *roles_bindings,
        *impersonation_bindings,
        *journeys_bindings,
        *cases_bindings,
        *custom_fields_bindings,
        *imports_bindings,
        *views_bindings,
        *progress_bindings,
        *comments_bindings,
        *documents_bindings,
        *reminders_bindings,
        *jobs_bindings,
        *activity_bindings,
        *expat_bindings,
        *external_bindings,
        *dashboard_bindings,
    ]


async def _permission_ids_by_key(db: AsyncSession) -> dict[str, uuid.UUID]:
    rows = (await db.execute(select(PermissionRow))).scalars().all()
    return {row.key: row.id for row in rows}


async def seed_system_roles(db: AsyncSession) -> None:
    """Idempotent and ADDITIVE for system roles.

    System roles belong to the platform (agency_id NULL, shared, not
    agency-editable), so the in-code SYSTEM_ROLE_MATRIX is their
    additive source of truth: every run creates missing roles AND
    inserts missing role_permission rows — that is how each step's
    "+1 catalogue line" reaches existing deployments. A re-seed NEVER
    deletes: removing a permission from a system role is an explicit
    migration, not a seed run. Custom (agency) roles are never touched.
    """
    perm_ids = await _permission_ids_by_key(db)
    existing_roles = {
        role.name: role for role in (await db.execute(select(Role).where(Role.is_system))).scalars()
    }
    for name, perms in SYSTEM_ROLE_MATRIX.items():
        role = existing_roles.get(name)
        if role is None:
            role = Role(name=name, is_system=True, agency_id=None)
            db.add(role)
            await db.flush()
            granted: set[uuid.UUID] = set()
        else:
            granted = set(
                (
                    await db.execute(
                        select(RolePermission.permission_id).where(
                            RolePermission.role_id == role.id
                        )
                    )
                ).scalars()
            )
        for perm in perms:
            permission_id = perm_ids[perm.value]
            if permission_id not in granted:
                db.add(RolePermission(role_id=role.id, permission_id=permission_id))

    # External system roles — created if missing, granted the 3 external.*
    # permissions (additive, idempotent like the internal ones). Each only
    # opens /external/* routes, all scoped by assignment.
    for name in EXTERNAL_ROLE_NAMES:
        role = existing_roles.get(name)
        if role is None:
            role = Role(name=name, is_system=True, is_external=True, agency_id=None)
            db.add(role)
            await db.flush()
            granted = set()
        else:
            granted = set(
                (
                    await db.execute(
                        select(RolePermission.permission_id).where(
                            RolePermission.role_id == role.id
                        )
                    )
                ).scalars()
            )
        for perm in EXTERNAL_PERMISSIONS:
            permission_id = perm_ids[perm.value]
            if permission_id not in granted:
                db.add(RolePermission(role_id=role.id, permission_id=permission_id))
    await db.commit()


async def seed_bindings(db: AsyncSession, bindings: Iterable[RouteBinding]) -> None:
    """Declarative upsert by (method, path): missing rows inserted,
    existing rows realigned on audience/permission (the code declares
    the contract; runtime binding edits are a post-MVP surface)."""
    perm_ids = await _permission_ids_by_key(db)
    existing = {
        (row.method, row.route): row
        for row in (await db.execute(select(ProtectedResource))).scalars()
    }
    for binding in bindings:
        permission_id = perm_ids[binding.permission.value] if binding.permission else None
        row = existing.get((binding.method, binding.path))
        if row is None:
            db.add(
                ProtectedResource(
                    method=binding.method,
                    route=binding.path,
                    audience=binding.audience.value,
                    permission_id=permission_id,
                )
            )
        else:
            row.audience = binding.audience.value
            row.permission_id = permission_id
    await db.commit()


async def seed_rbac_baseline(db: AsyncSession, bindings: Iterable[RouteBinding] = ()) -> None:
    """Catalogue + system roles + bindings, in dependency order."""
    await sync_permissions(db)
    await seed_system_roles(db)
    await seed_bindings(db, bindings)
