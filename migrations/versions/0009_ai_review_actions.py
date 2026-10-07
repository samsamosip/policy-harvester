"""The AI review acts: it corrects drafts and merges duplicates.

- Search chunks were frozen once embedded. A draft corrected before publication needs its chunk
  rebuilt from the corrected text; chunks of published versions stay frozen.
- Identity decisions get an ``ai_review`` actor: a merge the review model verified against the
  source is confirmed (an ``llm_suggestion`` still cannot be).
"""
from __future__ import annotations

from alembic import op

revision = "0009_ai_review_actions"
down_revision = "0008_ai_review"
branch_labels = None
depends_on = None

DDL = """
CREATE OR REPLACE FUNCTION inha_policy.prevent_published_chunk_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, inha_policy, public
AS $fn$
BEGIN
    IF OLD.embedding_created_at IS NOT NULL AND EXISTS (
        SELECT 1 FROM opportunity_versions v
        WHERE v.id = OLD.opportunity_version_id AND v.published_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION '% of a published version is finalized; append a new version instead', TG_TABLE_NAME;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$fn$;
DROP TRIGGER IF EXISTS search_chunks_immutable_when_embedded ON inha_policy.search_chunks;
CREATE TRIGGER search_chunks_immutable_when_embedded
    BEFORE UPDATE OR DELETE ON inha_policy.search_chunks
    FOR EACH ROW EXECUTE FUNCTION inha_policy.prevent_published_chunk_change();
ALTER TABLE inha_policy.identity_decisions DROP CONSTRAINT IF EXISTS identity_decisions_actor_kind_check;
ALTER TABLE inha_policy.identity_decisions ADD CONSTRAINT identity_decisions_actor_kind_check
  CHECK (actor_kind IN ('deterministic_rule', 'human', 'llm_suggestion', 'ai_review'));
"""


def upgrade() -> None:
    op.get_bind().connection.driver_connection.execute(DDL, prepare=False)


def downgrade() -> None:
    op.get_bind().connection.driver_connection.execute("""
DROP TRIGGER IF EXISTS search_chunks_immutable_when_embedded ON inha_policy.search_chunks;
CREATE TRIGGER search_chunks_immutable_when_embedded
    BEFORE UPDATE OR DELETE ON inha_policy.search_chunks
    FOR EACH ROW EXECUTE FUNCTION inha_policy.prevent_finalized_row_change('embedding_created_at');
DROP FUNCTION IF EXISTS inha_policy.prevent_published_chunk_change();
ALTER TABLE inha_policy.identity_decisions DROP CONSTRAINT IF EXISTS identity_decisions_actor_kind_check;
ALTER TABLE inha_policy.identity_decisions ADD CONSTRAINT identity_decisions_actor_kind_check
  CHECK (actor_kind IN ('deterministic_rule', 'human', 'llm_suggestion'));
""", prepare=False)
