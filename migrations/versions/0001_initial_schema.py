"""Initial schema for Escalada persistence."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "competitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("timezone('utc', now())"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("timezone('utc', now())"),
            nullable=False,
        ),
        sa.UniqueConstraint("name", name="uq_competition_name"),
    )
    op.create_index(
        op.f("ix_competitions_name"), "competitions", ["name"], unique=False
    )

    op.create_table(
        "boxes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("competition_id", sa.Integer(), sa.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("route_index", sa.Integer(), server_default="1", nullable=False),
        sa.Column("routes_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("holds_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("box_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("timezone('utc', now())"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["competition_id"],
            ["competitions.id"],
            name="fk_boxes_competition_id_competitions",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "competition_id", "name", name="uq_box_name_per_competition"
        ),
    )
    op.create_index(
        op.f("ix_boxes_competition_id"), "boxes", ["competition_id"], unique=False
    )
    op.create_index(op.f("ix_boxes_session_id"), "boxes", ["session_id"], unique=False)

    op.create_table(
        "competitors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("competition_id", sa.Integer(), sa.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("box_id", sa.Integer(), sa.ForeignKey("boxes.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=True),
        sa.Column("bib", sa.String(length=20), nullable=True),
        sa.Column("seed", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("timezone('utc', now())"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["competition_id"],
            ["competitions.id"],
            name="fk_competitors_competition_id_competitions",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "competition_id", "bib", name="uq_competitor_bib_per_competition"
        ),
    )
    op.create_index(
        op.f("ix_competitors_bib"), "competitors", ["bib"], unique=False
    )
    op.create_index(
        op.f("ix_competitors_competition_id"),
        "competitors",
        ["competition_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_competitors_box_id"),
        "competitors",
        ["box_id"],
        unique=False,
    )

    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("competition_id", sa.Integer(), sa.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("box_id", sa.Integer(), sa.ForeignKey("boxes.id", ondelete="CASCADE"), nullable=True),
        sa.Column("competitor_id", sa.Integer(), sa.ForeignKey("competitors.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("box_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("action_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("timezone('utc', now())"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["box_id"], ["boxes.id"], name="fk_events_box_id_boxes", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["competition_id"],
            ["competitions.id"],
            name="fk_events_competition_id_competitions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["competitor_id"],
            ["competitors.id"],
            name="fk_events_competitor_id_competitors",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("box_id", "action_id", name="uq_event_dedup"),
    )
    op.create_index(
        op.f("ix_events_action"), "events", ["action"], unique=False
    )
    op.create_index(
        op.f("ix_events_action_id"), "events", ["action_id"], unique=False
    )
    op.create_index(
        op.f("ix_events_box_id"), "events", ["box_id"], unique=False
    )
    op.create_index(
        op.f("ix_events_box_version"), "events", ["box_version"], unique=False
    )
    op.create_index(
        op.f("ix_events_competition_id"), "events", ["competition_id"], unique=False
    )
    op.create_index(
        "idx_events_competition_box_version",
        "events",
        ["competition_id", "box_id", "box_version"],
        unique=False,
    )
    op.create_index(
        op.f("ix_events_created_at"), "events", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_events_created_at"), table_name="events")
    op.drop_index("idx_events_competition_box_version", table_name="events")
    op.drop_index(op.f("ix_events_competition_id"), table_name="events")
    op.drop_index(op.f("ix_events_box_version"), table_name="events")
    op.drop_index(op.f("ix_events_box_id"), table_name="events")
    op.drop_index(op.f("ix_events_action_id"), table_name="events")
    op.drop_index(op.f("ix_events_action"), table_name="events")
    op.drop_table("events")
    op.drop_index(op.f("ix_competitors_competition_id"), table_name="competitors")
    op.drop_index(op.f("ix_competitors_box_id"), table_name="competitors")
    op.drop_index(op.f("ix_competitors_bib"), table_name="competitors")
    op.drop_table("competitors")
    op.drop_index(op.f("ix_boxes_session_id"), table_name="boxes")
    op.drop_index(op.f("ix_boxes_competition_id"), table_name="boxes")
    op.drop_table("boxes")
    op.drop_index(op.f("ix_competitions_name"), table_name="competitions")
    op.drop_table("competitions")
