"""add users table for auth"""

from datetime import datetime, timezone
import os

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from passlib.hash import pbkdf2_sha256

# revision identifiers, used by Alembic.
revision = "0002_add_users_table"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), server_default="viewer", nullable=False),
        sa.Column("assigned_boxes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.CheckConstraint(
            "role in ('admin','judge','viewer')", name="ck_users_role_valid"
        ),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    # Seed a default admin for local/dev use only
    admin_password = os.getenv("DEFAULT_ADMIN_PASSWORD", "admin")
    password_hash = pbkdf2_sha256.hash(admin_password)
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO users (username, password_hash, role, assigned_boxes, is_active, created_at, updated_at)
            VALUES (:username, :password_hash, 'admin', '[]'::jsonb, true, :now, :now)
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "username": "admin",
            "password_hash": password_hash,
            "now": datetime.now(timezone.utc),
        },
    )


def downgrade():
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
