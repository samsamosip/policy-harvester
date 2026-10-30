"""Durable, short-lived backend import jobs, separate from archival notice data."""
from alembic import op

revision = "0010_import_jobs"
down_revision = "0009_ai_review_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE inha_policy.import_jobs (
        request_id uuid PRIMARY KEY,
        api_key_id uuid REFERENCES inha_policy.api_keys(id),
        input_type text NOT NULL CHECK (input_type IN ('url', 'image')),
        source_url text,
        image_bytes bytea,
        image_mime text,
        callback_url text,
        status text NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
        response jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        started_at timestamptz,
        expires_at timestamptz NOT NULL DEFAULT now() + interval '24 hours',
        callback_attempts integer NOT NULL DEFAULT 0,
        callback_due_at timestamptz,
        CHECK ((input_type = 'url' AND source_url IS NOT NULL AND image_bytes IS NULL)
            OR (input_type = 'image' AND source_url IS NULL AND image_bytes IS NOT NULL)),
        CHECK (image_bytes IS NULL OR octet_length(image_bytes) <= 10485760)
    )
    """)
    op.execute("CREATE INDEX import_jobs_pending ON inha_policy.import_jobs (status, created_at)")
    op.execute("CREATE INDEX import_jobs_callbacks ON inha_policy.import_jobs (callback_due_at) WHERE callback_due_at IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP TABLE inha_policy.import_jobs")
