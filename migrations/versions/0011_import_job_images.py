"""Additional ordered captures for the same short-lived import job."""
from alembic import op

revision = "0011_import_job_images"
down_revision = "0010_import_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Position 1 is import_jobs.image_bytes, preserving queued jobs during upgrades.
    op.execute("""
        CREATE TABLE inha_policy.import_job_images (
            request_id uuid NOT NULL REFERENCES inha_policy.import_jobs(request_id) ON DELETE CASCADE,
            position integer NOT NULL CHECK (position BETWEEN 2 AND 5),
            image_bytes bytea NOT NULL CHECK (octet_length(image_bytes) BETWEEN 1 AND 10485760),
            image_mime text NOT NULL CHECK (image_mime IN ('image/png', 'image/jpeg', 'image/webp')),
            PRIMARY KEY (request_id, position)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE inha_policy.import_job_images")
