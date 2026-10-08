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
    # Agency-internal cost tracking (Reside). Two distinct gestures: SEE the
    # costs + total (cost.view) vs ENTER/correct/delete a line (cost.manage) —
    # an associate consults, an assistant records. NEVER an external permission.
    COST_VIEW = "cost.view"
    COST_MANAGE = "cost.manage"
    AGENCY_MANAGE = "agency.manage"
    AGENT_MANAGE = "agent.manage"
    AGENT_IMPERSONATE = "agent.impersonate"
    ROLE_MANAGE = "role.manage"
    JOB_MANAGE = "job.manage"
    FIELD_MANAGE = "field.manage"
    # Preparing/running a CRM import (the import socle, BLOC 1+). A config
    # capability like field.manage / journey.configure: admin and
    # case_manager hold it by default; viewer/member do not.
    IMPORT_MANAGE = "import.manage"
    # Email module (Lemlist campaigns, ported from Prism's outreach.*). The
    # "right to activate": an agency the platform switched on still grants
    # these one by one (Settings > Roles). Only admin and superadmin hold
    # them by default; case_manager EXCLUDES them (baseline.py), member and
    # viewer never list them, external roles never hold them. email.view
    # goes with case.view (checked by the module's Manager, like Prism's
    # outreach.view + contacts.view): the module shows client names and
    # email addresses.
    EMAIL_VIEW = "email.view"
    # Create, edit, duplicate, archive, delete campaigns, steps and AI
    # variables; previews and AI tests; senders.
    EMAIL_MANAGE = "email.manage"
    # Add client profiles or companies to a campaign; move between campaigns.
    EMAIL_ENROLL = "email.enroll"
    # Pause and resume a recipient.
    EMAIL_PAUSE = "email.pause"
    # Remove a recipient from a campaign.
    EMAIL_REMOVE = "email.remove"
    # Sync and Launch at Lemlist, and (with email.manage) any state change of
    # a campaign that lives at Lemlist: the gestures that send real email.
    EMAIL_SYNC = "email.sync"
    # PLATFORM-scope permission (NOT agency-scoped). Held ONLY by the
    # `superadmin` system role: creating an agency is a platform operation,
    # not agency work — admin/case_manager are explicitly excluded from it
    # (see _PLATFORM_SET in baseline.py), exactly like the external.* barrier.
    # Granting it conveys NO access to any agency's data: it gates a single
    # future endpoint (POST /agencies, BLOC 2) and nothing else. The
    # per-agency WHERE agency_id scoping in the repositories is untouched and
    # enforce() still ignores agency_id, so a superadmin can read no dossier.
    AGENCY_CREATE = "agency.create"
    # PLATFORM-scope (same barrier as AGENCY_CREATE): the superadmin internal
    # task backlog (/admin/tasks). Dedicated so delegating tasks one day never
    # implies delegating agency creation — recabling is data, not code.
    PLATFORM_TASK_MANAGE = "platform.task_manage"
    # External-provider permissions (wave B). The `external.` prefix is a
    # STRUCTURAL barrier: these only gate /external/* portal routes (each
    # scoped by assignment), and internal roles never hold them — so an
    # external permission cannot open an internal route, nor vice versa.
    EXTERNAL_CASE_VIEW = "external.case.view"
    EXTERNAL_DOCUMENT_UPLOAD = "external.document.upload"
    EXTERNAL_CASE_COMMENT = "external.case.comment"
    EXTERNAL_STEP_VALIDATE = "external.step.validate"


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
