"""API keys for the public API (only a SHA-256 of each key is stored)."""
from __future__ import annotations

from alembic import op

revision = "0003_api_keys"
down_revision = "0002_llm_exchanges"
branch_labels = None
depends_on = None

DDL = """
CREATE TABLE IF NOT EXISTS inha_policy.api_keys (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name                  text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
    key_prefix            text NOT NULL UNIQUE,
    key_sha256            text NOT NULL UNIQUE CHECK (key_sha256 ~ '^[0-9a-f]{64}$'),
    rate_limit_per_minute integer NOT NULL DEFAULT 120 CHECK (rate_limit_per_minute BETWEEN 1 AND 10000),
    created_by            uuid REFERENCES inha_policy.admin_users(id),
    created_at            timestamptz NOT NULL DEFAULT now(),
    expires_at            timestamptz,
    revoked_at            timestamptz,
    revoked_by            uuid REFERENCES inha_policy.admin_users(id),
    last_used_at          timestamptz,
    request_count         bigint NOT NULL DEFAULT 0,
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL)),
    CHECK (expires_at IS NULL OR expires_at > created_at)
);
"""


def upgrade() -> None:
    op.get_bind().connection.driver_connection.execute(DDL, prepare=False)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS inha_policy.api_keys")
