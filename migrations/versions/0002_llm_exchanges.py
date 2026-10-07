"""Raw LLM request/response log (one row per provider call)."""
from __future__ import annotations

from alembic import op

revision = "0002_llm_exchanges"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

DDL = """
CREATE TABLE IF NOT EXISTS inha_policy.llm_exchanges (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    purpose              text NOT NULL CHECK (purpose IN ('extraction', 'repair', 'transcription', 'other')),
    provider_name        text NOT NULL,
    model_id             text NOT NULL,
    notice_version_id    uuid REFERENCES inha_policy.notice_versions(id),
    asset_occurrence_id  uuid REFERENCES inha_policy.notice_version_assets(id),
    extraction_run_id    uuid REFERENCES inha_policy.extraction_runs(id),
    status               text NOT NULL CHECK (status IN ('ok', 'error')),
    error_message        text,
    request_storage_key  text NOT NULL,
    request_sha256       text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    response_storage_key text,
    response_sha256      text CHECK (response_sha256 IS NULL OR response_sha256 ~ '^[0-9a-f]{64}$'),
    latency_ms           integer CHECK (latency_ms >= 0),
    input_tokens         integer CHECK (input_tokens >= 0),
    output_tokens        integer CHECK (output_tokens >= 0),
    started_at           timestamptz NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    CHECK ((response_storage_key IS NULL) = (response_sha256 IS NULL))
);
CREATE INDEX IF NOT EXISTS llm_exchanges_run_idx ON inha_policy.llm_exchanges (extraction_run_id);
CREATE INDEX IF NOT EXISTS llm_exchanges_notice_idx ON inha_policy.llm_exchanges (notice_version_id, started_at);
DROP TRIGGER IF EXISTS llm_exchanges_append_only ON inha_policy.llm_exchanges;
CREATE TRIGGER llm_exchanges_append_only
    BEFORE UPDATE OR DELETE ON inha_policy.llm_exchanges
    FOR EACH ROW EXECUTE FUNCTION inha_policy.prevent_append_only_change();
"""


def upgrade() -> None:
    op.get_bind().connection.driver_connection.execute(DDL, prepare=False)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS inha_policy.llm_exchanges")
