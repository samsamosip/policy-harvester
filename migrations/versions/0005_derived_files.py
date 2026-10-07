"""Files derived from a stored asset for display, such as the PDF rendering of an HWP file."""
from __future__ import annotations

from alembic import op

revision = "0005_derived_files"
down_revision = "0004_v2_enum_checks"
branch_labels = None
depends_on = None

DDL = """
CREATE TABLE IF NOT EXISTS inha_policy.derived_files (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    binary_asset_id  uuid NOT NULL REFERENCES inha_policy.binary_assets(id),
    kind             text NOT NULL CHECK (kind IN ('pdf_render')),
    tool             text NOT NULL,
    storage_key      text NOT NULL,
    sha256           text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    byte_size        bigint NOT NULL CHECK (byte_size > 0),
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (binary_asset_id, kind, tool)
);
DROP TRIGGER IF EXISTS derived_files_append_only ON inha_policy.derived_files;
CREATE TRIGGER derived_files_append_only
    BEFORE UPDATE OR DELETE ON inha_policy.derived_files
    FOR EACH ROW EXECUTE FUNCTION inha_policy.prevent_append_only_change();
"""


def upgrade() -> None:
    op.get_bind().connection.driver_connection.execute(DDL, prepare=False)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS inha_policy.derived_files")
