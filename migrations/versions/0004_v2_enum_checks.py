"""Fix schema v2 enums in the database for rows written from now on.

Earlier (schema v1) rows hold free text such as "고등학생" or "장학금"; they are immutable history,
so the checks are NOT VALID: PostgreSQL enforces them on new rows without rewriting old ones.
"""
from __future__ import annotations

from alembic import op

revision = "0004_v2_enum_checks"
down_revision = "0003_api_keys"
branch_labels = None
depends_on = None

DDL = """
ALTER TABLE inha_policy.eligibility_profiles
  ADD CONSTRAINT eligibility_academic_levels_v2 CHECK (academic_levels <@ ARRAY[
    'elementary', 'middle', 'high', 'undergraduate', 'graduate', 'other']::text[]) NOT VALID,
  ADD CONSTRAINT eligibility_enrollment_states_v2 CHECK (enrollment_states <@ ARRAY[
    'enrolled', 'on_leave', 'returning', 'incoming', 'completed', 'other']::text[]) NOT VALID;
ALTER TABLE inha_policy.opportunity_versions
  ADD CONSTRAINT opportunity_categories_v2 CHECK (categories <@ ARRAY[
    'scholarship', 'loan', 'work_study', 'living_support', 'program', 'other']::text[]) NOT VALID;
"""


def upgrade() -> None:
    op.get_bind().connection.driver_connection.execute(DDL, prepare=False)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE inha_policy.eligibility_profiles DROP CONSTRAINT IF EXISTS eligibility_academic_levels_v2,
          DROP CONSTRAINT IF EXISTS eligibility_enrollment_states_v2;
        ALTER TABLE inha_policy.opportunity_versions DROP CONSTRAINT IF EXISTS opportunity_categories_v2;
    """)
