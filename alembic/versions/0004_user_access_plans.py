"""Persist Telegram usernames and subscription access.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("username", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("subscription_plan", sa.String(length=30), nullable=True))
    op.add_column("users", sa.Column("subscription_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_users_username", "users", ["username"], unique=False)
    # Existing active users keep a short transition window; new users need an explicit grant.
    op.execute("""
        UPDATE users
        SET subscription_plan = 'legacy',
            subscription_expires_at = CURRENT_TIMESTAMP + INTERVAL '30 days'
        WHERE active = TRUE
    """)


def downgrade():
    op.drop_index("ix_users_username", table_name="users")
    op.drop_column("users", "subscription_expires_at")
    op.drop_column("users", "subscription_plan")
    op.drop_column("users", "username")
