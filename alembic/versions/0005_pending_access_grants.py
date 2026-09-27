"""Allow admins to grant access before a user starts the bot.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pending_access_grants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=32), nullable=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("subscription_plan", sa.String(length=30), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(username IS NOT NULL AND telegram_id IS NULL) OR (username IS NULL AND telegram_id IS NOT NULL)",
            name="ck_pending_access_grant_single_target",
        ),
    )
    op.create_index("ix_pending_access_grants_username", "pending_access_grants", ["username"], unique=True)
    op.create_index("ix_pending_access_grants_telegram_id", "pending_access_grants", ["telegram_id"], unique=True)


def downgrade():
    op.drop_index("ix_pending_access_grants_telegram_id", table_name="pending_access_grants")
    op.drop_index("ix_pending_access_grants_username", table_name="pending_access_grants")
    op.drop_table("pending_access_grants")
