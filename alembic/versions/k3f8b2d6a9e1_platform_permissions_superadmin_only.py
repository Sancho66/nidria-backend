"""Platform permissions leave every role but the system superadmin

The probe of 08/10/2026 found an escalation into the platform layer: an
agency admin who knew the id of the `superadmin` system role renamed it
(PATCH /agencies/me/roles/{id}), and the copy-on-write flow minted, inside
the agency, a clone carrying every platform permission (agency.create,
platform.task_manage, job.manage). The invitation flow (no ceiling, and a
check on the role NAME, which the renamed clone escaped) then let the admin
invite someone with that clone, who reached /jobs. The code now keeps the
platform role out of every agency door and caps every grant at a ceiling
that never holds a platform permission (src/core/rbac/delegation.py).

This migration cleans what the hole, or any earlier slip, may have left:
every role_permission row of a platform permission on any role other than
the system superadmin (agency_id NULL, is_system, name 'superadmin') is
deleted. That covers the clones of superadmin, plain custom roles, the
clones of other system roles, and the system roles of the agencies (admin,
case_manager, member, viewer, the external ones). A custom role that an
agency happened to name 'superadmin' is an agency row: cleaned too.

No agent is touched: an agent wearing a clone of superadmin keeps that role,
now without any platform permission (an internal matrix equal to the admin's
at most); reviewing those agents is a human decision. permissions_reviewed_at
is not bumped: the boot no longer hands a platform permission to any clone
(propagate_new_permissions_to_clones filters them), and bumping it would turn
every permission born before today into a decision and block a legitimate
catch-up.

Pure SQL, idempotent, no application import: the keys are written here, as
PLATFORM_PERMISSIONS lists them today. A database without these rows (fresh:
the catalogue is inserted at boot, after the migrations) has nothing to
delete.

Revision ID: k3f8b2d6a9e1
Revises: j7d3f9b5e1c4
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "k3f8b2d6a9e1"
down_revision: str | Sequence[str] | None = "j7d3f9b5e1c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            DELETE FROM role_permission rp
            USING permission p, role r
            WHERE p.id = rp.permission_id
              AND r.id = rp.role_id
              AND p.key IN ('agency.create', 'platform.task_manage', 'job.manage')
              AND NOT (r.agency_id IS NULL AND r.is_system AND r.name = 'superadmin')
            """
        )
    )


def downgrade() -> None:
    """Gives nothing back, on purpose. A platform permission found on an
    agency role was obtained through the escalation, or granted by mistake:
    restoring it would reopen the hole this revision closes, and the upgrade
    keeps no record that could tell a legitimate grant apart (there is none:
    no agency role may hold one)."""
