"""add billing tables

Revision ID: 3f9c2b7e8a1d
Revises: aef98d1954d0
Create Date: 2026-09-15 09:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3f9c2b7e8a1d"
down_revision: str | None = "aef98d1954d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Written by hand, mirroring app/models/billing.py. Server defaults use the
    # portable sa.func.now() / literal forms so MySQL and SQLite both accept them.
    op.create_table(
        "wallet",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("balance", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("balance >= 0", name="ck_wallet_balance_non_negative"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "subscription",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column("plan_name", sa.String(length=64), nullable=False),
        sa.Column("billing_interval", sa.String(length=8), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("credits_included", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("subscription", schema=None) as batch_op:
        batch_op.create_index(
            "ix_subscription_user_id_status", ["user_id", "status"], unique=False
        )

    op.create_table(
        "credit_purchase",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("pack_code", sa.String(length=32), nullable=False),
        sa.Column("pack_name", sa.String(length=64), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("credit_purchase", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_credit_purchase_user_id"), ["user_id"], unique=False
        )

    op.create_table(
        "credit_transaction",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("reference_type", sa.String(length=32), nullable=True),
        sa.Column("reference_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("credit_transaction", schema=None) as batch_op:
        batch_op.create_index(
            "ix_credit_transaction_user_id_created_at",
            ["user_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("credit_transaction", schema=None) as batch_op:
        batch_op.drop_index("ix_credit_transaction_user_id_created_at")
    op.drop_table("credit_transaction")

    with op.batch_alter_table("credit_purchase", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_credit_purchase_user_id"))
    op.drop_table("credit_purchase")

    with op.batch_alter_table("subscription", schema=None) as batch_op:
        batch_op.drop_index("ix_subscription_user_id_status")
    op.drop_table("subscription")

    op.drop_table("wallet")
