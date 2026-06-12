"""single_role_per_agent_and_cow_clones

Prism RBAC model: ONE role per agent (agent.role_id FK NOT NULL,
agent_role M2M dropped) + copy-on-write system-role clones
(role.cloned_from_role_id).

Data backfill: every agent keeps their single current role; an agent
holding several keeps the MOST PRIVILEGED one. Privilege = permission
count DESC (which orders the system roles exactly admin > case_manager
> member > viewer and slots customs by their matrix size), system role
preferred on ties, then created_at/id as deterministic tiebreakers.
Agents with NO role (none exist in real databases — seed and
invitations always assign) fall back to the system 'viewer' role so
nobody is lost. No agent row is ever deleted.

Revision ID: d16404291a1d
Revises: d6565cf1841f
Create Date: 2026-06-12 19:17:58.691757

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d16404291a1d"
down_revision: Union[str, Sequence[str], None] = "d6565cf1841f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Copy-on-write link on role.
    op.add_column("role", sa.Column("cloned_from_role_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_role_cloned_from_role_id_role"),
        "role",
        "role",
        ["cloned_from_role_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_role_cloned_from_role_id"), "role", ["cloned_from_role_id"])

    # 2. agent.role_id, nullable for the backfill.
    op.add_column("agent", sa.Column("role_id", sa.Uuid(), nullable=True))

    # 3. Backfill: most privileged role per agent (see module docstring).
    op.execute(
        """
        WITH ranked AS (
            SELECT
                ar.agent_id,
                ar.role_id,
                ROW_NUMBER() OVER (
                    PARTITION BY ar.agent_id
                    ORDER BY
                        (SELECT COUNT(*) FROM role_permission rp
                          WHERE rp.role_id = r.id) DESC,
                        r.is_system DESC,
                        r.created_at,
                        r.id
                ) AS rn
            FROM agent_role ar
            JOIN role r ON r.id = ar.role_id
        )
        UPDATE agent
        SET role_id = ranked.role_id
        FROM ranked
        WHERE ranked.agent_id = agent.id AND ranked.rn = 1
        """
    )
    # Roleless agents (theoretical): least-privilege fallback, no agent lost.
    op.execute(
        """
        UPDATE agent
        SET role_id = (SELECT id FROM role WHERE name = 'viewer' AND is_system)
        WHERE role_id IS NULL
        """
    )

    # 4. Tighten and drop the M2M.
    op.alter_column("agent", "role_id", nullable=False)
    op.create_foreign_key(
        op.f("fk_agent_role_id_role"), "agent", "role", ["role_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_index(op.f("ix_agent_role_id"), "agent", ["role_id"])
    op.drop_table("agent_role")


def downgrade() -> None:
    op.create_table(
        "agent_role",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agent.id"], name=op.f("fk_agent_role_agent_id_agent"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["role.id"], name=op.f("fk_agent_role_role_id_role"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("agent_id", "role_id", name=op.f("pk_agent_role")),
    )
    op.execute("INSERT INTO agent_role (agent_id, role_id) SELECT id, role_id FROM agent")
    op.drop_index(op.f("ix_agent_role_id"), table_name="agent")
    op.drop_constraint(op.f("fk_agent_role_id_role"), "agent", type_="foreignkey")
    op.drop_column("agent", "role_id")
    op.drop_index(op.f("ix_role_cloned_from_role_id"), table_name="role")
    op.drop_constraint(op.f("fk_role_cloned_from_role_id_role"), "role", type_="foreignkey")
    op.drop_column("role", "cloned_from_role_id")
