-- INHA scholarship/opportunity ingestion: PostgreSQL 17 reference schema.
-- DEVELOPMENT / NEW EMPTY DATABASE ONLY. This file is not an operational migration.
-- This reference DDL was not executed against PostgreSQL in the authoring workspace.
-- Required extensions: pgvector (vector) and pg_trgm, installed by the database owner.
-- Python application code must additionally validate the documented JSON object shapes,
-- IANA time zones, evidence quotations, source identities, and publication decisions.
--
-- 31 tables. Per-fetch observations, semantic source versions, parser outputs, LLM runs,
-- opportunity versions, and embedding profiles have independent identities.
-- UUIDs are generated independently of titles; a corrected title must not change an ID.
-- Storage keys refer to durable object storage. Never use a transient signed URL as a key.
-- Day-only deadlines remain dates: do not invent 23:59:59 as source evidence.
-- Unknown restrictions are different from explicitly unrestricted eligibility.
-- Initial vector retrieval is EXACT. Optional HNSW commands are commented at the end.
--
-- Publication protocol:
--   1. Append a fetch_snapshot for EVERY request, including identical bytes/304/errors.
--      Store bytes once by hash; fetch the full daily body AND every file independently.
--      Build a notice_version only when semantic body/asset content changes, then seal it.
--   2. Parse immutable inputs into documents/blocks; seal each finished parser result.
--   3. Run and validate LLM extraction; seal the terminal extraction_run.
--   4. Resolve same-cycle identity and field-scoped amendments with an audit trail.
--      Assemble a DRAFT opportunity_version, mappings, field_resolutions, and search
--      chunks. Historical values/links survive; an extension edits the same stable ID.
--   5. Verify conflicts/completeness, then set publication_state/published_at and the
--      opportunity.current_version_id in ONE transaction; the outbox event commits with it.
--   6. Switch embedding_profiles.is_active only after backfilling the replacement
--      profile. Query and document embeddings must use that exact same profile.
--
-- Immutable means append a replacement version. Fixes must not modify published facts.
-- Operational job/crawl state and current-version pointers remain mutable.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
CREATE SCHEMA inha_policy;
SET LOCAL search_path = inha_policy, public, pg_catalog;

-- 01. Source identity is site + board, independent of selected category filters.
-- Category 215 belongs in crawl_config so later scope changes do not duplicate notices.
CREATE TABLE sources (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_key              text NOT NULL UNIQUE,
    name                    text NOT NULL,
    base_url                text NOT NULL,
    list_url                text NOT NULL,
    board_key               text NOT NULL,
    schedule_timezone       text NOT NULL DEFAULT 'Asia/Seoul',
    schedule_local_time     time NOT NULL DEFAULT TIME '03:00',
    enabled                 boolean NOT NULL DEFAULT true,
    crawl_config            jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_successful_run_at  timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(crawl_config) = 'object'),
    UNIQUE (base_url, board_key)
);

-- Example configuration, intentionally not inserted:
-- source_key='inha-kr-8', board_key='kr/8'
-- crawl_config={"category_keys":["215"],"category_names":{"215":"장학"}}
-- list_url='https://www.inha.ac.kr/bbs/kr/8/artclList.do?bbsClSeq=215'
-- Category filtering must inspect actual rows; global pinned notices can be unrelated.

-- 02. One scheduled run; scheduled_for is a UTC instant for the Korean daily schedule.
CREATE TABLE crawl_runs (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id               uuid NOT NULL REFERENCES sources(id),
    mode                    text NOT NULL,
    status                  text NOT NULL DEFAULT 'queued',
    scheduled_for           timestamptz NOT NULL,
    started_at              timestamptz,
    finished_at             timestamptz,
    discovered_count        integer NOT NULL DEFAULT 0 CHECK (discovered_count >= 0),
    fetched_count           integer NOT NULL DEFAULT 0 CHECK (fetched_count >= 0),
    changed_count           integer NOT NULL DEFAULT 0 CHECK (changed_count >= 0),
    failed_count            integer NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    metrics                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_summary           text,
    CHECK (mode IN ('backfill', 'daily_full', 'reconciliation', 'manual')),
    CHECK (status IN ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')),
    CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at),
    CHECK (jsonb_typeof(metrics) = 'object'),
    UNIQUE (source_id, scheduled_for, mode)
);

CREATE UNIQUE INDEX crawl_runs_one_active_per_source
    ON crawl_runs (source_id) WHERE status IN ('queued', 'running');

-- 03. Stable source article identity and mutable observation state.
CREATE TABLE notices (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id                   uuid NOT NULL REFERENCES sources(id),
    external_post_id            text NOT NULL,
    canonical_url               text NOT NULL,
    current_notice_version_id   uuid,
    first_seen_at               timestamptz NOT NULL DEFAULT now(),
    last_seen_at                timestamptz NOT NULL DEFAULT now(),
    last_checked_at             timestamptz,
    last_content_verified_at    timestamptz,
    last_http_status            smallint,
    availability_status         text NOT NULL DEFAULT 'available',
    scope_status                text NOT NULL DEFAULT 'uncertain',
    consecutive_missing_count   integer NOT NULL DEFAULT 0,
    unavailable_since           timestamptz,
    CHECK (last_http_status IS NULL OR last_http_status BETWEEN 100 AND 599),
    CHECK (availability_status IN ('available', 'uncertain', 'unavailable', 'deleted')),
    CHECK (scope_status IN ('included', 'excluded', 'uncertain')),
    CHECK (consecutive_missing_count >= 0),
    UNIQUE (source_id, external_post_id)
);

CREATE INDEX notices_source_checked_idx ON notices (source_id, last_checked_at);

-- 04. A frozen SEMANTIC source version, not a daily fetch observation.
-- content_fingerprint includes meaningful body DOM and confirmed asset bytes/membership,
-- excluding view counts, transient UI, and transient fetch failures. Same URL/file ID
-- with new bytes creates a version even if HTML is unchanged. A timeout is an observation,
-- not evidence of content deletion. Failed downloads never clear known canonical facts.
-- raw_html_sha256 hashes the unmodified response; the two hashes have different purposes.
CREATE TABLE notice_versions (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    notice_id                   uuid NOT NULL REFERENCES notices(id),
    revision_no                 integer NOT NULL CHECK (revision_no > 0),
    crawl_run_id                uuid REFERENCES crawl_runs(id),
    origin_fetch_snapshot_id    uuid,
    content_fingerprint         text,
    raw_html_sha256              text NOT NULL,
    raw_html_storage_key         text NOT NULL,
    title                       text NOT NULL,
    author_name                 text,
    department_name             text,
    category_key                text,
    category_name               text,
    is_pinned                   boolean NOT NULL DEFAULT false,
    published_on                date,
    source_published_at         timestamptz,
    source_modified_at          timestamptz,
    source_effective_on          date,
    source_effective_at          timestamptz,
    source_effective_precision   text NOT NULL DEFAULT 'unknown',
    body_html                   text NOT NULL DEFAULT '',
    body_text                   text NOT NULL DEFAULT '',
    body_markdown               text,
    source_metadata             jsonb NOT NULL DEFAULT '{}'::jsonb,
    asset_collection_status     text NOT NULL DEFAULT 'pending',
    fetched_at                  timestamptz NOT NULL DEFAULT now(),
    observed_at                 timestamptz NOT NULL DEFAULT clock_timestamp(),
    collection_started_at       timestamptz,
    collection_finished_at      timestamptz,
    sealed_at                   timestamptz,
    CHECK (content_fingerprint IS NULL OR content_fingerprint ~ '^[0-9a-f]{64}$'),
    CHECK (raw_html_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (jsonb_typeof(source_metadata) = 'object'),
    CHECK (asset_collection_status IN ('pending', 'complete', 'partial')),
    CHECK (source_effective_precision IN ('unknown', 'date', 'datetime')),
    CHECK ((source_effective_precision = 'unknown' AND source_effective_on IS NULL
            AND source_effective_at IS NULL) OR
           (source_effective_precision = 'date' AND source_effective_on IS NOT NULL
            AND source_effective_at IS NULL) OR
           (source_effective_precision = 'datetime' AND source_effective_at IS NOT NULL)),
    CHECK (collection_finished_at IS NULL OR collection_started_at IS NULL OR
           collection_finished_at >= collection_started_at),
    CHECK (sealed_at IS NULL OR
           (content_fingerprint IS NOT NULL AND asset_collection_status <> 'pending')),
    UNIQUE (notice_id, revision_no),
    UNIQUE (notice_id, id)
);

-- Compare a fetched fingerprint with the CURRENT revision for idempotence.
-- A -> B -> A is a new revision; historical equal hashes must remain possible.
CREATE INDEX notice_versions_fingerprint_idx
    ON notice_versions (notice_id, content_fingerprint);

ALTER TABLE notices ADD CONSTRAINT notices_current_version_fk
    FOREIGN KEY (id, current_notice_version_id)
    REFERENCES notice_versions (notice_id, id)
    DEFERRABLE INITIALLY DEFERRED;

-- 05. Content-addressed original/derived bytes, INCLUDING raw HTML response bodies.
-- The same poster exposed inline and as an attachment shares this row.
-- MIME must be detected from bytes; source headers can falsely say x-msdownload.
CREATE TABLE binary_assets (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sha256                  text NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    storage_key             text NOT NULL UNIQUE,
    detected_mime           text NOT NULL,
    byte_size               bigint NOT NULL CHECK (byte_size >= 0),
    width_px                integer CHECK (width_px > 0),
    height_px               integer CHECK (height_px > 0),
    page_count              integer CHECK (page_count > 0),
    technical_metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(technical_metadata) = 'object')
);

-- 06. An occurrence in a source snapshot. Failed downloads keep their URLs and names.
-- ordinal/occurrence_key describe source position, not binary identity.
-- A failed occurrence must not silently point to an old cached binary as a new success.
CREATE TABLE notice_version_assets (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    notice_version_id       uuid NOT NULL REFERENCES notice_versions(id),
    occurrence_key          text NOT NULL,
    ordinal                 integer NOT NULL CHECK (ordinal >= 0),
    role                    text NOT NULL,
    original_url            text NOT NULL,
    resolved_url            text,
    original_filename       text,
    alt_text                text,
    dom_path                text,
    request_method          text NOT NULL DEFAULT 'GET',
    request_metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    reported_mime           text,
    etag                    text,
    last_modified_header    text,
    binary_asset_id         uuid REFERENCES binary_assets(id),
    download_status         text NOT NULL DEFAULT 'pending',
    http_status             smallint,
    error_code              text,
    error_message           text,
    attempted_at            timestamptz,
    fetch_snapshot_id       uuid,
    CHECK (role IN ('attachment', 'inline_image', 'css_image')),
    CHECK (request_method IN ('GET', 'POST')),
    CHECK (download_status IN ('pending', 'succeeded', 'failed', 'skipped')),
    CHECK ((download_status = 'succeeded' AND binary_asset_id IS NOT NULL) OR
           (download_status <> 'succeeded' AND binary_asset_id IS NULL)),
    CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599),
    CHECK (jsonb_typeof(request_metadata) = 'object'),
    UNIQUE (notice_version_id, occurrence_key),
    UNIQUE (notice_version_id, id)
);

CREATE INDEX notice_version_assets_binary_idx ON notice_version_assets (binary_asset_id);

-- 07. One parser result/attempt, tied to the exact source snapshot and occurrence.
-- Body HTML has asset_occurrence_id NULL. Images inside HWP/PDF etc. are block assets.
-- parser_config_sha256 covers OCR engine/language/layout options, not just package name.
CREATE TABLE documents (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    notice_version_id           uuid NOT NULL REFERENCES notice_versions(id),
    asset_occurrence_id         uuid,
    origin_kind                 text NOT NULL,
    parser_name                 text NOT NULL,
    parser_version              text NOT NULL,
    parser_config_sha256        text NOT NULL,
    normalizer_version          text NOT NULL,
    attempt_no                  integer NOT NULL DEFAULT 1 CHECK (attempt_no > 0),
    status                      text NOT NULL DEFAULT 'queued',
    parsed_text                 text,
    parsed_markdown             text,
    structure_storage_key       text,
    language_code               text,
    quality_flags               text[] NOT NULL DEFAULT ARRAY[]::text[],
    error_message               text,
    started_at                  timestamptz,
    finished_at                 timestamptz,
    sealed_at                   timestamptz,
    CHECK (origin_kind IN ('html_body', 'asset')),
    CHECK ((origin_kind = 'html_body' AND asset_occurrence_id IS NULL) OR
           (origin_kind = 'asset' AND asset_occurrence_id IS NOT NULL)),
    CHECK (parser_config_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (status IN ('queued', 'running', 'succeeded', 'partial', 'failed',
                      'unsupported', 'encrypted')),
    CHECK (sealed_at IS NULL OR
           (finished_at IS NOT NULL AND status NOT IN ('queued', 'running'))),
    CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at),
    FOREIGN KEY (notice_version_id, asset_occurrence_id)
        REFERENCES notice_version_assets (notice_version_id, id),
    UNIQUE NULLS NOT DISTINCT
        (notice_version_id, asset_occurrence_id, parser_name, parser_version,
         parser_config_sha256, normalizer_version, attempt_no),
    UNIQUE (notice_version_id, id)
);

CREATE INDEX documents_snapshot_status_idx ON documents (notice_version_id, status);

-- 08. Stable provenance units for field extraction and search.
-- source_path: HTML DOM locator, HWPX section/paragraph/cell path, or parser locator.
-- table_data example: {"columns":[...],"cells":[{"row":0,"col":0,"rowspan":1,...}]}.
-- bbox is normalized [x0,y0,x1,y1] in top-left coordinates, never mixed with PDF points.
CREATE TABLE document_blocks (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id             uuid NOT NULL REFERENCES documents(id),
    parent_block_id         uuid,
    block_index             integer NOT NULL CHECK (block_index >= 0),
    block_kind              text NOT NULL,
    text_content            text NOT NULL DEFAULT '',
    markdown_content        text,
    table_data              jsonb,
    page_number             integer CHECK (page_number > 0),
    source_path             text,
    bbox                    double precision[],
    binary_asset_id         uuid REFERENCES binary_assets(id),
    ocr_used                boolean NOT NULL DEFAULT false,
    ocr_confidence          numeric(5,4) CHECK (ocr_confidence BETWEEN 0 AND 1),
    extraction_metadata     jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (block_kind IN ('heading', 'paragraph', 'list_item', 'table', 'table_cell',
                         'image', 'caption', 'footnote', 'page_header', 'page_footer', 'other')),
    CHECK (table_data IS NULL OR jsonb_typeof(table_data) = 'object'),
    CHECK (jsonb_typeof(extraction_metadata) = 'object'),
    CHECK (bbox IS NULL OR
           (array_ndims(bbox) = 1 AND array_lower(bbox, 1) = 1
            AND cardinality(bbox) = 4 AND array_position(bbox, NULL) IS NULL
            AND bbox[1] >= 0 AND bbox[2] >= 0
            AND bbox[3] <= 1 AND bbox[4] <= 1
            AND bbox[1] < bbox[3] AND bbox[2] < bbox[4])),
    UNIQUE (document_id, block_index),
    UNIQUE (document_id, id),
    FOREIGN KEY (document_id, parent_block_id)
        REFERENCES document_blocks (document_id, id)
        DEFERRABLE INITIALLY DEFERRED
);

-- 09. One LLM extraction of one immutable notice snapshot, possibly yielding many items.
-- One published opportunity version can combine multiple such runs through table 12.
-- input_manifest lists EXACT document/block IDs and content hashes sent to the model.
-- Never reconstruct old model input from whichever parser output happens to be current.
CREATE TABLE extraction_runs (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    notice_version_id       uuid NOT NULL REFERENCES notice_versions(id),
    schema_version          text NOT NULL,
    prompt_version          text NOT NULL,
    prompt_sha256           text NOT NULL CHECK (prompt_sha256 ~ '^[0-9a-f]{64}$'),
    provider_name           text NOT NULL,
    model_id                text NOT NULL,
    processing_code_version text NOT NULL,
    model_parameters        jsonb NOT NULL DEFAULT '{}'::jsonb,
    input_manifest          jsonb NOT NULL,
    input_snapshot_sha256   text NOT NULL CHECK (input_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    status                  text NOT NULL DEFAULT 'queued',
    raw_response            jsonb,
    parsed_output           jsonb,
    validation_errors       jsonb NOT NULL DEFAULT '[]'::jsonb,
    quality_flags           text[] NOT NULL DEFAULT ARRAY[]::text[],
    input_tokens            bigint CHECK (input_tokens >= 0),
    output_tokens           bigint CHECK (output_tokens >= 0),
    estimated_cost          numeric(16,8) CHECK (estimated_cost >= 0),
    cost_currency           text CHECK (cost_currency ~ '^[A-Z]{3}$'),
    error_code              text,
    error_message           text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    started_at              timestamptz,
    finished_at             timestamptz,
    sealed_at               timestamptz,
    CHECK (status IN ('queued', 'running', 'succeeded', 'validation_failed',
                      'failed', 'refused', 'incomplete')),
    CHECK (jsonb_typeof(model_parameters) = 'object'),
    CHECK (jsonb_typeof(input_manifest) = 'object'),
    CHECK (parsed_output IS NULL OR jsonb_typeof(parsed_output) = 'object'),
    CHECK (jsonb_typeof(validation_errors) = 'array'),
    CHECK (status <> 'succeeded' OR parsed_output IS NOT NULL),
    CHECK (sealed_at IS NULL OR
           (finished_at IS NOT NULL AND status NOT IN ('queued', 'running'))),
    CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at),
    UNIQUE (id, notice_version_id)
);

CREATE INDEX extraction_runs_snapshot_idx ON extraction_runs (notice_version_id, created_at);

-- 10. Stable API identity for a particular scholarship/program/cycle.
-- Separate annual/semester cycles should not be merged solely by fuzzy title similarity.
CREATE TABLE opportunities (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    program_key             text,
    current_version_id      uuid,
    lifecycle_status        text NOT NULL DEFAULT 'active',
    merged_into_id          uuid REFERENCES opportunities(id),
    last_identity_decision_id uuid,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    CHECK (lifecycle_status IN ('active', 'inactive', 'merged')),
    CHECK ((lifecycle_status = 'merged' AND merged_into_id IS NOT NULL) OR
           (lifecycle_status <> 'merged' AND merged_into_id IS NULL)),
    CHECK (merged_into_id IS NULL OR merged_into_id <> id)
);

-- program_key optionally groups recurring editions; it is not a unique recruitment ID.
CREATE INDEX opportunities_program_key_idx ON opportunities (program_key);

-- 11. Queryable typed facts plus constrained extension objects.
-- Contacts shape: [{"name":null,"department":null,"phone":null,"email":null,"hours":null}].
-- Required documents: [{"name":"...","required":true,"condition":null,"asset_id":null}].
-- Links: [{"kind":"application|provider|reference","url":"...","label":"..."}].
-- Full nested JSON validation belongs to versioned Pydantic models; SQL checks containers.
CREATE TABLE opportunity_versions (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_id              uuid NOT NULL REFERENCES opportunities(id),
    version_no                  integer NOT NULL CHECK (version_no > 0),
    supersedes_version_id       uuid,
    identity_decision_id         uuid,
    edit_kind                   text NOT NULL DEFAULT 'initial',
    update_scope_mode           text NOT NULL DEFAULT 'patch',
    cycle_key                   text,
    schema_version              text NOT NULL,
    publication_state           text NOT NULL DEFAULT 'draft',
    title                       text NOT NULL,
    summary                     text,
    opportunity_kind            text NOT NULL DEFAULT 'scholarship',
    categories                  text[] NOT NULL DEFAULT ARRAY[]::text[],
    tags                        text[] NOT NULL DEFAULT ARRAY[]::text[],
    provider_name               text,
    administrator_name          text,
    academic_year               integer CHECK (academic_year BETWEEN 1900 AND 2200),
    academic_term               text,
    round_label                 text,
    support_summary             text,
    eligibility_summary         text,
    application_instructions    text,
    selection_process           text,
    selection_capacity          integer CHECK (selection_capacity >= 0),
    selection_capacity_scope    text,
    source_status_override      text,
    required_documents          jsonb NOT NULL DEFAULT '[]'::jsonb,
    contacts                    jsonb NOT NULL DEFAULT '[]'::jsonb,
    links                       jsonb NOT NULL DEFAULT '[]'::jsonb,
    additional_details          jsonb NOT NULL DEFAULT '{}'::jsonb,
    data_quality_status         text NOT NULL DEFAULT 'partial',
    quality_flags               text[] NOT NULL DEFAULT ARRAY[]::text[],
    unresolved_field_paths      text[] NOT NULL DEFAULT ARRAY[]::text[],
    extraction_coverage         jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_at                 timestamptz NOT NULL DEFAULT clock_timestamp(),
    source_effective_on          date,
    source_effective_at          timestamptz,
    source_effective_precision   text NOT NULL DEFAULT 'unknown',
    created_at                  timestamptz NOT NULL DEFAULT now(),
    published_at                timestamptz,
    CHECK (publication_state IN ('draft', 'published', 'rejected')),
    CHECK (edit_kind IN ('initial', 'in_place_edit', 'extension', 'correction',
                         'cancellation', 'reopened', 'new_round', 'merge', 'split',
                         'unlink', 'restore', 'other')),
    CHECK (update_scope_mode IN ('patch', 'replacement', 'unknown')),
    CHECK (source_effective_precision IN ('unknown', 'date', 'datetime')),
    CHECK ((source_effective_precision = 'unknown' AND source_effective_on IS NULL
            AND source_effective_at IS NULL) OR
           (source_effective_precision = 'date' AND source_effective_on IS NOT NULL
            AND source_effective_at IS NULL) OR
           (source_effective_precision = 'datetime' AND source_effective_at IS NOT NULL)),
    CHECK ((publication_state = 'published') = (published_at IS NOT NULL)),
    CHECK (opportunity_kind IN ('scholarship', 'education', 'employment',
                                'housing', 'support', 'event', 'grant', 'loan', 'other')),
    CHECK (academic_term IS NULL OR academic_term IN
           ('spring', 'summer', 'fall', 'winter', 'annual', 'other', 'unknown')),
    CHECK (source_status_override IS NULL OR source_status_override IN
           ('cancelled', 'suspended', 'closed_by_source')),
    CHECK (data_quality_status IN ('complete', 'partial', 'needs_review')),
    CHECK (data_quality_status <> 'complete' OR cardinality(unresolved_field_paths) = 0),
    CHECK (jsonb_typeof(required_documents) = 'array'),
    CHECK (jsonb_typeof(contacts) = 'array'),
    CHECK (jsonb_typeof(links) = 'array'),
    CHECK (jsonb_typeof(additional_details) = 'object'),
    CHECK (jsonb_typeof(extraction_coverage) = 'object'),
    UNIQUE (opportunity_id, version_no),
    UNIQUE (opportunity_id, id),
    FOREIGN KEY (opportunity_id, supersedes_version_id)
        REFERENCES opportunity_versions (opportunity_id, id)
        DEFERRABLE INITIALLY DEFERRED
);

ALTER TABLE opportunities ADD CONSTRAINT opportunities_current_version_fk
    FOREIGN KEY (id, current_version_id)
    REFERENCES opportunity_versions (opportunity_id, id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX opportunity_versions_title_trgm_idx
    ON opportunity_versions USING gin (title public.gin_trgm_ops);
CREATE INDEX opportunity_versions_categories_idx
    ON opportunity_versions USING gin (categories);

-- 12. M:N sources/extractions. One extraction snapshot is selected per source snapshot
-- for this published version; the same run can contribute multiple distinct opportunities.
-- An extension notice is a separate source with relation_kind='extension'.
CREATE TABLE opportunity_version_sources (
    opportunity_version_id  uuid NOT NULL REFERENCES opportunity_versions(id),
    notice_version_id       uuid NOT NULL REFERENCES notice_versions(id),
    extraction_run_id       uuid NOT NULL,
    extraction_item_path    text NOT NULL,
    relation_kind           text NOT NULL DEFAULT 'original',
    relationship_status     text NOT NULL DEFAULT 'confirmed',
    relationship_note       text,
    identity_decision_id    uuid,
    source_usage           text NOT NULL DEFAULT 'current',
    is_current_dependency  boolean NOT NULL DEFAULT true,
    requires_live_source   boolean NOT NULL DEFAULT true,
    dependency_reason      text,
    PRIMARY KEY (opportunity_version_id, notice_version_id),
    UNIQUE (opportunity_version_id, notice_version_id, extraction_run_id),
    FOREIGN KEY (extraction_run_id, notice_version_id)
        REFERENCES extraction_runs (id, notice_version_id),
    CHECK (left(extraction_item_path, 1) = '/'),
    CHECK (relation_kind IN ('original', 'correction', 'extension', 'cancellation',
                             'supplement', 'duplicate', 'reopened')),
    CHECK (relationship_status IN ('confirmed', 'candidate')),
    CHECK (source_usage IN ('current', 'supporting_snapshot', 'superseded', 'context')),
    CHECK (NOT requires_live_source OR is_current_dependency),
    CHECK (source_usage NOT IN ('superseded', 'context') OR
           (NOT is_current_dependency AND NOT requires_live_source)),
    CHECK ((is_current_dependency AND requires_live_source) OR dependency_reason IS NOT NULL)
);

CREATE INDEX opportunity_version_sources_notice_idx
    ON opportunity_version_sources (notice_version_id);

-- Supporting old eligibility can remain watched for later edits (true/false) while an
-- old source's 404 does not invalidate the current extension. Fully replaced/context-only
-- sources use false/false. Only one semantic version per notice is a current dependency;
-- older same-notice versions are historical, not simultaneous current conflict candidates.

-- 13. Multiple application/nomination/document-delivery windows.
-- "2026-10-08 17:00" => date + time + minute. "2026-10-08까지" => date + NULL + date.
-- Conflicting dates remain evidence candidates; the affected canonical bound stays NULL.
-- Open/closed status is computed at query time in timezone; NULL end is not "always open".
CREATE TABLE application_windows (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_version_id  uuid NOT NULL REFERENCES opportunity_versions(id),
    window_key              text NOT NULL,
    window_kind             text NOT NULL DEFAULT 'application',
    phase_label             text,
    application_authority   text NOT NULL DEFAULT 'unknown',
    audience_scope          text,
    track_key               text,
    scope_key               text NOT NULL DEFAULT 'main',
    channel                 text,
    start_date              date,
    start_time              time,
    start_precision         text NOT NULL DEFAULT 'unknown',
    end_date                date,
    end_time                time,
    end_precision           text NOT NULL DEFAULT 'unknown',
    timezone                text NOT NULL DEFAULT 'Asia/Seoul',
    start_inclusive         boolean NOT NULL DEFAULT true,
    end_inclusive           boolean NOT NULL DEFAULT true,
    closing_rule            text NOT NULL DEFAULT 'unknown',
    confirmation_status     text NOT NULL DEFAULT 'unknown',
    raw_text                text,
    conditions_text         text,
    CHECK (window_kind IN ('application', 'nomination', 'document_delivery',
                            'additional_application', 'program', 'event', 'interview',
                            'result', 'payment', 'other')),
    CHECK (channel IS NULL OR channel IN ('online', 'email', 'postal', 'visit', 'other')),
    CHECK (application_authority IN ('provider', 'university', 'department', 'other', 'unknown')),
    CHECK (start_precision IN ('unknown', 'date', 'minute', 'second')),
    CHECK (end_precision IN ('unknown', 'date', 'minute', 'second')),
    CHECK (
        (start_precision = 'unknown' AND start_date IS NULL AND start_time IS NULL) OR
        (start_precision = 'date' AND start_date IS NOT NULL AND start_time IS NULL) OR
        (start_precision IN ('minute', 'second') AND
         start_date IS NOT NULL AND start_time IS NOT NULL)
    ),
    CHECK (
        (end_precision = 'unknown' AND end_date IS NULL AND end_time IS NULL) OR
        (end_precision = 'date' AND end_date IS NOT NULL AND end_time IS NULL) OR
        (end_precision IN ('minute', 'second') AND
         end_date IS NOT NULL AND end_time IS NOT NULL)
    ),
    CHECK (start_precision <> 'minute' OR extract(second FROM start_time) = 0),
    CHECK (end_precision <> 'minute' OR extract(second FROM end_time) = 0),
    CHECK (start_date IS NULL OR end_date IS NULL OR start_date <= end_date),
    CHECK (start_date IS DISTINCT FROM end_date OR start_time IS NULL OR
           end_time IS NULL OR start_time <= end_time),
    CHECK (closing_rule IN ('fixed', 'rolling', 'until_budget_exhausted',
                            'until_filled', 'unknown')),
    CHECK (confirmation_status IN ('confirmed', 'conflicting', 'unknown')),
    UNIQUE (opportunity_version_id, window_key)
);

CREATE INDEX application_windows_end_idx ON application_windows (end_date, end_time);
CREATE INDEX application_windows_version_idx ON application_windows (opportunity_version_id);

-- 14. Monetary and nonmonetary support can coexist in a single opportunity.
-- Fixed amounts use identical min/max. Full tuition is usually formula/variable, not zero.
CREATE TABLE benefits (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_version_id  uuid NOT NULL REFERENCES opportunity_versions(id),
    benefit_key             text NOT NULL,
    benefit_kind            text NOT NULL,
    amount_kind             text NOT NULL DEFAULT 'unknown',
    amount_min              numeric(18,2) CHECK (amount_min >= 0),
    amount_max              numeric(18,2) CHECK (amount_max >= 0),
    currency                text CHECK (currency ~ '^[A-Z]{3}$'),
    percentage              numeric(8,4) CHECK (percentage BETWEEN 0 AND 100),
    percentage_basis        text,
    payment_frequency       text,
    beneficiary_unit        text,
    formula_text            text,
    conditions_text         text,
    duplication_rules_text  text,
    raw_text                text,
    CHECK (benefit_kind IN ('tuition', 'living_cost', 'travel', 'cash', 'in_kind',
                            'service', 'other', 'unknown')),
    CHECK (amount_kind IN ('fixed', 'maximum', 'range', 'percentage', 'formula',
                           'variable', 'unknown')),
    CHECK (amount_min IS NULL OR amount_max IS NULL OR amount_min <= amount_max),
    CHECK (amount_kind <> 'fixed' OR
           (amount_min IS NOT NULL AND amount_max IS NOT NULL AND amount_min = amount_max)),
    CHECK (amount_kind <> 'maximum' OR amount_max IS NOT NULL),
    CHECK (amount_kind <> 'range' OR (amount_min IS NOT NULL AND amount_max IS NOT NULL)),
    CHECK (amount_kind <> 'percentage' OR percentage IS NOT NULL),
    CHECK ((amount_min IS NULL AND amount_max IS NULL) OR currency IS NOT NULL),
    CHECK (payment_frequency IS NULL OR payment_frequency IN
           ('once', 'per_month', 'per_semester', 'per_year', 'other', 'unknown')),
    UNIQUE (opportunity_version_id, benefit_key)
);

CREATE INDEX benefits_version_idx ON benefits (opportunity_version_id);

-- 15. Typed projections for common filters + full versioned eligibility expression tree.
-- Rule states: unknown (not evidenced), unrestricted (explicitly no restriction),
-- restricted (a condition exists). Empty arrays do not mean unrestricted.
-- *_projection_is_exact means the scalars express that entire dimension, not one OR arm.
-- Example GPA: cumulative >=3.5/4.5 OR >=3.3/4.3 => gpa_rule_status='restricted',
-- gpa_projection_is_exact=false, GPA scalar columns NULL, full alternatives in rules.
-- Never convert 4.3 to 4.5 without an explicit source conversion method.
CREATE TABLE eligibility_profiles (
    opportunity_version_id      uuid PRIMARY KEY REFERENCES opportunity_versions(id),
    university_rule_status      text NOT NULL DEFAULT 'unknown',
    eligible_universities       text[] NOT NULL DEFAULT ARRAY[]::text[],
    academic_level_rule_status  text NOT NULL DEFAULT 'unknown',
    academic_levels             text[] NOT NULL DEFAULT ARRAY[]::text[],
    enrollment_rule_status      text NOT NULL DEFAULT 'unknown',
    enrollment_states           text[] NOT NULL DEFAULT ARRAY[]::text[],
    grade_min                   smallint CHECK (grade_min >= 1),
    grade_max                   smallint CHECK (grade_max >= 1),
    age_rule_status             text NOT NULL DEFAULT 'unknown',
    age_min                     smallint CHECK (age_min BETWEEN 0 AND 120),
    age_max                     smallint CHECK (age_max BETWEEN 0 AND 120),
    age_basis                   text,
    gpa_rule_status             text NOT NULL DEFAULT 'unknown',
    gpa_projection_is_exact     boolean NOT NULL DEFAULT false,
    gpa_threshold               numeric(6,3),
    gpa_operator                text,
    gpa_scale                   numeric(6,3),
    gpa_basis                   text,
    credit_rule_status          text NOT NULL DEFAULT 'unknown',
    credit_projection_is_exact  boolean NOT NULL DEFAULT false,
    credits_min                 numeric(6,1) CHECK (credits_min >= 0),
    credits_basis               text,
    income_rule_status          text NOT NULL DEFAULT 'unknown',
    income_projection_is_exact  boolean NOT NULL DEFAULT false,
    income_metric               text,
    income_min                  numeric(16,4),
    income_max                  numeric(16,4),
    income_reference_year       integer,
    income_currency             text CHECK (income_currency ~ '^[A-Z]{3}$'),
    residency_rule_status       text NOT NULL DEFAULT 'unknown',
    residency_projection_is_exact boolean NOT NULL DEFAULT false,
    residency_subject           text,
    residency_region_codes      text[] NOT NULL DEFAULT ARRAY[]::text[],
    residency_min_months        integer CHECK (residency_min_months >= 0),
    residency_as_of             date,
    employment_rule_status      text NOT NULL DEFAULT 'unknown',
    employment_states           text[] NOT NULL DEFAULT ARRAY[]::text[],
    original_eligibility_text   text NOT NULL DEFAULT '',
    residual_conditions_text   text,
    rule_schema_version         text NOT NULL DEFAULT 'eligibility-1.0',
    rules                       jsonb NOT NULL DEFAULT '{"status":"unknown"}'::jsonb,
    machine_evaluation_supported boolean NOT NULL DEFAULT false,
    quality_flags               text[] NOT NULL DEFAULT ARRAY[]::text[],
    CHECK (university_rule_status IN ('unknown', 'unrestricted', 'restricted')),
    CHECK (academic_level_rule_status IN ('unknown', 'unrestricted', 'restricted')),
    CHECK (enrollment_rule_status IN ('unknown', 'unrestricted', 'restricted')),
    CHECK (age_rule_status IN ('unknown', 'unrestricted', 'restricted')),
    CHECK (gpa_rule_status IN ('unknown', 'unrestricted', 'restricted')),
    CHECK (credit_rule_status IN ('unknown', 'unrestricted', 'restricted')),
    CHECK (income_rule_status IN ('unknown', 'unrestricted', 'restricted')),
    CHECK (residency_rule_status IN ('unknown', 'unrestricted', 'restricted')),
    CHECK (employment_rule_status IN ('unknown', 'unrestricted', 'restricted')),
    CHECK (grade_min IS NULL OR grade_max IS NULL OR grade_min <= grade_max),
    CHECK (age_min IS NULL OR age_max IS NULL OR age_min <= age_max),
    CHECK (age_basis IS NULL OR age_basis IN
           ('international_age', 'year_age', 'birthdate_range', 'other', 'unknown')),
    CHECK (gpa_operator IS NULL OR gpa_operator IN ('>=', '>', '=', '<=', '<')),
    CHECK (gpa_basis IS NULL OR gpa_basis IN
           ('previous_semester', 'cumulative', 'admission', 'high_school', 'other')),
    CHECK (gpa_threshold IS NULL OR gpa_threshold >= 0),
    CHECK (gpa_scale IS NULL OR gpa_scale > 0),
    CHECK (gpa_threshold IS NULL OR gpa_scale IS NULL OR gpa_threshold <= gpa_scale),
    CHECK (
        (gpa_projection_is_exact AND gpa_rule_status = 'restricted'
         AND gpa_threshold IS NOT NULL AND gpa_operator IS NOT NULL
         AND gpa_scale IS NOT NULL AND gpa_basis IS NOT NULL) OR
        (NOT gpa_projection_is_exact AND gpa_threshold IS NULL AND gpa_operator IS NULL
         AND gpa_scale IS NULL AND gpa_basis IS NULL)
    ),
    CHECK (
        (credit_projection_is_exact AND credit_rule_status = 'restricted'
         AND credits_min IS NOT NULL AND credits_basis IS NOT NULL) OR
        (NOT credit_projection_is_exact AND credits_min IS NULL AND credits_basis IS NULL)
    ),
    CHECK (credits_basis IS NULL OR credits_basis IN
           ('previous_semester', 'cumulative', 'current_semester', 'other')),
    CHECK (income_metric IS NULL OR income_metric IN
           ('kosaf_support_bracket', 'median_income_percentage',
            'household_income_amount', 'other')),
    CHECK (income_min IS NULL OR income_max IS NULL OR income_min <= income_max),
    CHECK (income_min IS NULL OR income_min >= 0),
    CHECK (income_max IS NULL OR income_max >= 0),
    CHECK (NOT income_projection_is_exact OR
           (income_rule_status = 'restricted' AND income_metric IS NOT NULL
            AND income_metric <> 'other' AND (income_min IS NOT NULL OR income_max IS NOT NULL))),
    CHECK (income_metric IS DISTINCT FROM 'household_income_amount' OR income_currency IS NOT NULL),
    CHECK (income_projection_is_exact OR
           (income_metric IS NULL AND income_min IS NULL AND income_max IS NULL
            AND income_reference_year IS NULL AND income_currency IS NULL)),
    CHECK (residency_subject IS NULL OR residency_subject IN
           ('self', 'parent', 'self_or_parent', 'household', 'other')),
    CHECK (NOT residency_projection_is_exact OR
           (residency_rule_status = 'restricted' AND residency_subject IS NOT NULL
            AND cardinality(residency_region_codes) > 0)),
    CHECK (residency_projection_is_exact OR
           (residency_subject IS NULL AND cardinality(residency_region_codes) = 0
            AND residency_min_months IS NULL AND residency_as_of IS NULL)),
    CHECK (university_rule_status = 'restricted' OR cardinality(eligible_universities) = 0),
    CHECK (academic_level_rule_status = 'restricted' OR cardinality(academic_levels) = 0),
    CHECK (enrollment_rule_status = 'restricted' OR cardinality(enrollment_states) = 0),
    CHECK (employment_rule_status = 'restricted' OR cardinality(employment_states) = 0),
    CHECK (jsonb_typeof(rules) = 'object')
);

CREATE INDEX eligibility_academic_levels_idx
    ON eligibility_profiles USING gin (academic_levels);
CREATE INDEX eligibility_enrollment_states_idx
    ON eligibility_profiles USING gin (enrollment_states);
CREATE INDEX eligibility_income_idx
    ON eligibility_profiles (income_metric, income_max)
    WHERE income_projection_is_exact;

-- 16. Field-level evidence AND conflicting candidate values.
-- The composite FKs ensure the block actually belongs to the mapped source snapshot/run.
-- Store both deadline candidates when body and attachment disagree; do not silently
-- prefer one format. accepted requires application verification of the actual quotation.
-- field_path is an application-defined JSON Pointer into the frozen API version.
CREATE TABLE field_evidence (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_version_id  uuid NOT NULL,
    notice_version_id       uuid NOT NULL,
    extraction_run_id       uuid NOT NULL,
    document_id             uuid NOT NULL,
    block_id                uuid NOT NULL,
    field_path              text NOT NULL CHECK (left(field_path, 1) = '/'),
    scope_key               text NOT NULL DEFAULT 'main',
    resolution_id           uuid,
    candidate_value         jsonb NOT NULL,
    quote_text              text NOT NULL,
    assertion_kind          text NOT NULL DEFAULT 'explicit',
    candidate_status        text NOT NULL DEFAULT 'candidate',
    verification_status     text NOT NULL DEFAULT 'pending',
    conflict_group_key      text,
    resolution_reason       text,
    CHECK (assertion_kind IN ('explicit', 'explicit_correction', 'derived', 'inferred')),
    CHECK (candidate_status IN ('accepted', 'candidate', 'conflicting', 'superseded', 'rejected')),
    CHECK (verification_status IN ('pending', 'verified', 'failed')),
    CHECK (candidate_status <> 'accepted' OR verification_status = 'verified'),
    CHECK (candidate_status <> 'superseded' OR resolution_id IS NOT NULL),
    UNIQUE (opportunity_version_id, field_path, scope_key, id),
    FOREIGN KEY (opportunity_version_id, notice_version_id, extraction_run_id)
        REFERENCES opportunity_version_sources
            (opportunity_version_id, notice_version_id, extraction_run_id),
    FOREIGN KEY (notice_version_id, document_id)
        REFERENCES documents (notice_version_id, id),
    FOREIGN KEY (document_id, block_id)
        REFERENCES document_blocks (document_id, id)
);

CREATE INDEX field_evidence_field_idx ON field_evidence (opportunity_version_id, field_path);
CREATE INDEX field_evidence_block_idx ON field_evidence (block_id);

-- 17. Embedding identity includes preprocessing, not just model name.
-- The profile records vector dimensions. The unconstrained vector column permits a new
-- dimension without rewriting historical rows; application queries must bind one profile.
CREATE TABLE embedding_profiles (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_key             text NOT NULL UNIQUE,
    model_id                text NOT NULL,
    dimensions              smallint NOT NULL DEFAULT 1536 CHECK (dimensions > 0),
    distance_metric         text NOT NULL DEFAULT 'cosine' CHECK (distance_metric = 'cosine'),
    input_template_version  text NOT NULL,
    chunker_version         text NOT NULL,
    tokenizer_version       text NOT NULL,
    config_sha256           text NOT NULL UNIQUE CHECK (config_sha256 ~ '^[0-9a-f]{64}$'),
    config                  jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_active               boolean NOT NULL DEFAULT false,
    created_at              timestamptz NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(config) = 'object')
);

CREATE UNIQUE INDEX embedding_profiles_one_active_idx
    ON embedding_profiles (is_active) WHERE is_active;

-- 18. Search material tied to an EXACT published opportunity version and model profile.
-- lexical_tokens is tokenized with the same Korean analyzer at ingestion/query time.
-- Use title/organization context plus source chunks; do not embed only an LLM summary.
-- Rows may be staged while the opportunity version is still a draft.
CREATE TABLE search_chunks (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_version_id  uuid NOT NULL REFERENCES opportunity_versions(id),
    embedding_profile_id    uuid NOT NULL REFERENCES embedding_profiles(id),
    chunk_key               text NOT NULL,
    chunk_kind              text NOT NULL,
    evidence_role           text NOT NULL DEFAULT 'current',
    superseded_field_paths  text[] NOT NULL DEFAULT ARRAY[]::text[],
    canonical_overrides     jsonb NOT NULL DEFAULT '[]'::jsonb,
    applied_resolution_ids  uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],
    ordinal                 integer NOT NULL CHECK (ordinal >= 0),
    chunk_text              text NOT NULL CHECK (length(chunk_text) > 0),
    input_sha256            text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    token_count             integer CHECK (token_count >= 0),
    lexical_tokens          text NOT NULL DEFAULT '',
    search_tsv              tsvector GENERATED ALWAYS AS
                                (to_tsvector('pg_catalog.simple'::regconfig, lexical_tokens)) STORED,
    embedding               public.vector,
    embedding_status        text NOT NULL DEFAULT 'pending',
    embedding_created_at    timestamptz,
    embedding_error         text,
    chunk_metadata          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    CHECK (chunk_kind IN ('summary', 'body', 'attachment', 'ocr', 'table')),
    CHECK (evidence_role IN ('current', 'historical', 'mixed')),
    CHECK (jsonb_typeof(canonical_overrides) = 'array'),
    CHECK (evidence_role <> 'current' OR cardinality(superseded_field_paths) = 0),
    CHECK (evidence_role <> 'mixed' OR
           (cardinality(superseded_field_paths) > 0 AND
            jsonb_array_length(canonical_overrides) > 0 AND
            cardinality(applied_resolution_ids) > 0)),
    CHECK (embedding_status IN ('pending', 'succeeded', 'failed')),
    CHECK ((embedding_status = 'succeeded' AND embedding IS NOT NULL
            AND embedding_created_at IS NOT NULL) OR
           (embedding_status <> 'succeeded' AND embedding IS NULL
            AND embedding_created_at IS NULL)),
    CHECK (jsonb_typeof(chunk_metadata) = 'object'),
    UNIQUE (opportunity_version_id, embedding_profile_id, chunk_key),
    UNIQUE (id, opportunity_version_id)
);

CREATE INDEX search_chunks_version_profile_idx
    ON search_chunks (opportunity_version_id, embedding_profile_id);
CREATE INDEX search_chunks_lexical_idx ON search_chunks USING gin (search_tsv);

-- 19. Real FK-backed provenance for a chunk spanning several source blocks.
-- char_start/char_end are optional Unicode-codepoint offsets in the stored block text.
CREATE TABLE search_chunk_blocks (
    chunk_id                uuid NOT NULL,
    opportunity_version_id  uuid NOT NULL,
    notice_version_id       uuid NOT NULL,
    document_id             uuid NOT NULL,
    block_id                uuid NOT NULL,
    part_index              integer NOT NULL CHECK (part_index >= 0),
    char_start              integer CHECK (char_start >= 0),
    char_end                integer CHECK (char_end >= 0),
    PRIMARY KEY (chunk_id, block_id, part_index),
    CHECK ((char_start IS NULL) = (char_end IS NULL)),
    CHECK (char_start IS NULL OR char_end >= char_start),
    FOREIGN KEY (chunk_id, opportunity_version_id)
        REFERENCES search_chunks (id, opportunity_version_id),
    FOREIGN KEY (opportunity_version_id, notice_version_id)
        REFERENCES opportunity_version_sources (opportunity_version_id, notice_version_id),
    FOREIGN KEY (notice_version_id, document_id)
        REFERENCES documents (notice_version_id, id),
    FOREIGN KEY (document_id, block_id)
        REFERENCES document_blocks (document_id, id)
);

CREATE INDEX search_chunk_blocks_block_idx ON search_chunk_blocks (block_id);

-- 20. Minimal durable job state. A scheduler creates one run; workers claim queued jobs.
-- job_key is a deterministic stage-specific key. A retry updates this job's attempts;
-- a new parser/model result gets a new immutable output row after a terminal attempt.
CREATE TABLE crawl_jobs (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    crawl_run_id            uuid NOT NULL REFERENCES crawl_runs(id),
    stage                   text NOT NULL,
    job_key                 text NOT NULL,
    notice_id               uuid REFERENCES notices(id),
    notice_version_id       uuid REFERENCES notice_versions(id),
    target_url              text,
    payload                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    status                  text NOT NULL DEFAULT 'queued',
    attempt_count           integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts            integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    available_at            timestamptz NOT NULL DEFAULT now(),
    worker_id               text,
    locked_at               timestamptz,
    heartbeat_at            timestamptz,
    started_at              timestamptz,
    finished_at             timestamptz,
    error_code              text,
    error_message           text,
    result_metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    CHECK (stage IN ('discover_list', 'fetch_detail', 'download_asset', 'parse_document',
                     'structure', 'embed', 'publish')),
    CHECK (status IN ('queued', 'running', 'retry', 'succeeded', 'failed', 'skipped')),
    CHECK (jsonb_typeof(payload) = 'object'),
    CHECK (jsonb_typeof(result_metadata) = 'object'),
    CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at),
    UNIQUE (crawl_run_id, stage, job_key)
);

CREATE INDEX crawl_jobs_claim_idx ON crawl_jobs (available_at, created_at)
    WHERE status IN ('queued', 'retry');

-- 21. An immutable observation for EVERY physical request, independent of semantic change.
-- A repeated 200 stores a new observation pointing to the same content-addressed bytes.
-- A 304 stores no new body, but references a verified prior representation. A failed
-- request is recorded without claiming that yesterday's bytes were verified today.
-- collection_id groups a detail request and its file requests; it is an observation
-- interval, not an atomic snapshot of a remote server. Always re-fetch files independently
-- even when the HTML is identical. Do not infer file identity from URL/file ID/filename.
-- Header JSON is an ALLOWLIST (content-type/length, etag, last-modified, date, location,
-- retry-after). Never store Authorization, Cookie, Set-Cookie or session/token headers.
CREATE TABLE fetch_snapshots (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id                   uuid NOT NULL REFERENCES sources(id),
    crawl_run_id                uuid REFERENCES crawl_runs(id),
    crawl_job_id                uuid REFERENCES crawl_jobs(id),
    collection_id               uuid NOT NULL,
    notice_id                   uuid REFERENCES notices(id),
    baseline_notice_version_id  uuid,
    parent_fetch_snapshot_id    uuid REFERENCES fetch_snapshots(id),
    resource_kind               text NOT NULL,
    resource_key                text NOT NULL,
    requested_url               text NOT NULL,
    final_url                   text,
    request_method              text NOT NULL DEFAULT 'GET',
    request_metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    response_metadata           jsonb NOT NULL DEFAULT '{}'::jsonb,
    redirect_chain              jsonb NOT NULL DEFAULT '[]'::jsonb,
    outcome                     text NOT NULL,
    http_status                 smallint,
    response_body_asset_id      uuid REFERENCES binary_assets(id),
    representation_asset_id     uuid REFERENCES binary_assets(id),
    reuses_snapshot_id          uuid REFERENCES fetch_snapshots(id),
    etag                        text,
    last_modified_header        text,
    error_code                  text,
    error_message               text,
    fetch_started_at            timestamptz NOT NULL,
    fetched_at                  timestamptz NOT NULL,
    observed_at                 timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (resource_kind IN ('list', 'detail', 'attachment', 'inline_image', 'external_resource')),
    CHECK (resource_kind = 'list' OR notice_id IS NOT NULL),
    CHECK (request_method IN ('GET', 'POST')),
    CHECK (outcome IN ('received', 'not_modified', 'http_error', 'network_error', 'invalid_content')),
    CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599),
    CHECK (fetched_at >= fetch_started_at),
    CHECK (outcome = 'network_error' OR http_status IS NOT NULL),
    CHECK (reuses_snapshot_id IS NULL OR reuses_snapshot_id <> id),
    CHECK (parent_fetch_snapshot_id IS NULL OR parent_fetch_snapshot_id <> id),
    CHECK (baseline_notice_version_id IS NULL OR notice_id IS NOT NULL),
    CHECK (
        (outcome = 'received' AND http_status BETWEEN 200 AND 299
         AND response_body_asset_id IS NOT NULL AND representation_asset_id IS NOT NULL
         AND response_body_asset_id = representation_asset_id AND reuses_snapshot_id IS NULL) OR
        (outcome = 'not_modified' AND http_status = 304 AND response_body_asset_id IS NULL
         AND representation_asset_id IS NOT NULL AND reuses_snapshot_id IS NOT NULL) OR
        (outcome IN ('http_error', 'network_error', 'invalid_content')
         AND representation_asset_id IS NULL AND reuses_snapshot_id IS NULL)
    ),
    CHECK (outcome <> 'network_error' OR http_status IS NULL),
    CHECK (outcome <> 'http_error' OR http_status BETWEEN 300 AND 599),
    CHECK (jsonb_typeof(request_metadata) = 'object'),
    CHECK (jsonb_typeof(response_metadata) = 'object'),
    CHECK (jsonb_typeof(redirect_chain) = 'array'),
    FOREIGN KEY (notice_id, baseline_notice_version_id)
        REFERENCES notice_versions (notice_id, id),
    UNIQUE (notice_id, id)
);

CREATE INDEX fetch_snapshots_notice_time_idx ON fetch_snapshots (notice_id, fetched_at DESC);
CREATE INDEX fetch_snapshots_resource_time_idx ON fetch_snapshots (source_id, resource_key, fetched_at DESC);
CREATE INDEX fetch_snapshots_collection_idx ON fetch_snapshots (collection_id, fetched_at);

ALTER TABLE notice_versions ADD CONSTRAINT notice_versions_origin_fetch_fk
    FOREIGN KEY (notice_id, origin_fetch_snapshot_id)
    REFERENCES fetch_snapshots (notice_id, id);
ALTER TABLE notice_versions ADD CONSTRAINT notice_versions_origin_when_sealed_check
    CHECK (sealed_at IS NULL OR origin_fetch_snapshot_id IS NOT NULL);
ALTER TABLE notice_version_assets ADD CONSTRAINT notice_version_assets_fetch_fk
    FOREIGN KEY (fetch_snapshot_id) REFERENCES fetch_snapshots(id);

-- 22. Append-only identity/link decisions, including mistaken links and their reversals.
-- An operation affects a pair (or one notice); use decision_batch_id for a multiway split.
-- before_state/after_state contain typed mapping/version/redirect snapshots, not destructive
-- UPDATE instructions. CONFIRMED actions create NEW opportunity versions; old mappings,
-- versions, and losing IDs remain queryable. Undo adds a decision with reverses_decision_id.
-- A new annual/semester/track round is a new opportunity, even if its title is unchanged.
CREATE TABLE identity_decisions (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_batch_id           uuid NOT NULL DEFAULT gen_random_uuid(),
    decision_kind               text NOT NULL,
    decision_status             text NOT NULL DEFAULT 'proposed',
    opportunity_id              uuid NOT NULL REFERENCES opportunities(id),
    other_opportunity_id        uuid REFERENCES opportunities(id),
    notice_id                   uuid REFERENCES notices(id),
    related_notice_id           uuid REFERENCES notices(id),
    source_notice_version_id    uuid,
    related_notice_version_id   uuid,
    supersedes_decision_id       uuid REFERENCES identity_decisions(id),
    reverses_decision_id         uuid REFERENCES identity_decisions(id),
    identity_basis              text NOT NULL DEFAULT 'insufficient',
    same_program_verified       boolean NOT NULL DEFAULT false,
    same_cycle_verified         boolean NOT NULL DEFAULT false,
    same_scope_verified         boolean NOT NULL DEFAULT false,
    amendment_kind              text NOT NULL DEFAULT 'unknown',
    update_scope_mode           text NOT NULL DEFAULT 'unknown',
    scope                       jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence                    jsonb NOT NULL DEFAULT '[]'::jsonb,
    before_state                jsonb NOT NULL DEFAULT '{}'::jsonb,
    after_state                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    rule_version                text NOT NULL,
    decision_reason             text NOT NULL,
    actor_kind                  text NOT NULL,
    actor_id                    text,
    observed_at                 timestamptz NOT NULL,
    decided_at                  timestamptz NOT NULL DEFAULT clock_timestamp(),
    source_effective_on         date,
    source_effective_at          timestamptz,
    CHECK (decision_kind IN ('link_notice', 'unlink_notice', 'merge', 'split',
                             'undo', 'new_round', 'reject_link')),
    CHECK (decision_status IN ('proposed', 'confirmed', 'rejected')),
    CHECK (identity_basis IN ('same_source_article', 'explicit_cross_reference',
                              'verified_cycle_scope', 'human_verified', 'insufficient')),
    CHECK (actor_kind IN ('deterministic_rule', 'human', 'llm_suggestion')),
    CHECK (decision_status <> 'confirmed' OR
           (actor_kind <> 'llm_suggestion' AND identity_basis <> 'insufficient')),
    CHECK (decision_status <> 'confirmed' OR decision_kind NOT IN ('link_notice', 'merge') OR
           (same_program_verified AND same_cycle_verified AND same_scope_verified)),
    CHECK (decision_kind NOT IN ('link_notice', 'unlink_notice', 'reject_link') OR notice_id IS NOT NULL),
    CHECK (decision_kind NOT IN ('merge', 'split') OR other_opportunity_id IS NOT NULL),
    CHECK (decision_kind <> 'undo' OR reverses_decision_id IS NOT NULL),
    CHECK (decision_kind <> 'new_round' OR NOT same_cycle_verified),
    CHECK (other_opportunity_id IS NULL OR other_opportunity_id <> opportunity_id),
    CHECK (supersedes_decision_id IS NULL OR supersedes_decision_id <> id),
    CHECK (reverses_decision_id IS NULL OR reverses_decision_id <> id),
    CHECK (source_notice_version_id IS NULL OR notice_id IS NOT NULL),
    CHECK (related_notice_version_id IS NULL OR related_notice_id IS NOT NULL),
    CHECK (amendment_kind IN ('extension', 'correction', 'cancellation', 'reopened',
                              'new_round', 'none', 'unknown')),
    CHECK (update_scope_mode IN ('patch', 'replacement', 'unknown')),
    CHECK (jsonb_typeof(scope) = 'object'),
    CHECK (jsonb_typeof(evidence) = 'array'),
    CHECK (jsonb_typeof(before_state) = 'object'),
    CHECK (jsonb_typeof(after_state) = 'object'),
    FOREIGN KEY (notice_id, source_notice_version_id) REFERENCES notice_versions (notice_id, id),
    FOREIGN KEY (related_notice_id, related_notice_version_id) REFERENCES notice_versions (notice_id, id)
);

CREATE INDEX identity_decisions_opportunity_idx ON identity_decisions (opportunity_id, decided_at);
CREATE INDEX identity_decisions_notice_idx ON identity_decisions (notice_id, decided_at);
CREATE INDEX identity_decisions_batch_idx ON identity_decisions (decision_batch_id);

ALTER TABLE opportunities ADD CONSTRAINT opportunities_identity_decision_fk
    FOREIGN KEY (last_identity_decision_id) REFERENCES identity_decisions(id);
ALTER TABLE opportunity_versions ADD CONSTRAINT opportunity_versions_identity_decision_fk
    FOREIGN KEY (identity_decision_id) REFERENCES identity_decisions(id);
ALTER TABLE opportunity_version_sources ADD CONSTRAINT opportunity_version_sources_identity_fk
    FOREIGN KEY (identity_decision_id) REFERENCES identity_decisions(id);

-- 23. One field/scope resolution per published version, preserving every old candidate.
-- No global source-format priority. Explicit verified amendment intent plus same cycle
-- and scope can supersede stale attachments. Confidence or a later fetch timestamp alone
-- is never a precedence rule. Provider application vs school nomination are DIFFERENT scopes.
-- Same-source, same-slot historical revisions are excluded from the current candidate set
-- before resolving cross-source conflicts; an in-place edit needs no '[수정]' marker.
-- Amendment signals are field patches by default, not replacement of every policy field.
CREATE TABLE field_resolutions (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_id              uuid NOT NULL,
    opportunity_version_id      uuid NOT NULL,
    field_path                  text NOT NULL CHECK (left(field_path, 1) = '/'),
    scope_key                   text NOT NULL,
    scope                       jsonb NOT NULL,
    status                      text NOT NULL DEFAULT 'unresolved',
    selected_value              jsonb,
    selected_evidence_id        uuid,
    intent_evidence_id          uuid,
    cross_reference_evidence_id uuid,
    supersedes_resolution_id    uuid,
    rule_version                text NOT NULL,
    precedence_basis            text NOT NULL,
    same_cycle_verified         boolean NOT NULL DEFAULT false,
    same_scope_verified         boolean NOT NULL DEFAULT false,
    explicit_amendment_verified boolean NOT NULL DEFAULT false,
    pending_amendment           boolean NOT NULL DEFAULT false,
    new_value_verified          boolean NOT NULL DEFAULT false,
    reliable_cross_reference_verified boolean NOT NULL DEFAULT false,
    amendment_kind              text NOT NULL DEFAULT 'unknown',
    update_scope_mode           text NOT NULL DEFAULT 'patch',
    retain_previous_when_not_stated boolean NOT NULL DEFAULT true,
    supersession_edges          jsonb NOT NULL DEFAULT '[]'::jsonb,
    decision_reason             text NOT NULL,
    observed_at                 timestamptz NOT NULL,
    decided_at                  timestamptz NOT NULL DEFAULT clock_timestamp(),
    source_effective_on         date,
    source_effective_at          timestamptz,
    source_effective_precision  text NOT NULL DEFAULT 'unknown',
    CHECK (status IN ('agreed', 'resolved_explicit_update', 'unresolved', 'not_stated')),
    CHECK (precedence_basis IN ('agreement', 'same_source_revision', 'explicit_amendment',
                                'unresolved', 'not_stated')),
    CHECK ((status IN ('agreed', 'resolved_explicit_update') AND selected_value IS NOT NULL
            AND selected_evidence_id IS NOT NULL) OR
           (status IN ('unresolved', 'not_stated') AND selected_value IS NULL
            AND selected_evidence_id IS NULL)),
    CHECK (status <> 'resolved_explicit_update' OR
           (precedence_basis = 'explicit_amendment' AND same_cycle_verified AND same_scope_verified
            AND explicit_amendment_verified AND intent_evidence_id IS NOT NULL
            AND (new_value_verified OR (reliable_cross_reference_verified
                                       AND cross_reference_evidence_id IS NOT NULL)))),
    CHECK (reliable_cross_reference_verified = false OR cross_reference_evidence_id IS NOT NULL),
    CHECK (NOT pending_amendment OR
           (status = 'unresolved' AND same_cycle_verified AND same_scope_verified
            AND explicit_amendment_verified AND intent_evidence_id IS NOT NULL)),
    CHECK (retain_previous_when_not_stated OR update_scope_mode = 'replacement'),
    CHECK (amendment_kind IN ('extension', 'correction', 'cancellation', 'reopened',
                              'in_place_edit', 'none', 'unknown')),
    CHECK (update_scope_mode IN ('patch', 'replacement', 'unknown')),
    CHECK (jsonb_typeof(scope) = 'object'),
    CHECK (jsonb_typeof(supersession_edges) = 'array'),
    CHECK (supersedes_resolution_id IS NULL OR supersedes_resolution_id <> id),
    CHECK (source_effective_precision IN ('unknown', 'date', 'datetime')),
    CHECK ((source_effective_precision = 'unknown' AND source_effective_on IS NULL
            AND source_effective_at IS NULL) OR
           (source_effective_precision = 'date' AND source_effective_on IS NOT NULL
            AND source_effective_at IS NULL) OR
           (source_effective_precision = 'datetime' AND source_effective_at IS NOT NULL)),
    UNIQUE (opportunity_version_id, field_path, scope_key),
    UNIQUE (opportunity_version_id, field_path, scope_key, id),
    UNIQUE (opportunity_id, field_path, scope_key, id),
    FOREIGN KEY (opportunity_id, opportunity_version_id)
        REFERENCES opportunity_versions (opportunity_id, id),
    FOREIGN KEY (opportunity_version_id, field_path, scope_key, selected_evidence_id)
        REFERENCES field_evidence (opportunity_version_id, field_path, scope_key, id),
    FOREIGN KEY (intent_evidence_id) REFERENCES field_evidence(id),
    FOREIGN KEY (cross_reference_evidence_id) REFERENCES field_evidence(id),
    FOREIGN KEY (opportunity_id, field_path, scope_key, supersedes_resolution_id)
        REFERENCES field_resolutions (opportunity_id, field_path, scope_key, id)
);

ALTER TABLE field_evidence ADD CONSTRAINT field_evidence_resolution_fk
    FOREIGN KEY (opportunity_version_id, field_path, scope_key, resolution_id)
    REFERENCES field_resolutions (opportunity_version_id, field_path, scope_key, id)
    DEFERRABLE INITIALLY DEFERRED;

-- 24. Transactional outbox + ordered public change feed. Unchanged daily observations
-- do not emit public updates. A newly registered extension emits updated for the same
-- opportunity_id, with edit_kind='extension' and a new immutable version.
-- IMPORTANT: sequence allocation is serialized BEFORE nextval with an advisory TX lock.
-- bigserial/created_at alone is NOT a commit-safe cursor: a later ID can commit first.
-- event_seq can have rollback gaps; consumers use > cursor, never assume gap-free IDs.
CREATE SEQUENCE change_event_order_seq AS bigint;

CREATE TABLE change_events (
    event_seq                   bigint PRIMARY KEY,
    id                          uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    event_kind                  text NOT NULL,
    opportunity_id              uuid NOT NULL REFERENCES opportunities(id),
    opportunity_version_id      uuid,
    previous_version_id         uuid,
    identity_decision_id         uuid REFERENCES identity_decisions(id),
    edit_kind                   text,
    source_is_stale              boolean NOT NULL DEFAULT false,
    visible_after               boolean NOT NULL,
    observed_at                 timestamptz NOT NULL,
    recorded_at                 timestamptz NOT NULL DEFAULT clock_timestamp(),
    payload                     jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (event_kind IN ('created', 'updated', 'extended', 'corrected', 'cancelled',
                          'unpublished', 'removed', 'restored', 'merged', 'split',
                          'freshness_changed')),
    CHECK (jsonb_typeof(payload) = 'object'),
    FOREIGN KEY (opportunity_id, opportunity_version_id)
        REFERENCES opportunity_versions (opportunity_id, id),
    FOREIGN KEY (opportunity_id, previous_version_id)
        REFERENCES opportunity_versions (opportunity_id, id)
);

CREATE INDEX change_events_opportunity_idx ON change_events (opportunity_id, event_seq);

-- 25. Administrator identities are separate from public API consumers. Password hashes
-- are PHC strings (Argon2id by default); plaintext passwords are never stored.
CREATE TABLE admin_users (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email                   text NOT NULL UNIQUE,
    display_name            text NOT NULL,
    password_hash           text,
    role                    text NOT NULL DEFAULT 'viewer',
    is_active               boolean NOT NULL DEFAULT true,
    sso_subject             text UNIQUE,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    last_login_at           timestamptz,
    CHECK (role IN ('viewer', 'operator', 'reviewer', 'admin')),
    CHECK (password_hash IS NOT NULL OR sso_subject IS NOT NULL),
    CHECK (email = lower(email))
);

-- 26. Each key is overridden independently. Deleting a row immediately restores the
-- environment/default value. value_json must never contain credentials.
CREATE TABLE runtime_settings (
    setting_key             text PRIMARY KEY,
    value_json              jsonb NOT NULL,
    updated_by              uuid NOT NULL REFERENCES admin_users(id),
    updated_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (setting_key ~ '^[a-z][a-z0-9_.-]{1,127}$')
);

-- 27. Encrypted provider credentials. ciphertext contains a nonce and authenticated
-- ciphertext produced with the deployment master key; key material is never in this DB.
CREATE TABLE encrypted_secrets (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    secret_key              text NOT NULL,
    version_no              integer NOT NULL CHECK (version_no > 0),
    ciphertext              bytea NOT NULL,
    key_id                  text NOT NULL,
    fingerprint             text NOT NULL,
    masked_value            text NOT NULL,
    is_active               boolean NOT NULL DEFAULT true,
    created_by              uuid NOT NULL REFERENCES admin_users(id),
    created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    revoked_at              timestamptz,
    CHECK (secret_key ~ '^[a-z][a-z0-9_.-]{1,127}$'),
    CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    CHECK ((is_active AND revoked_at IS NULL) OR (NOT is_active AND revoked_at IS NOT NULL)),
    UNIQUE (secret_key, version_no)
);
CREATE UNIQUE INDEX encrypted_secrets_one_active_idx
    ON encrypted_secrets (secret_key) WHERE is_active;

-- 28. Quality and identity work is durable and assignable. Resolving an item does not
-- alter the source/extraction rows; the resulting decision/version is linked here.
CREATE TABLE review_items (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    review_kind             text NOT NULL,
    status                  text NOT NULL DEFAULT 'open',
    priority                smallint NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
    entity_type             text NOT NULL,
    entity_id               uuid NOT NULL,
    opportunity_id          uuid REFERENCES opportunities(id),
    notice_id               uuid REFERENCES notices(id),
    field_path              text,
    payload                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    assigned_to             uuid REFERENCES admin_users(id),
    resolution_note         text,
    resolution_entity_type  text,
    resolution_entity_id    uuid,
    created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    resolved_at             timestamptz,
    CHECK (review_kind IN ('date_conflict', 'amount_conflict', 'eligibility_conflict',
                           'identity_uncertain', 'revision_candidate', 'parsing_failed',
                           'ocr_failed', 'llm_validation_failed', 'embedding_failed',
                           'privacy_review', 'other')),
    CHECK (status IN ('open', 'in_review', 'resolved', 'dismissed')),
    CHECK (jsonb_typeof(payload) = 'object'),
    CHECK ((status IN ('resolved', 'dismissed') AND resolved_at IS NOT NULL) OR
           (status IN ('open', 'in_review') AND resolved_at IS NULL))
);
CREATE INDEX review_items_open_idx ON review_items (priority DESC, created_at)
    WHERE status IN ('open', 'in_review');

-- 29. Manual changes are append-only commands. A remove command references the set command
-- it cancels. The final-value assembler applies the latest valid command per field/scope.
CREATE TABLE manual_overrides (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_id          uuid NOT NULL REFERENCES opportunities(id),
    field_path              text NOT NULL CHECK (left(field_path, 1) = '/'),
    scope_key               text NOT NULL DEFAULT 'main',
    action                  text NOT NULL,
    value_json              jsonb,
    supersedes_override_id  uuid REFERENCES manual_overrides(id),
    actor_id                uuid NOT NULL REFERENCES admin_users(id),
    reason                  text NOT NULL CHECK (length(btrim(reason)) > 0),
    created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (action IN ('set', 'remove')),
    CHECK ((action = 'set' AND value_json IS NOT NULL) OR
           (action = 'remove' AND value_json IS NULL AND supersedes_override_id IS NOT NULL)),
    CHECK (supersedes_override_id IS NULL OR supersedes_override_id <> id)
);
CREATE INDEX manual_overrides_field_idx
    ON manual_overrides (opportunity_id, field_path, scope_key, created_at DESC);

-- 30. One audit envelope for human and automated changes. Detailed immutable source and
-- model histories remain in their own version tables; this table makes operations searchable.
CREATE TABLE audit_logs (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_kind              text NOT NULL,
    actor_id                text,
    action                  text NOT NULL,
    entity_type             text NOT NULL,
    entity_id               text NOT NULL,
    before_state            jsonb,
    after_state             jsonb,
    reason                  text,
    related_source_id       uuid REFERENCES sources(id),
    automated               boolean NOT NULL,
    request_id              text,
    created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (actor_kind IN ('admin_user', 'worker', 'scheduler', 'system')),
    CHECK (before_state IS NULL OR jsonb_typeof(before_state) IN ('object', 'array', 'null')),
    CHECK (after_state IS NULL OR jsonb_typeof(after_state) IN ('object', 'array', 'null'))
);
CREATE INDEX audit_logs_entity_idx ON audit_logs (entity_type, entity_id, created_at DESC);

-- 31. Reprocessing is independent from crawl runs and can target one entity or a versioned
-- corpus slice. target_key plus processing profile makes enqueue idempotent.
CREATE TABLE reprocessing_jobs (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_kind                text NOT NULL,
    target_type             text NOT NULL,
    target_key              text NOT NULL,
    processing_profile      jsonb NOT NULL DEFAULT '{}'::jsonb,
    status                  text NOT NULL DEFAULT 'queued',
    requested_by            uuid REFERENCES admin_users(id),
    attempt_count           integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts            integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    available_at            timestamptz NOT NULL DEFAULT now(),
    locked_at               timestamptz,
    worker_id               text,
    error_message           text,
    result_metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at             timestamptz,
    CHECK (job_kind IN ('parse', 'ocr', 'extract', 'resolve', 'embed', 'publish')),
    CHECK (status IN ('queued', 'running', 'retry', 'succeeded', 'failed', 'cancelled')),
    CHECK (jsonb_typeof(processing_profile) = 'object'),
    CHECK (jsonb_typeof(result_metadata) = 'object'),
    UNIQUE (job_kind, target_type, target_key, processing_profile)
);
CREATE INDEX reprocessing_jobs_claim_idx ON reprocessing_jobs (available_at, created_at)
    WHERE status IN ('queued', 'retry');

-- --------------------- Snapshot/publication invariants ---------------------

-- Immutable observations/decisions/events are appended only after a complete attempt
-- or decision is known. Reconsideration is a NEW row, never a rewrite of the old row.
CREATE FUNCTION prevent_append_only_change()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    RAISE EXCEPTION '% is append-only; record a new observation/decision/event', TG_TABLE_NAME;
END;
$fn$;

CREATE TRIGGER fetch_snapshots_append_only
    BEFORE UPDATE OR DELETE ON fetch_snapshots
    FOR EACH ROW EXECUTE FUNCTION prevent_append_only_change();
CREATE TRIGGER identity_decisions_append_only
    BEFORE UPDATE OR DELETE ON identity_decisions
    FOR EACH ROW EXECUTE FUNCTION prevent_append_only_change();
CREATE TRIGGER change_events_append_only
    BEFORE UPDATE OR DELETE ON change_events
    FOR EACH ROW EXECUTE FUNCTION prevent_append_only_change();
CREATE TRIGGER manual_overrides_append_only
    BEFORE UPDATE OR DELETE ON manual_overrides
    FOR EACH ROW EXECUTE FUNCTION prevent_append_only_change();
CREATE TRIGGER audit_logs_append_only
    BEFORE UPDATE OR DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION prevent_append_only_change();

CREATE FUNCTION validate_fetch_revalidation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, inha_policy, public
AS $fn$
BEGIN
    IF NEW.outcome = 'not_modified' AND NOT EXISTS (
        SELECT 1 FROM fetch_snapshots previous
        WHERE previous.id = NEW.reuses_snapshot_id
          AND previous.source_id = NEW.source_id
          AND previous.resource_key = NEW.resource_key
          AND previous.request_method = NEW.request_method
          AND previous.representation_asset_id = NEW.representation_asset_id
          AND previous.outcome IN ('received', 'not_modified')
          AND previous.fetched_at <= NEW.fetch_started_at
    ) THEN
        RAISE EXCEPTION '304 must revalidate a prior successful representation of this resource';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER fetch_snapshots_validate_revalidation
    BEFORE INSERT ON fetch_snapshots
    FOR EACH ROW EXECUTE FUNCTION validate_fetch_revalidation();

-- Deny changing/deleting a finalized parent row. Operational pointers live elsewhere.
CREATE FUNCTION prevent_finalized_row_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, inha_policy, public
AS $fn$
BEGIN
    IF to_jsonb(OLD) ->> TG_ARGV[0] IS NOT NULL THEN
        RAISE EXCEPTION '% is finalized; append a new version instead', TG_TABLE_NAME;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER notice_versions_immutable
    BEFORE UPDATE OR DELETE ON notice_versions
    FOR EACH ROW EXECUTE FUNCTION prevent_finalized_row_change('sealed_at');
CREATE TRIGGER documents_immutable
    BEFORE UPDATE OR DELETE ON documents
    FOR EACH ROW EXECUTE FUNCTION prevent_finalized_row_change('sealed_at');
CREATE TRIGGER extraction_runs_immutable
    BEFORE UPDATE OR DELETE ON extraction_runs
    FOR EACH ROW EXECUTE FUNCTION prevent_finalized_row_change('sealed_at');
CREATE TRIGGER opportunity_versions_immutable
    BEFORE UPDATE OR DELETE ON opportunity_versions
    FOR EACH ROW EXECUTE FUNCTION prevent_finalized_row_change('published_at');
CREATE TRIGGER search_chunks_immutable_when_embedded
    BEFORE UPDATE OR DELETE ON search_chunks
    FOR EACH ROW EXECUTE FUNCTION prevent_finalized_row_change('embedding_created_at');

-- A stable profile/blob ID must never acquire a different mathematical/binary identity.
-- Descriptive metadata and profile activation remain mutable.
CREATE FUNCTION prevent_identity_column_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, inha_policy, public
AS $fn$
DECLARE
    column_name text;
BEGIN
    FOREACH column_name IN ARRAY TG_ARGV LOOP
        IF (to_jsonb(OLD) -> column_name) IS DISTINCT FROM (to_jsonb(NEW) -> column_name) THEN
            RAISE EXCEPTION '%.% is immutable; create a new identity', TG_TABLE_NAME, column_name;
        END IF;
    END LOOP;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER binary_assets_identity_immutable
    BEFORE UPDATE ON binary_assets
    FOR EACH ROW EXECUTE FUNCTION prevent_identity_column_change
        ('sha256', 'storage_key', 'byte_size', 'detected_mime');
CREATE TRIGGER embedding_profiles_identity_immutable
    BEFORE UPDATE ON embedding_profiles
    FOR EACH ROW EXECUTE FUNCTION prevent_identity_column_change
        ('model_id', 'dimensions', 'distance_metric', 'input_template_version',
         'chunker_version', 'tokenizer_version', 'config_sha256', 'config');

-- Deny altering frozen child collections, including inserting an extra child afterward.
-- FOR SHARE prevents a race between modifying a child and finalizing its parent.
CREATE FUNCTION prevent_finalized_parent_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, inha_policy, public
AS $fn$
DECLARE
    parent_ids uuid[];
    parent_id uuid;
    finalized boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        parent_ids := ARRAY[(to_jsonb(NEW) ->> TG_ARGV[2])::uuid];
    ELSIF TG_OP = 'DELETE' THEN
        parent_ids := ARRAY[(to_jsonb(OLD) ->> TG_ARGV[2])::uuid];
    ELSE
        parent_ids := ARRAY[(to_jsonb(OLD) ->> TG_ARGV[2])::uuid,
                            (to_jsonb(NEW) ->> TG_ARGV[2])::uuid];
    END IF;
    FOR parent_id IN SELECT DISTINCT unnest(parent_ids) LOOP
        EXECUTE format('SELECT %I IS NOT NULL FROM inha_policy.%I WHERE id = $1 FOR SHARE',
                       TG_ARGV[1], TG_ARGV[0])
            INTO finalized USING parent_id;
        IF finalized THEN
            RAISE EXCEPTION '% belongs to finalized %; append a new version instead',
                            TG_TABLE_NAME, TG_ARGV[0];
        END IF;
    END LOOP;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER notice_version_assets_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON notice_version_assets
    FOR EACH ROW EXECUTE FUNCTION prevent_finalized_parent_change
        ('notice_versions', 'sealed_at', 'notice_version_id');
CREATE TRIGGER document_blocks_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON document_blocks
    FOR EACH ROW EXECUTE FUNCTION prevent_finalized_parent_change
        ('documents', 'sealed_at', 'document_id');
CREATE TRIGGER opportunity_version_sources_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON opportunity_version_sources
    FOR EACH ROW EXECUTE FUNCTION prevent_finalized_parent_change
        ('opportunity_versions', 'published_at', 'opportunity_version_id');
CREATE TRIGGER application_windows_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON application_windows
    FOR EACH ROW EXECUTE FUNCTION prevent_finalized_parent_change
        ('opportunity_versions', 'published_at', 'opportunity_version_id');
CREATE TRIGGER benefits_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON benefits
    FOR EACH ROW EXECUTE FUNCTION prevent_finalized_parent_change
        ('opportunity_versions', 'published_at', 'opportunity_version_id');
CREATE TRIGGER eligibility_profiles_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON eligibility_profiles
    FOR EACH ROW EXECUTE FUNCTION prevent_finalized_parent_change
        ('opportunity_versions', 'published_at', 'opportunity_version_id');
CREATE TRIGGER field_evidence_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON field_evidence
    FOR EACH ROW EXECUTE FUNCTION prevent_finalized_parent_change
        ('opportunity_versions', 'published_at', 'opportunity_version_id');
CREATE TRIGGER field_resolutions_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON field_resolutions
    FOR EACH ROW EXECUTE FUNCTION prevent_finalized_parent_change
        ('opportunity_versions', 'published_at', 'opportunity_version_id');
CREATE TRIGGER search_chunk_blocks_immutable_when_embedded
    BEFORE INSERT OR UPDATE OR DELETE ON search_chunk_blocks
    FOR EACH ROW EXECUTE FUNCTION prevent_finalized_parent_change
        ('search_chunks', 'embedding_created_at', 'chunk_id');

-- A published version must have confirmed, sealed, successful source extractions.
-- Partial source/attachment coverage is allowed only as explicitly reported quality.
CREATE FUNCTION validate_opportunity_publication()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, inha_policy, public
AS $fn$
BEGIN
    IF NEW.published_at IS NOT NULL THEN
        IF NOT EXISTS (
            SELECT 1 FROM opportunity_version_sources s
            WHERE s.opportunity_version_id = NEW.id
        ) THEN
            RAISE EXCEPTION 'Published opportunity version needs a source mapping';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM opportunity_version_sources s
            JOIN notice_versions nv ON nv.id = s.notice_version_id
            JOIN extraction_runs er ON er.id = s.extraction_run_id
            WHERE s.opportunity_version_id = NEW.id
              AND (s.relationship_status <> 'confirmed' OR nv.sealed_at IS NULL
                   OR er.sealed_at IS NULL OR er.status <> 'succeeded')
        ) THEN
            RAISE EXCEPTION 'Publish only confirmed mappings to sealed successful extractions';
        END IF;
        IF EXISTS (
            SELECT 1 FROM field_evidence e
            WHERE e.opportunity_version_id = NEW.id AND e.candidate_status = 'conflicting'
              AND NOT EXISTS (
                  SELECT 1 FROM field_resolutions r WHERE r.id = e.resolution_id
                    AND r.status IN ('agreed', 'resolved_explicit_update')
              )
        ) AND NEW.data_quality_status <> 'needs_review' THEN
            RAISE EXCEPTION 'Unresolved evidence conflicts require needs_review quality';
        END IF;
        IF EXISTS (
            SELECT 1 FROM field_resolutions r
            WHERE r.opportunity_version_id = NEW.id AND r.status = 'unresolved'
        ) AND NEW.data_quality_status <> 'needs_review' THEN
            RAISE EXCEPTION 'Unresolved/pending amendments require needs_review quality';
        END IF;
        IF EXISTS (
            SELECT 1 FROM field_resolutions r
            JOIN field_evidence chosen ON chosen.id = r.selected_evidence_id
            WHERE r.opportunity_version_id = NEW.id
              AND (chosen.verification_status <> 'verified'
                   OR chosen.candidate_status <> 'accepted'
                   OR chosen.candidate_value IS DISTINCT FROM r.selected_value)
        ) THEN
            RAISE EXCEPTION 'A resolved value must equal its verified accepted evidence candidate';
        END IF;
        IF EXISTS (
            SELECT 1 FROM field_resolutions r
            JOIN field_evidence witness
              ON witness.id IN (r.intent_evidence_id, r.cross_reference_evidence_id)
            WHERE r.opportunity_version_id = NEW.id
              AND (witness.opportunity_version_id <> NEW.id
                   OR witness.verification_status <> 'verified')
        ) THEN
            RAISE EXCEPTION 'Amendment/cross-reference evidence must be verified in this published version';
        END IF;
        IF EXISTS (
            SELECT 1 FROM field_evidence e JOIN field_resolutions r ON r.id = e.resolution_id
            WHERE e.opportunity_version_id = NEW.id AND e.candidate_status = 'superseded'
              AND r.status NOT IN ('agreed', 'resolved_explicit_update')
        ) THEN
            RAISE EXCEPTION 'Superseded candidates require a resolved field decision';
        END IF;
        IF EXISTS (
            SELECT nv.notice_id FROM opportunity_version_sources s
            JOIN notice_versions nv ON nv.id = s.notice_version_id
            WHERE s.opportunity_version_id = NEW.id AND s.is_current_dependency
            GROUP BY nv.notice_id HAVING count(*) > 1
        ) THEN
            RAISE EXCEPTION 'Use one current dependency per source notice; retain old versions as history';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM opportunity_version_sources s
            WHERE s.opportunity_version_id = NEW.id AND s.is_current_dependency
        ) THEN
            RAISE EXCEPTION 'A current published opportunity needs at least one watched source';
        END IF;
        IF EXISTS (
            SELECT 1 FROM opportunity_version_sources s
            LEFT JOIN identity_decisions d ON d.id = s.identity_decision_id
            WHERE s.opportunity_version_id = NEW.id
              AND ((s.identity_decision_id IS NOT NULL AND
                    (d.decision_status <> 'confirmed' OR
                     NEW.opportunity_id NOT IN (d.opportunity_id, COALESCE(d.other_opportunity_id, d.opportunity_id))))
                   OR (s.relation_kind IN ('extension', 'correction', 'cancellation', 'reopened')
                       AND s.identity_decision_id IS NULL))
        ) THEN
            RAISE EXCEPTION 'Cross-notice amendments need a confirmed same-opportunity identity decision';
        END IF;
        IF EXISTS (
            SELECT 1 FROM documents d
            WHERE d.id IN (
                SELECT e.document_id FROM field_evidence e
                WHERE e.opportunity_version_id = NEW.id
                UNION
                SELECT b.document_id FROM search_chunk_blocks b
                WHERE b.opportunity_version_id = NEW.id
            ) AND (d.sealed_at IS NULL OR d.status NOT IN ('succeeded', 'partial'))
        ) THEN
            RAISE EXCEPTION 'Published evidence/search provenance needs sealed successful or partial documents';
        END IF;
        IF NEW.data_quality_status = 'complete' AND (
            EXISTS (
                SELECT 1 FROM opportunity_version_sources s
                JOIN notice_versions nv ON nv.id = s.notice_version_id
                WHERE s.opportunity_version_id = NEW.id
                  AND s.is_current_dependency
                  AND nv.asset_collection_status <> 'complete'
            ) OR EXISTS (
                SELECT 1 FROM documents d
                WHERE d.id IN (
                    SELECT e.document_id FROM field_evidence e
                    WHERE e.opportunity_version_id = NEW.id
                    UNION
                    SELECT b.document_id FROM search_chunk_blocks b
                    WHERE b.opportunity_version_id = NEW.id
                ) AND d.status <> 'succeeded'
            )
        ) THEN
            RAISE EXCEPTION 'Complete quality requires complete asset collection and successful provenance documents';
        END IF;
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER opportunity_versions_validate_publication
    BEFORE INSERT OR UPDATE ON opportunity_versions
    FOR EACH ROW EXECUTE FUNCTION validate_opportunity_publication();

-- A later embedding-profile backfill can add chunks for an already published version.
-- Enforce immutable provenance on that path too, not only during initial publication.
CREATE FUNCTION validate_search_chunk_document()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, inha_policy, public
AS $fn$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM documents d
        WHERE d.id = NEW.document_id AND d.sealed_at IS NOT NULL
          AND d.status IN ('succeeded', 'partial')
    ) THEN
        RAISE EXCEPTION 'Search chunk provenance requires a sealed successful or partial document';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER search_chunk_blocks_validate_document
    BEFORE INSERT OR UPDATE ON search_chunk_blocks
    FOR EACH ROW EXECUTE FUNCTION validate_search_chunk_document();

-- A mixed snippet must expose canonical_overrides to the API/UI. Example object:
-- {"field_path":"/application_windows/main/end_date","scope_key":"provider/main",
--  "resolution_id":"UUID","value":"2026-10-08","label":"현행 마감일"}.
-- Split a historical deadline sentence from still-current eligibility where possible.
-- Never mark an entire old PDF historical solely because one deadline was superseded.
CREATE FUNCTION validate_chunk_resolutions()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, inha_policy, public
AS $fn$
BEGIN
    IF EXISTS (
        SELECT 1 FROM unnest(NEW.applied_resolution_ids) AS selected(resolution_id)
        WHERE NOT EXISTS (
            SELECT 1 FROM field_resolutions r WHERE r.id = selected.resolution_id
              AND r.opportunity_version_id = NEW.opportunity_version_id
              AND r.status IN ('agreed', 'resolved_explicit_update')
        )
    ) THEN
        RAISE EXCEPTION 'Chunk overrides require resolved decisions from the same opportunity version';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER search_chunks_validate_resolutions
    BEFORE INSERT OR UPDATE ON search_chunks
    FOR EACH ROW EXECUTE FUNCTION validate_chunk_resolutions();

-- No DEFAULT/identity is defined on event_seq: allocation MUST happen after this lock.
-- Supplied cursors are rejected. The lock is held until COMMIT/ROLLBACK, so another
-- transaction cannot allocate a later cursor and commit before this transaction ends.
-- Writers should also take this same advisory lock at the START of their short publish/
-- freshness transaction, before taking row locks, to avoid multi-row lock-order deadlocks.
CREATE FUNCTION allocate_change_event_order()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, inha_policy, public
AS $fn$
BEGIN
    IF NEW.event_seq IS NOT NULL THEN
        RAISE EXCEPTION 'event_seq is server allocated; do not supply a cursor';
    END IF;
    PERFORM pg_advisory_xact_lock(7410932176501::bigint);
    NEW.event_seq := nextval('inha_policy.change_event_order_seq'::regclass);
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER change_events_allocate_order
    BEFORE INSERT ON change_events
    FOR EACH ROW EXECUTE FUNCTION allocate_change_event_order();
ALTER TABLE change_events ENABLE ALWAYS TRIGGER change_events_allocate_order;

-- A published pointer/lifecycle change automatically appends its event in the SAME tx.
-- This event is the outbox message; dispatch acknowledgements/retries are consumer state,
-- not edits to this immutable event. Publish the version BEFORE switching the pointer.
CREATE FUNCTION emit_opportunity_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, inha_policy, public
AS $fn$
DECLARE
    previous_id uuid;
    previous_lifecycle text;
    previous_redirect uuid;
    selected_kind text;
    version_edit_kind text;
    version_observed_at timestamptz;
    stale boolean := false;
    visible boolean;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        previous_id := OLD.current_version_id;
        previous_lifecycle := OLD.lifecycle_status;
        previous_redirect := OLD.merged_into_id;
        IF OLD.current_version_id IS NOT DISTINCT FROM NEW.current_version_id
           AND OLD.lifecycle_status IS NOT DISTINCT FROM NEW.lifecycle_status
           AND OLD.merged_into_id IS NOT DISTINCT FROM NEW.merged_into_id THEN
            RETURN NEW;
        END IF;
    END IF;
    IF NEW.current_version_id IS NULL AND previous_id IS NULL THEN RETURN NEW; END IF;
    IF NEW.current_version_id IS NOT NULL THEN
        SELECT ov.edit_kind, ov.observed_at INTO version_edit_kind, version_observed_at
        FROM opportunity_versions ov
        WHERE ov.id = NEW.current_version_id AND ov.opportunity_id = NEW.id
          AND ov.publication_state = 'published' AND ov.published_at IS NOT NULL;
        IF NOT FOUND THEN RAISE EXCEPTION 'Publish the version before switching its public pointer'; END IF;
        SELECT EXISTS (
            SELECT 1 FROM opportunity_version_sources s
            JOIN notice_versions nv ON nv.id = s.notice_version_id
            JOIN notices n ON n.id = nv.notice_id
            WHERE s.opportunity_version_id = NEW.current_version_id AND s.is_current_dependency
              AND (n.current_notice_version_id IS DISTINCT FROM s.notice_version_id OR
                   (s.requires_live_source AND
                    (n.availability_status <> 'available' OR n.scope_status <> 'included')))
        ) INTO stale;
    END IF;
    visible := NEW.lifecycle_status = 'active' AND NEW.current_version_id IS NOT NULL AND NOT stale;
    selected_kind := CASE
        WHEN NEW.lifecycle_status = 'merged' THEN 'merged'
        WHEN NEW.lifecycle_status = 'inactive' OR NEW.current_version_id IS NULL THEN 'unpublished'
        WHEN version_edit_kind = 'split' THEN 'split'
        WHEN previous_lifecycle IN ('inactive', 'merged') THEN 'restored'
        WHEN previous_id IS NULL THEN 'created'
        WHEN version_edit_kind = 'extension' THEN 'extended'
        WHEN version_edit_kind = 'correction' THEN 'corrected'
        WHEN version_edit_kind = 'cancellation' THEN 'cancelled'
        ELSE 'updated' END;
    INSERT INTO change_events (
        event_kind, opportunity_id, opportunity_version_id, previous_version_id,
        identity_decision_id, edit_kind, source_is_stale, visible_after, observed_at, payload
    ) VALUES (
        selected_kind, NEW.id, NEW.current_version_id, previous_id,
        NEW.last_identity_decision_id, version_edit_kind, stale, visible,
        COALESCE(version_observed_at, clock_timestamp()),
        jsonb_build_object('previous_lifecycle', previous_lifecycle,
                           'lifecycle', NEW.lifecycle_status,
                           'previous_redirect', previous_redirect,
                           'merged_into_id', NEW.merged_into_id)
    );
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER opportunities_emit_change
    AFTER INSERT OR UPDATE ON opportunities
    FOR EACH ROW EXECUTE FUNCTION emit_opportunity_change();

-- Deferred pointer checks allow assembly/publication/current-pointer switch in one tx.
CREATE FUNCTION validate_notice_current_pointer()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, inha_policy, public
AS $fn$
BEGIN
    IF NEW.current_notice_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM notice_versions nv
        WHERE nv.id = NEW.current_notice_version_id AND nv.notice_id = NEW.id
          AND nv.sealed_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'Current notice pointer must reference its sealed snapshot';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE CONSTRAINT TRIGGER notices_validate_current_pointer
    AFTER INSERT OR UPDATE ON notices
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_notice_current_pointer();

CREATE FUNCTION validate_opportunity_current_pointer()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, inha_policy, public
AS $fn$
BEGIN
    IF NEW.current_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM opportunity_versions ov
        WHERE ov.id = NEW.current_version_id AND ov.opportunity_id = NEW.id
          AND ov.publication_state = 'published' AND ov.published_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'Current opportunity pointer must reference its published version';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE CONSTRAINT TRIGGER opportunities_validate_current_pointer
    AFTER INSERT OR UPDATE ON opportunities
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_opportunity_current_pointer();

-- ----------------------------- Read models -----------------------------
-- Current structured API data includes stale status explicitly. The old published
-- version survives a parser/LLM failure, but must not be advertised as freshly verified.
CREATE VIEW current_opportunity_versions AS
SELECT ov.*,
       o.lifecycle_status,
       EXISTS (
           SELECT 1
           FROM opportunity_version_sources s
           JOIN notice_versions nv ON nv.id = s.notice_version_id
           JOIN notices n ON n.id = nv.notice_id
           WHERE s.opportunity_version_id = ov.id
             AND s.is_current_dependency
             AND (n.current_notice_version_id IS DISTINCT FROM s.notice_version_id OR
                  (s.requires_live_source AND
                   (n.availability_status <> 'available' OR n.scope_status <> 'included')))
       ) AS source_is_stale
FROM opportunities o
JOIN opportunity_versions ov
  ON ov.id = o.current_version_id AND ov.opportunity_id = o.id
WHERE o.lifecycle_status = 'active'
  AND ov.publication_state = 'published'
  AND ov.published_at IS NOT NULL;

-- Default "current" search excludes obsolete versions, obsolete embedding profiles,
-- historical/mixed raw snippets, incomplete embeddings, and newly changed/unavailable
-- sources awaiting publication. Mixed snippets are retained for explicit history/detail
-- modes with canonical override labels; split their still-current blocks for normal search.
-- A separate explicitly stale result mode can query the current-opportunity view.
CREATE VIEW current_search_chunks AS
SELECT sc.*,
       ov.opportunity_id,
       ov.title AS opportunity_title,
       ov.opportunity_kind,
       ov.categories,
       ov.provider_name,
       ov.data_quality_status,
       ov.source_status_override
FROM search_chunks sc
JOIN current_opportunity_versions ov ON ov.id = sc.opportunity_version_id
JOIN embedding_profiles ep ON ep.id = sc.embedding_profile_id
WHERE ep.is_active
  AND sc.evidence_role = 'current'
  AND sc.embedding_status = 'succeeded'
  AND sc.embedding IS NOT NULL
  AND NOT ov.source_is_stale;

-- Feed reads are immutable event snapshots, not a join to whichever version is current
-- when a client paginates. Expose an opaque/signed cursor wrapping event_seq plus API/filter
-- version. Consumers advance to the LAST RETURNED event, not an unprocessed global MAX.
CREATE VIEW published_change_feed AS
SELECT event_seq, id, event_kind, opportunity_id, opportunity_version_id,
       previous_version_id, identity_decision_id, edit_kind, source_is_stale,
       visible_after, observed_at, recorded_at, payload
FROM change_events;

COMMIT;

-- ------------------------- Query/operations notes -------------------------
--
-- The following are EXAMPLES, not executed statements.
--
-- Exact vector candidates after typed filters (application supplies :query_vector,
-- :profile_id, :as_of_date and all predicates as bound parameters):
--
-- WITH eligible_chunks AS MATERIALIZED (
--   SELECT c.id, c.opportunity_id, c.opportunity_version_id, c.embedding
--   FROM inha_policy.current_search_chunks c
--   WHERE c.embedding_profile_id = :profile_id
--     AND c.source_status_override IS NULL
--     AND EXISTS (
--       SELECT 1 FROM inha_policy.application_windows w
--       WHERE w.opportunity_version_id = c.opportunity_version_id
--         AND w.window_kind IN ('application', 'additional_application')
--         AND w.confirmation_status = 'confirmed'
--         AND w.closing_rule = 'fixed'
--         AND w.start_precision = 'date' AND w.end_precision = 'date'
--         AND (w.start_date < :as_of_date OR
--              (w.start_inclusive AND w.start_date = :as_of_date))
--         AND (w.end_date > :as_of_date OR
--              (w.end_inclusive AND w.end_date = :as_of_date))
--     )
-- )
-- SELECT *, embedding <=> CAST(:query_vector AS public.vector(1536)) AS distance
-- FROM eligible_chunks
-- ORDER BY embedding <=> CAST(:query_vector AS public.vector(1536))
-- LIMIT 100;
--
-- The example deliberately covers DATE-precision fixed windows only. For minute/second
-- bounds construct (date + time) AT TIME ZONE timezone and compare to an as-of instant.
-- Rolling/budget/capacity closing rules need separate "conditional/unknown" statuses.
-- Unknown dates and unknown eligibility must never silently become open/unrestricted.
--
-- Lexical retrieval uses the same current_search_chunks view and a query whose content
-- words were normalized by the configured Korean analyzer. Combine lexical/vector ranks
-- with RRF at OPPORTUNITY level, preserving top evidence chunks. Do not sum raw tsvector
-- ranks and cosine distances or allow one long attachment to occupy all result slots.
--
-- Optional HNSW, only after measuring exact latency and filtered recall:
--
-- CREATE INDEX search_chunks_embedding_hnsw_idx
--   ON inha_policy.search_chunks USING hnsw (embedding public.vector_cosine_ops)
--   WHERE embedding_status = 'succeeded';
--
-- With many retained versions/profiles, consider a separate partial index for a fixed
-- embedding_profile_id or an explicitly managed active search table after measurement.
-- ANN filters can reduce recall; a WHERE clause alone does not guarantee enough results.
-- In a transaction for pgvector >= 0.8:
-- SET LOCAL hnsw.iterative_scan = strict_order;
-- SET LOCAL hnsw.ef_search = 100;
-- Tune against filtered EXACT results; these example values are not a universal optimum.
-- Keep ORDER BY embedding <=> :vector with LIMIT for the index-friendly ANN shape.
--
-- Worker claim pattern: SELECT ... FOR UPDATE SKIP LOCKED from crawl_jobs, then
-- status='running' and attempt_count=attempt_count+1 in that same short transaction.
-- Do not hold a database transaction while downloading files or calling an LLM.
-- Recover expired leases using locked_at/heartbeat_at; preserve terminal errors.
--
-- Runtime guarantees still required:
-- * On notices.current_notice_version_id / availability_status / scope_status changes,
--   the worker compares old/new dependent-opportunity freshness and INSERTs a
--   freshness_changed event for each visibility transition IN THAT SAME transaction.
--   Include source_is_stale and visible_after. This handles a record hidden while new
--   structuring is pending, and its restoration. Unlike published pointer/lifecycle
--   events (automatic above), source-freshness transition detection is an APP duty.
-- * Acquire pg_advisory_xact_lock(7410932176501::bigint) before mutable publish/freshness
--   row locks. Event INSERT omits event_seq; its ALWAYS trigger allocates AFTER locking.
--   Do not use created_at or a plain bigserial as the incremental consumer cursor.
-- * Append fetch_snapshots for every attempted detail/file request, including repeated
--   200/304/failures. Do not overwrite prior observations or fabricate a successful fetch
--   from cache. Header metadata must be allowlisted/redacted; never render raw HTML.
-- * A separately registered verified extension updates the SAME opportunity ID with a
--   new version/event. New year/semester/track rounds get new IDs. Validate merge graphs
--   are acyclic, and perform merge/split/unlink/undo with identity_decisions + new versions
--   + all affected current pointers/events in one transaction. Never delete the losing ID.
-- * Validate identity decision participants and before/after snapshots; SQL stores the
--   reversible audit but does not automatically perform merge/split mutations for you.
-- * Build conflict candidates from current source slots, not every historical revision.
--   In-place changes need no correction marker. Cross-current-source precedence requires
--   verified field/cycle/scope-specific amendment intent and a new value/reliable reference.
-- * A confirmed extension with an unreadable new date is pending_amendment=true,
--   status=unresolved, canonical deadline unknown/needs_review. An old lone readable
--   candidate must not be treated as agreed in that situation.
-- * Validate supersession_edges form an acyclic, same-field/scope DAG and identify every
--   affected candidate. A late fetch date, confidence score or HTML-vs-PDF format alone
--   is never proof of precedence. A patch preserves unmentioned fields; not_stated is
--   not unrestricted and a failed parser must not blank a previously valid field.
-- * Map canonical_overrides to their applied_resolution_ids and display them in mixed
--   snippets. Exclude historical-only chunks. Do not relabel an entire source as stale
--   when only one field is superseded; keep still-current eligibility searchable.
-- * Verify every field_evidence.quote_text against the referenced block text.
-- * Verify evidence block membership in extraction_runs.input_manifest in application
--   code. The SQL FKs enforce source/run identity, not JSON-manifest membership.
-- * Validate total expected document/asset coverage before claiming complete quality;
--   the publication trigger checks cited documents and downloaded-asset completeness,
--   but cannot infer intentionally omitted or unsupported input from a JSON manifest.
-- * Validate rules/contacts/links/required_documents with versioned Pydantic schemas.
-- * Source body/attachment conflicts retain ALL candidates until explicitly resolved.
-- * Seal parser results and extraction runs only after their input manifest is complete.
-- * Never mutate a profile's model/preprocessing identity once embeddings exist.
-- * Maintain updated_at/last_checked_at and run counters in the worker transactions.
-- * Store relative/ambiguous date phrases verbatim rather than inventing absolute dates.
-- * Detect binary signatures and hash downloaded bytes even when MIME is wrong.
-- * ETag/Last-Modified are opportunistic: absence is normal for download endpoints.
--
-- Official technical references consulted:
-- https://github.com/pgvector/pgvector
-- https://www.postgresql.org/docs/current/pgtrgm.html
-- https://github.com/bab2min/kiwipiepy
-- https://docling-project.github.io/docling/usage/supported_formats/
-- https://tech.hancom.com/hwpxformat/
-- https://tech.hancom.com/python-hwp-parsing-1/
-- https://pyhwp.readthedocs.io/en/latest/intro.html
