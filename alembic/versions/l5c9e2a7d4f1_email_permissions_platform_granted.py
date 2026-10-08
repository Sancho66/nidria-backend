"""The Email keys leave every role but the system superadmin: the platform grants them

Decision of Alexandre on 08/10/2026, replacing the spec's N3 ("admin and
superadmin receive them, the agency grants them role by role"): nobody holds
the six `email.*` keys by default, the agency admin included. A superadmin
chooses, agency by agency, which roles (admin included) receive them (PUT
/agencies/{agency_id}/email-module/roles/{role_id}); no agency flow adds or
removes one (src/core/rbac/delegation.py, PLATFORM_MANAGED_PERMISSIONS in
src/core/rbac/baseline.py).

The code change only stops the seed from granting them to the system admin:
seed_system_roles is additive and never deletes. This migration removes the
rows already granted: every role_permission row of an `email.*` key on EVERY
role except the system superadmin (agency_id NULL, is_system, name
'superadmin'), which works in its home agency, the module's pilot. That
covers the system admin, its copy-on-write clones, clones of other system
roles, plain custom roles an agency admin granted them to under the old rule,
and the clones of superadmin. After it, a superadmin grants them again where
they belong.

No agent is touched: an agent keeps its role, now without the Email keys.
permissions_reviewed_at is not bumped: the boot never hands a platform-managed
key to a clone (propagate_new_permissions_to_clones filters them), and bumping
it would turn every permission born before today into a decision and block a
legitimate catch-up.

In production the module never ran: the `email.*` rows of the catalogue do not
exist yet (they are inserted at boot by sync_permissions, after the
migrations), so this deletes nothing there; it cleans the development and
acceptance databases, where the system admin received them.

Pure SQL, idempotent, no application import: the keys are written here, as
EMAIL_PERMISSIONS lists them today.

Downgrade gives the six keys back to the system admin (the default before the
decision), idempotently. NOT restored: the grants of clones and custom roles,
which the upgrade keeps no record of (with the old code, the boot gives them
back to an admin clone whose last matrix decision predates the keys' birth in
that database, not to the others). NOT removed: the grants the platform made
after the upgrade, which the old code reads as ordinary agency grants.

Revision ID: l5c9e2a7d4f1
Revises: k3f8b2d6a9e1
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "l5c9e2a7d4f1"
down_revision: str | Sequence[str] | None = "k3f8b2d6a9e1"
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
              AND p.key IN ('email.view', 'email.manage', 'email.enroll',
                            'email.pause', 'email.remove', 'email.sync')
              AND NOT (r.agency_id IS NULL AND r.is_system AND r.name = 'superadmin')
            """
        )
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO role_permission (role_id, permission_id)
            SELECT r.id, p.id
            FROM role r
            JOIN permission p
              ON p.key IN ('email.view', 'email.manage', 'email.enroll',
                           'email.pause', 'email.remove', 'email.sync')
            WHERE r.agency_id IS NULL AND r.is_system AND r.name = 'admin'
            ON CONFLICT DO NOTHING
            """
        )
    )
