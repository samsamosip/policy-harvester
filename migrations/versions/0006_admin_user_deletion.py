"""Deleting an admin account keeps the row (audit logs, keys and overrides point at it).

The account leaves the list and can no longer sign in; its email is released for reuse and the
original address is kept in deleted_email.
"""
from __future__ import annotations

from alembic import op

revision = "0006_admin_user_deletion"
down_revision = "0005_derived_files"
branch_labels = None
depends_on = None

DDL = """
ALTER TABLE inha_policy.admin_users
  ADD COLUMN IF NOT EXISTS deleted_at timestamptz,
  ADD COLUMN IF NOT EXISTS deleted_email text;
ALTER TABLE inha_policy.admin_users DROP CONSTRAINT IF EXISTS admin_users_deleted_inactive;
ALTER TABLE inha_policy.admin_users
  ADD CONSTRAINT admin_users_deleted_inactive CHECK (deleted_at IS NULL OR NOT is_active);
-- Deactivation is gone from the admin; accounts deactivated before are treated as deleted.
UPDATE inha_policy.admin_users
SET deleted_at=now(), deleted_email=email, email='deleted+' || id::text || '@deleted.invalid', updated_at=now()
WHERE NOT is_active AND deleted_at IS NULL;
"""


def upgrade() -> None:
    op.get_bind().connection.driver_connection.execute(DDL, prepare=False)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE inha_policy.admin_users DROP CONSTRAINT IF EXISTS admin_users_deleted_inactive,
          DROP COLUMN IF EXISTS deleted_at, DROP COLUMN IF EXISTS deleted_email
    """)
