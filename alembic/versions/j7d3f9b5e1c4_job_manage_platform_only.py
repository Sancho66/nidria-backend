"""job.manage becomes platform-only: removed from every role but superadmin

Decision of 08/10/2026: `job.manage` joins PLATFORM_PERMISSIONS, so only the
platform's superadmin manages the scheduler (/jobs/*). A job serves EVERY
agency (email_sync goes live in production): until now any agency admin could
enable, trigger or pause it for the whole platform, and read run logs that
name other agencies and their recipients.

The code change only stops the seed from granting it: seed_system_roles is
additive and never deletes. This migration removes the rows already granted:
every role_permission row of `job.manage` on EVERY role except the system
superadmin (agency_id NULL, is_system, name 'superadmin') — the system admin,
its copy-on-write clones, clones of other system roles (superadmin's
included) and plain custom roles alike: a platform permission belongs to no
agency role.

Nothing gives it back at boot: seed_system_roles no longer lists it for
admin, and propagate_new_permissions_to_clones only fills a permission BORN
after a clone's last matrix decision. `job.manage` is in the catalogue since
the baseline (11/06), before copy-on-write clones existed (12/06): in every
database its row was inserted at a boot that precedes any clone, so on a
clone (a superadmin clone included, whose origin keeps it) its absence reads
as a decision, never as ignorance. That is why permissions_reviewed_at is NOT
bumped here — bumping it would turn every permission born before today into
a decision and block a legitimate catch-up.

Pure SQL, idempotent, no application import. A database without the
`job.manage` row (the catalogue is inserted at boot by sync_permissions, after
the migrations) has nothing to delete.

Downgrade gives `job.manage` back to the system admin and to its direct
clones (the default before the decision), idempotently. NOT restored: grants
to plain custom roles or to clones of other system roles, which the upgrade
keeps no record of; and an admin clone whose agency had removed `job.manage`
on purpose gets it back.

Revision ID: j7d3f9b5e1c4
Revises: h9e5b1f7c3d0
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "j7d3f9b5e1c4"
down_revision: str | Sequence[str] | None = "h9e5b1f7c3d0"
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
              AND p.key = 'job.manage'
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
            JOIN permission p ON p.key = 'job.manage'
            WHERE (r.agency_id IS NULL AND r.is_system AND r.name = 'admin')
               OR r.cloned_from_role_id IN (
                    SELECT a.id FROM role a
                    WHERE a.agency_id IS NULL AND a.is_system AND a.name = 'admin'
               )
            ON CONFLICT DO NOTHING
            """
        )
    )
