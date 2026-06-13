from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.rbac import Permission as PermissionRow


class Permission(StrEnum):
    """The permission catalogue — the ONLY hardcoded piece of the RBAC
    engine (typo-safe, autocompleted, refactorable). Synced to the
    `permission` table at boot; bindings and role assignments live in
    data. A permission means nothing unless a binding checks it:
    adding a guarded action = one line here + one binding.
    """

    CASE_VIEW = "case.view"
    CASE_EDIT = "case.edit"
    CASE_DELETE = "case.delete"
    # Writing to the per-step comment thread (a CLIENT-VISIBLE channel) is
    # a capability distinct from viewing (case.view) or editing the
    # dossier (case.edit): an agency can let someone talk to the client
    # without granting dossier edits, or withhold it from a pure viewer.
    CASE_COMMENT = "case.comment"
    STEP_COMPLETE = "step.complete"
    REMINDER_CREATE = "reminder.create"
    REMINDER_APPROVE = "reminder.approve"
    JOURNEY_CONFIGURE = "journey.configure"
    DOCUMENT_VALIDATE = "document.validate"
    NOTE_VIEW_CONFIDENTIAL = "note.view_confidential"
    AGENCY_MANAGE = "agency.manage"
    AGENT_MANAGE = "agent.manage"
    AGENT_IMPERSONATE = "agent.impersonate"
    ROLE_MANAGE = "role.manage"
    JOB_MANAGE = "job.manage"
    FIELD_MANAGE = "field.manage"


def _label(key: str) -> str:
    return key.replace(".", " ").replace("_", " ").capitalize()


async def sync_permissions(db: AsyncSession) -> None:
    """Mirror the catalogue into the `permission` table.

    Inserts missing keys, NEVER deletes — an unknown key in DB may
    belong to a newer parallel deployment, and role_permission rows
    must not be silently severed.
    """
    existing = set((await db.execute(select(PermissionRow.key))).scalars())
    for perm in Permission:
        if perm.value not in existing:
            db.add(
                PermissionRow(
                    key=perm.value,
                    label=_label(perm.value),
                    category=perm.value.split(".", 1)[0],
                )
            )
    await db.commit()
