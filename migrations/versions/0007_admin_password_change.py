"""Password reset and change for admin accounts.

An admin can reset another account to a one-time temporary password; that account must choose
a new password at its next sign-in. password_changed_at also ends every session signed in
before the change.
"""
from __future__ import annotations

from alembic import op

revision = "0007_admin_password_change"
down_revision = "0006_admin_user_deletion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE inha_policy.admin_users
          ADD COLUMN IF NOT EXISTS password_change_required boolean NOT NULL DEFAULT false,
          ADD COLUMN IF NOT EXISTS password_changed_at timestamptz
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE inha_policy.admin_users DROP COLUMN IF EXISTS password_change_required,
          DROP COLUMN IF EXISTS password_changed_at
    """)
