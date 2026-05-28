"""add stage_map

Revision ID: 6b62897e
Revises: c015cc994f32
Create Date: 2026-05-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6b62897e"
down_revision: str | None = "c015cc994f32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    stage_trigger_enum = sa.Enum(
        "branch_created",
        "pr_opened",
        "ci_passed",
        "changes_requested",
        "pr_approved",
        "pr_closed",
        name="stage_trigger",
    )
    stage_trigger_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "stage_map",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plane_project_id", sa.String(), nullable=False),
        sa.Column("trigger", stage_trigger_enum, nullable=False),
        sa.Column("plane_state_name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plane_project_id", "trigger", name="uq_stage_map_project_trigger"
        ),
    )


def downgrade() -> None:
    op.drop_table("stage_map")
    op.execute("DROP TYPE IF EXISTS stage_trigger")
