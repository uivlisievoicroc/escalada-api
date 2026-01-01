"""Add actor metadata columns to events (audit log)."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0003_add_event_actor_meta"
down_revision = "0002_add_users_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("actor_username", sa.String(length=100), nullable=True))
    op.add_column("events", sa.Column("actor_role", sa.String(length=20), nullable=True))
    op.add_column("events", sa.Column("actor_ip", sa.String(length=45), nullable=True))
    op.add_column("events", sa.Column("actor_user_agent", sa.Text(), nullable=True))

    op.create_index(op.f("ix_events_actor_username"), "events", ["actor_username"], unique=False)
    op.create_index(op.f("ix_events_actor_role"), "events", ["actor_role"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_events_actor_role"), table_name="events")
    op.drop_index(op.f("ix_events_actor_username"), table_name="events")

    op.drop_column("events", "actor_user_agent")
    op.drop_column("events", "actor_ip")
    op.drop_column("events", "actor_role")
    op.drop_column("events", "actor_username")

