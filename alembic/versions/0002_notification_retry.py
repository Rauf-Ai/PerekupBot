"""Add retry scheduling to notifications.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("notifications")}
    if "attempts" not in columns:
        op.add_column("notifications", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
    if "next_attempt_at" not in columns:
        op.add_column("notifications", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("notifications")}
    if "next_attempt_at" in columns:
        op.drop_column("notifications", "next_attempt_at")
    if "attempts" in columns:
        op.drop_column("notifications", "attempts")

