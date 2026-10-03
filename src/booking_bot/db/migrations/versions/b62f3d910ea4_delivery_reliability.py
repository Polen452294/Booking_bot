"""Transactional webhook receipts and notification claim ownership."""

import sqlalchemy as sa
from alembic import op

revision = "b62f3d910ea4"
down_revision = "a41d2c9e7b63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_update_receipts",
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("namespace", "update_id"),
    )
    op.create_index(
        "ix_telegram_update_receipts_expires_at", "telegram_update_receipts", ["expires_at"]
    )
    op.add_column("notification_jobs", sa.Column("claim_token", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("notification_jobs", "claim_token")
    op.drop_table("telegram_update_receipts")
