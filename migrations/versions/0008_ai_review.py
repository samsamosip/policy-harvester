"""AI second review before a review item reaches people.

Review items that hinge on model judgement (quality, identity, revision candidates) are created
as ``ai_pending`` and a ``review`` job asks a stronger model; it either dismisses the item or
opens it with its reasoning.
"""
from __future__ import annotations

from alembic import op

revision = "0008_ai_review"
down_revision = "0007_admin_password_change"
branch_labels = None
depends_on = None

DDL = """
ALTER TABLE inha_policy.review_items DROP CONSTRAINT IF EXISTS review_items_status_check;
ALTER TABLE inha_policy.review_items ADD CONSTRAINT review_items_status_check
  CHECK (status IN ('ai_pending', 'open', 'in_review', 'resolved', 'dismissed'));
ALTER TABLE inha_policy.review_items DROP CONSTRAINT IF EXISTS review_items_check;
ALTER TABLE inha_policy.review_items ADD CONSTRAINT review_items_check
  CHECK ((status IN ('resolved', 'dismissed') AND resolved_at IS NOT NULL) OR
         (status IN ('ai_pending', 'open', 'in_review') AND resolved_at IS NULL));
ALTER TABLE inha_policy.crawl_jobs DROP CONSTRAINT IF EXISTS crawl_jobs_stage_check;
ALTER TABLE inha_policy.crawl_jobs ADD CONSTRAINT crawl_jobs_stage_check
  CHECK (stage IN ('discover_list', 'fetch_detail', 'download_asset', 'parse_document',
                   'structure', 'embed', 'publish', 'review'));
"""


def upgrade() -> None:
    op.get_bind().connection.driver_connection.execute(DDL, prepare=False)


def downgrade() -> None:
    op.get_bind().connection.driver_connection.execute("""
UPDATE inha_policy.review_items SET status='open' WHERE status='ai_pending';
DELETE FROM inha_policy.crawl_jobs WHERE stage='review';
ALTER TABLE inha_policy.review_items DROP CONSTRAINT review_items_status_check;
ALTER TABLE inha_policy.review_items ADD CONSTRAINT review_items_status_check
  CHECK (status IN ('open', 'in_review', 'resolved', 'dismissed'));
ALTER TABLE inha_policy.review_items DROP CONSTRAINT review_items_check;
ALTER TABLE inha_policy.review_items ADD CONSTRAINT review_items_check
  CHECK ((status IN ('resolved', 'dismissed') AND resolved_at IS NOT NULL) OR
         (status IN ('open', 'in_review') AND resolved_at IS NULL));
ALTER TABLE inha_policy.crawl_jobs DROP CONSTRAINT crawl_jobs_stage_check;
ALTER TABLE inha_policy.crawl_jobs ADD CONSTRAINT crawl_jobs_stage_check
  CHECK (stage IN ('discover_list', 'fetch_detail', 'download_asset', 'parse_document',
                   'structure', 'embed', 'publish'));
""", prepare=False)
