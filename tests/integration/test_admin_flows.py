"""Admin and worker flows against a disposable PostgreSQL database and the real providers.

Run with ``scripts/integration-test.sh``; it creates a ``*_test`` database, applies the
migrations, and points DATABASE_URL at it. The tests refuse to run against any other database.
Extraction and embedding use the LLM/embedding configured in ``.env``; nothing is faked, so the
assertions check structure and invariants rather than exact model wording. A handful of
realistic notices is seeded once and shared, which keeps the number of model calls small.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unittest
import uuid
from typing import Any
from urllib.parse import urlparse

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SAFE_DATABASE = urlparse(DATABASE_URL).path.rstrip("/").endswith("_test")

if SAFE_DATABASE:
    import httpx
    from argon2 import PasswordHasher
    from sqlalchemy import text

    from policy_harvester import worker as worker_module
    from policy_harvester.admin import serializer
    from policy_harvester.apikeys import generate_key, hash_key
    from policy_harvester.ai.prompts import (EXTRACTION_PROMPT_VERSION_V2, EXTRACTION_SCHEMA_VERSION_V2,
                                             EXTRACTION_SYSTEM_PROMPT_V2)
    from policy_harvester.ai.providers import ProviderRegistry
    from policy_harvester.ai.schema_v2 import ExtractionBundleV2
    from policy_harvester.api import app
    from policy_harvester.config import effective_configuration, resolved_settings
    from policy_harvester.db import SessionFactory, engine
    from policy_harvester.pipeline import LATEST_DOCUMENTS_SQL, OpportunityAssembler

PASSWORD = "integration-password-1234"

NOTICES: dict[str, dict[str, str]] = {
    "multi": {
        "title": "[학부-교외장학] 2027년 가온장학재단 장학생 선발 안내",
        "body": """<p>2027년 가온장학재단에서 아래 두 가지 장학생을 선발하오니 희망 학생은 신청하기 바랍니다.</p>
<p>1. 가온 학업장학금: 학기당 등록금 전액, 선발인원 5명, 직전학기 평점 3.5/4.5 이상인 재학생</p>
<p>2. 가온 생활비장학금: 월 50만원(10개월), 선발인원 10명, 학자금 지원구간 3구간 이하 재학생</p>
<p>신청기간: 2026. 11. 2.(월) ~ 2026. 11. 13.(금) 17:00</p>
<p>신청방법: 가온장학재단 홈페이지 온라인 신청</p>
<p>문의: 학생지원팀 032-860-0000</p>""",
    },
    "original": {
        "title": "[학부-교외장학] 2027년 나래재단 장학생 선발 안내",
        "body": """<p>2027년 나래재단 장학생을 다음과 같이 선발합니다.</p>
<p>지원금액: 1인당 300만원(1회)</p>
<p>지원자격: 2027년 1학기 재학 예정인 학부생, 직전학기 평점 3.0/4.5 이상</p>
<p>신청기간: 2026. 9. 15.(화) ~ 2026. 10. 1.(목) 17:00</p>
<p>선발인원: 3명</p>
<p>신청방법: 학생지원팀 방문 제출</p>""",
    },
    "extension": {
        "title": "[기간연장] 2027년 나래재단 장학생 선발 안내",
        "body": """<p>2027년 나래재단 장학생 선발의 신청기간을 아래와 같이 연장합니다.</p>
<p>변경 전: 2026. 10. 1.(목) 17:00까지</p>
<p>변경 후: 2026. 10. 8.(목) 17:00까지 (기간연장)</p>
<p>그 외 지원자격과 지원금액은 기존 공고와 동일합니다.</p>""",
    },
    "repost": {
        "title": "[재게시] 2027년 나래재단 장학생 선발 안내",
        "body": """<p>2027년 나래재단 장학생 선발 공고를 다시 안내합니다.</p>
<p>지원금액: 1인당 300만원(1회)</p>
<p>지원자격: 2027년 1학기 재학 예정인 학부생, 직전학기 평점 3.0/4.5 이상</p>
<p>신청기간: 2026. 9. 15.(화) ~ 2026. 10. 1.(목) 17:00</p>""",
    },
    "previous_year": {
        "title": "[학부-교외장학] 2026년 나래재단 장학생 선발 안내",
        "body": """<p>2026년 나래재단 장학생을 다음과 같이 선발합니다.</p>
<p>지원금액: 1인당 300만원(1회)</p>
<p>신청기간: 2025. 9. 15.(월) ~ 2025. 10. 1.(수) 17:00</p>
<p>선발인원: 3명</p>""",
    },
    "out_of_scope": {
        "title": "[인하대학교] 2026-2학기 직원 채용 공고",
        "body": "<p>2026-2학기 직원을 채용합니다. 접수: 2026. 11. 30.까지</p>",
        "scope": "excluded",
    },
    "assembler_only": {
        "title": "[학부-교외장학] 조립 검증용 공고",
        "body": "<p>다온 장학금 신청 안내</p><p>보람 장학금 신청 안내</p>",
        "scope": "excluded",
    },
}
SEEDED: dict[str, dict[str, uuid.UUID]] = {}


def _missing() -> dict[str, Any]:
    return {"value": None, "state": "not_found", "evidence": []}


def _fixture_item(key: str, name: str, block_id: str, quote: str, year: int) -> dict[str, Any]:
    ref = [{"block_id": block_id, "quote": quote, "evidence_type": "explicit"}]
    eligibility = {field: _missing() for field in (
        "student_types", "grades", "majors", "gpa_min", "gpa_scale", "income_bracket_max",
        "regions", "schools", "enrollment_states")}
    eligibility.update({"rule_tree": None, "residual_conditions": []})
    return {"local_key": key, "name": {"value": name, "state": "stated", "evidence": ref},
            "aliases": [], "organization": _missing(), "category": "scholarship",
            "academic_year": {"value": year, "state": "stated", "evidence": ref},
            "semester": _missing(), "round_label": _missing(),
            "summary": name, "benefits": [], "application_windows": [], "eligibility": eligibility,
            "selection": {"selection_count": _missing(), "nomination_quota": _missing(),
                          "count_text": None, "method": _missing()},
            "application_methods": [], "required_documents": [], "contacts": []}


@unittest.skipUnless(SAFE_DATABASE, "integration tests need DATABASE_URL pointing at a *_test database")
class AdminFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.worker = worker_module.Worker()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://testserver")
        if not SEEDED:
            for key, notice in NOTICES.items():
                SEEDED[key] = await self._seed_notice(notice["title"], notice["body"],
                                                      notice.get("scope", "included"))
            await self._drain()
        self.csrf = await self._login("reviewer")
        self.client.headers["X-API-Key"] = await self._issue_api_key()

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await engine.dispose()

    # helpers -------------------------------------------------------------------------------

    async def _login(self, role: str) -> str:
        email = f"{role}-{uuid.uuid4().hex[:8]}@example.test"
        async with SessionFactory() as session:
            await session.execute(text("""
                INSERT INTO inha_policy.admin_users (email, display_name, password_hash, role)
                VALUES (:email, :email, :hash, :role)
            """), {"email": email, "hash": PasswordHasher().hash(PASSWORD), "role": role})
            await session.commit()
        self.client.cookies.clear()
        response = await self.client.post("/admin/login", data={"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 303)
        return serializer.loads(self.client.cookies["policy_admin"])["csrf"]

    async def _issue_api_key(self, rate_limit: int = 1000) -> str:
        key, prefix, digest = generate_key()
        async with SessionFactory() as session:
            await session.execute(text("""
                INSERT INTO inha_policy.api_keys (name, key_prefix, key_sha256, rate_limit_per_minute)
                VALUES ('test', :prefix, :digest, :rate)
            """), {"prefix": prefix, "digest": digest, "rate": rate_limit})
            await session.commit()
        return key

    async def _post(self, path: str, **form: Any) -> httpx.Response:
        return await self.client.post(path, data={"csrf": self.csrf, **form})

    async def _scalar(self, sql: str, **params: Any) -> Any:
        async with SessionFactory() as session:
            return (await session.execute(text(sql), params)).scalar()

    async def _rows(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        async with SessionFactory() as session:
            return [dict(row) for row in (await session.execute(text(sql), params)).mappings()]

    async def _drain(self) -> None:
        for _ in range(200):
            if not await self.worker.run_once():
                return
        self.fail("worker queue did not drain")

    async def _seed_notice(self, title: str, body: str, scope: str) -> dict[str, uuid.UUID]:
        raw_sha = hashlib.sha256(body.encode()).hexdigest()
        async with SessionFactory() as session:
            source_id = (await session.execute(text("""
                INSERT INTO inha_policy.sources (source_key, name, base_url, list_url, board_key)
                VALUES ('it-source', 'Integration', 'https://example.test',
                        'https://example.test/list', 'it')
                ON CONFLICT (source_key) DO UPDATE SET name=EXCLUDED.name RETURNING id
            """))).scalar_one()
            run_id = (await session.execute(text("""
                INSERT INTO inha_policy.crawl_runs
                  (source_id, mode, scheduled_for, status, started_at, finished_at)
                VALUES (:source, 'manual', now(), 'succeeded', now(), now()) RETURNING id
            """), {"source": source_id})).scalar_one()
            post_id = uuid.uuid4().hex
            notice_id = (await session.execute(text("""
                INSERT INTO inha_policy.notices (source_id, external_post_id, canonical_url, scope_status)
                VALUES (:source, :post, :url, :scope) RETURNING id
            """), {"source": source_id, "post": post_id, "scope": scope,
                     "url": f"https://example.test/{post_id}"})).scalar_one()
            asset_id = (await session.execute(text("""
                INSERT INTO inha_policy.binary_assets (sha256, storage_key, detected_mime, byte_size)
                VALUES (:sha, :key, 'text/html', :size)
                ON CONFLICT (sha256) DO UPDATE SET sha256=EXCLUDED.sha256 RETURNING id
            """), {"sha": raw_sha, "key": f"sha256/{raw_sha}", "size": len(body.encode())})).scalar_one()
            snapshot_id = (await session.execute(text("""
                INSERT INTO inha_policy.fetch_snapshots
                  (source_id, crawl_run_id, collection_id, notice_id, resource_kind, resource_key,
                   requested_url, outcome, http_status, response_body_asset_id,
                   representation_asset_id, fetch_started_at, fetched_at)
                VALUES (:source, :run, gen_random_uuid(), :notice, 'detail', :post, :url,
                        'received', 200, :asset, :asset, now(), now()) RETURNING id
            """), {"source": source_id, "run": run_id, "notice": notice_id, "post": post_id,
                     "url": f"https://example.test/{post_id}", "asset": asset_id})).scalar_one()
            version_id = (await session.execute(text("""
                INSERT INTO inha_policy.notice_versions
                  (notice_id, crawl_run_id, revision_no, raw_html_sha256, raw_html_storage_key,
                   title, body_html, origin_fetch_snapshot_id, content_fingerprint,
                   asset_collection_status)
                VALUES (:notice, :run, 1, :sha, :key, :title, :body, :snapshot, :sha, 'complete')
                RETURNING id
            """), {"notice": notice_id, "run": run_id, "sha": raw_sha, "snapshot": snapshot_id,
                     "key": f"sha256/{raw_sha}", "title": title, "body": body})).scalar_one()
            await session.execute(text(
                "UPDATE inha_policy.notice_versions SET sealed_at=now() WHERE id=:id"), {"id": version_id})
            await session.execute(text(
                "UPDATE inha_policy.notices SET current_notice_version_id=:version WHERE id=:id"
            ), {"version": version_id, "id": notice_id})
            await session.execute(text("""
                INSERT INTO inha_policy.crawl_jobs
                  (crawl_run_id, stage, job_key, notice_id, notice_version_id, payload)
                VALUES (:run, 'parse_document', :key, :notice, :version, '{"origin":"html_body"}')
            """), {"run": run_id, "key": f"body:{version_id}", "notice": notice_id,
                     "version": version_id})
            await session.commit()
        return {"run": run_id, "notice": notice_id, "version": version_id}

    async def _new_attempt(self, notice_version_id: uuid.UUID, status: str) -> uuid.UUID:
        """Append a newer sealed parse attempt with the same text, so extraction input changes."""
        document_id = uuid.uuid4()
        async with SessionFactory() as session:
            await session.execute(text("""
                INSERT INTO inha_policy.documents
                  (id, notice_version_id, origin_kind, parser_name, parser_version,
                   parser_config_sha256, normalizer_version, attempt_no, status, parsed_text,
                   quality_flags, started_at, finished_at)
                SELECT DISTINCT ON (notice_version_id) :id, notice_version_id, origin_kind,
                       'test-reparse', '1.0', parser_config_sha256, normalizer_version, 1,
                       :status, parsed_text, CASE WHEN :status='partial' THEN ARRAY['ocr_page']
                                                  ELSE ARRAY[]::text[] END,
                       now(), now() + interval '1 second'
                FROM inha_policy.documents WHERE notice_version_id=:version
                ORDER BY notice_version_id, finished_at DESC
            """), {"id": document_id, "version": notice_version_id, "status": status})
            await session.execute(text("""
                INSERT INTO inha_policy.document_blocks
                  (id, document_id, block_index, block_kind, text_content, extraction_metadata)
                SELECT gen_random_uuid(), :id, block_index, block_kind, text_content, '{}'::jsonb
                FROM inha_policy.document_blocks
                WHERE document_id=(SELECT id FROM inha_policy.documents
                                   WHERE notice_version_id=:version AND id<>:id
                                   ORDER BY finished_at DESC LIMIT 1)
            """), {"id": document_id, "version": notice_version_id})
            await session.execute(text(
                "UPDATE inha_policy.documents SET sealed_at=now() WHERE id=:id"), {"id": document_id})
            await session.commit()
        return document_id

    async def _opportunities(self, notice_version_id: uuid.UUID) -> list[dict[str, Any]]:
        return await self._rows("""
            SELECT DISTINCT ON (ov.opportunity_id) ov.opportunity_id, ov.id AS version_id,
                   ov.title, ov.version_no, o.lifecycle_status
            FROM inha_policy.opportunity_version_sources ovs
            JOIN inha_policy.opportunity_versions ov ON ov.id=ovs.opportunity_version_id
            JOIN inha_policy.opportunities o ON o.id=ov.opportunity_id
            WHERE ovs.notice_version_id=:id ORDER BY ov.opportunity_id, ov.version_no DESC
        """, id=notice_version_id)

    async def _one(self, key: str) -> dict[str, Any]:
        items = await self._opportunities(SEEDED[key]["version"])
        self.assertTrue(items, f"{key}: the model produced no opportunity")
        return items[0]

    async def _block(self, key: str, pattern: str) -> dict[str, Any]:
        rows = await self._rows(LATEST_DOCUMENTS_SQL + """
            SELECT b.id, b.text_content FROM latest d
            JOIN inha_policy.document_blocks b ON b.document_id=d.id ORDER BY b.block_index
        """, version=SEEDED[key]["version"])
        for row in rows:
            match = re.search(pattern, row["text_content"])
            if match:
                return {"id": row["id"], "quote": match.group(0)}
        self.fail(f"{key}: no block matches {pattern}")

    # pipeline ------------------------------------------------------------------------------

    async def test_pipeline_uses_configured_provider_and_splits_programs(self) -> None:
        jobs = await self._rows("""
            SELECT stage, status, error_message FROM inha_policy.crawl_jobs j
            JOIN inha_policy.notices n ON n.id=j.notice_id
            WHERE n.scope_status='included' AND j.job_key NOT LIKE 'broken:%'
        """)
        self.assertTrue(all(row["status"] == "succeeded" for row in jobs), jobs)
        async with SessionFactory() as session:
            registry = ProviderRegistry(await resolved_settings(session))
        primary, crosscheck = registry.extraction(), registry.crosscheck()
        runs = {(row["provider_name"], row["model_id"]) for row in await self._rows("""
            SELECT DISTINCT provider_name, model_id FROM inha_policy.extraction_runs
            WHERE status='succeeded' AND processing_code_version <> 'fixture'
              AND provider_name <> 'fixture'
        """)}
        # Only the configured extraction model and its cross-check/fallback model are used. Which of
        # the two produced the runs depends on the providers' availability (a 503 falls back).
        allowed = {(primary.provider_name, primary.model)}
        if crosscheck:
            allowed.add((crosscheck.provider_name, crosscheck.model))
        self.assertTrue(runs)
        self.assertLessEqual(runs, allowed)
        multi = [row for row in await self._opportunities(SEEDED["multi"]["version"])]
        initial = await self._rows("""
            SELECT DISTINCT ov.opportunity_id FROM inha_policy.opportunity_versions ov
            JOIN inha_policy.opportunity_version_sources s ON s.opportunity_version_id=ov.id
            WHERE s.notice_version_id=:id AND ov.edit_kind='initial'
        """, id=SEEDED["multi"]["version"])
        self.assertEqual(len(initial), 2, [row["title"] for row in multi])
        titles = " ".join(row["title"] for row in multi)
        self.assertIn("학업", titles)
        self.assertIn("생활비", titles)
        windows = await self._rows("""
            SELECT w.end_date::text AS end_date, w.end_time::text AS end_time
            FROM inha_policy.application_windows w WHERE w.opportunity_version_id=:id
        """, id=multi[0]["version_id"])
        self.assertIn({"end_date": "2026-11-13", "end_time": "17:00:00"}, windows)
        evidence = await self._scalar("""
            SELECT count(*) FROM inha_policy.field_evidence
            WHERE opportunity_version_id=:id AND verification_status='verified'
        """, id=multi[0]["version_id"])
        self.assertGreater(evidence, 0)
        chunks = await self._scalar("""
            SELECT count(*) FROM inha_policy.search_chunks
            WHERE embedding_status='succeeded' AND opportunity_version_id=:id
        """, id=multi[0]["version_id"])
        self.assertEqual(chunks, 1)

    async def test_out_of_scope_notice_never_reaches_llm(self) -> None:
        runs = await self._scalar("""
            SELECT count(*) FROM inha_policy.extraction_runs WHERE notice_version_id=:id
        """, id=SEEDED["out_of_scope"]["version"])
        self.assertEqual(runs, 0)
        self.assertEqual(await self._opportunities(SEEDED["out_of_scope"]["version"]), [])

    # identity ------------------------------------------------------------------------------

    async def test_repost_is_proposed_not_merged_and_previous_year_is_not(self) -> None:
        original = await self._one("original")
        repost = await self._one("repost")
        previous = await self._one("previous_year")
        self.assertNotEqual(original["opportunity_id"], repost["opportunity_id"])
        reviews = await self._rows("""
            SELECT id, payload FROM inha_policy.review_items
            WHERE opportunity_id=:id AND payload->>'operation'='cross_notice_candidates'
        """, id=repost["opportunity_id"])
        self.assertTrue(reviews, "a repost of the same edition must be proposed")
        candidates = {item["opportunity_id"]: item for item in reviews[0]["payload"]["candidates"]}
        self.assertIn(str(original["opportunity_id"]), candidates)
        self.assertNotIn(str(previous["opportunity_id"]), candidates)
        cross_year = await self._scalar("""
            SELECT count(*) FROM inha_policy.identity_decisions
            WHERE :a IN (opportunity_id, other_opportunity_id)
              AND :b IN (opportunity_id, other_opportunity_id)
        """, a=previous["opportunity_id"], b=original["opportunity_id"])
        self.assertEqual(cross_year, 0, "another year's edition is never a merge candidate")

        page = await self.client.get(f"/admin/reviews/{reviews[0]['id']}/identity")
        self.assertEqual(page.status_code, 200)
        proposal = candidates[str(original["opportunity_id"])]
        merged = await self._post(f"/admin/opportunities/{repost['opportunity_id']}/merge",
                                  winner_id=str(original["opportunity_id"]),
                                  proposal_id=proposal["decision_id"], review_id=str(reviews[0]["id"]),
                                  reason="같은 모집 재게시")
        self.assertEqual(merged.status_code, 303)
        again = await self._post(f"/admin/identity-decisions/{proposal['decision_id']}/reject",
                                 reason="이미 병합됨")
        self.assertEqual(again.status_code, 409)
        confirmed = (await self._rows("""
            SELECT d.decision_status, d.supersedes_decision_id::text AS supersedes
            FROM inha_policy.identity_decisions d
            JOIN inha_policy.opportunities o ON o.last_identity_decision_id=d.id WHERE o.id=:id
        """, id=repost["opportunity_id"]))[0]
        self.assertEqual(confirmed, {"decision_status": "confirmed", "supersedes": proposal["decision_id"]})

    async def test_same_notice_reextraction_matching_is_deterministic(self) -> None:
        """Assembler matching on fixed extraction output; no model is involved here."""
        version_id = SEEDED["assembler_only"]["version"]
        blocks = {row["text_content"]: str(row["id"]) for row in await self._rows("""
            SELECT b.id, b.text_content FROM inha_policy.document_blocks b
            JOIN inha_policy.documents d ON d.id=b.document_id WHERE d.notice_version_id=:id
        """, id=version_id)}
        dawn, bloom = "다온 장학금 신청 안내", "보람 장학금 신청 안내"

        def item(key: str, name: str, block: str, year: int) -> dict[str, Any]:
            return _fixture_item(key, name, blocks[block], block.split(" 신청")[0], year)

        async def assemble(items: list[dict[str, Any]]) -> list[uuid.UUID]:
            bundle = ExtractionBundleV2.model_validate({
                "schema_version": EXTRACTION_SCHEMA_VERSION_V2, "opportunities": items, "revisions": [],
                "coverage": "complete", "warnings": []})
            run_id = uuid.uuid4()
            async with SessionFactory() as session:
                await session.execute(text("""
                    INSERT INTO inha_policy.extraction_runs
                      (id, notice_version_id, schema_version, prompt_version, prompt_sha256,
                       provider_name, model_id, processing_code_version, model_parameters,
                       input_manifest, input_snapshot_sha256, status, parsed_output,
                       started_at, finished_at, sealed_at)
                    VALUES (:id, :version, :schema, 'fixture', :sha, 'fixture', 'fixture', 'fixture',
                            '{}', '{}', :sha, 'succeeded', CAST(:output AS jsonb), now(), now(), now())
                """), {"id": run_id, "version": version_id, "schema": EXTRACTION_SCHEMA_VERSION_V2,
                         "sha": hashlib.sha256(run_id.bytes).hexdigest(),
                         "output": bundle.model_dump_json()})
                versions = await OpportunityAssembler(session).assemble(
                    bundle=bundle, extraction_run_id=run_id, notice_version_id=version_id,
                    crawl_run_id=SEEDED["assembler_only"]["run"])
                ids = [(await session.execute(text(
                    "SELECT opportunity_id FROM inha_policy.opportunity_versions WHERE id=:id"
                ), {"id": version})).scalar_one() for version in versions]
                await session.commit()
            return ids

        first = await assemble([item("a", "2027년 다온 장학금", dawn, 2027),
                                item("b", "2027년 보람 장학금", bloom, 2027)])
        # Reworded names keep their opportunities; a changed year is a new edition.
        second = await assemble([item("a", "2027년도 다온장학생 선발", dawn, 2027),
                                 item("b", "2026년 보람 장학금", bloom, 2026)])
        self.assertEqual(second[0], first[0])
        self.assertNotIn(second[1], first)
        review = (await self._rows("""
            SELECT payload FROM inha_policy.review_items
            WHERE opportunity_id=:id AND payload->>'operation'='same_notice_unmatched'
        """, id=second[1]))[0]["payload"]
        verdicts = {row["opportunity_id"]: row["verdict"] for row in review["comparisons"]}
        self.assertEqual(verdicts[str(first[1])], "different_edition")
        # Two items can never land on the same existing opportunity.
        third = await assemble([item("a", "2027년 다온 장학금", dawn, 2027),
                                item("b", "2027년 다온 장학금", bloom, 2027)])
        self.assertEqual(third[0], first[0])
        self.assertNotEqual(third[1], first[0])

    # admin actions -------------------------------------------------------------------------

    async def test_csrf_and_role_are_enforced(self) -> None:
        opportunity = (await self._one("multi"))["opportunity_id"]
        response = await self.client.post(f"/admin/opportunities/{opportunity}/overrides",
                                          data={"csrf": "wrong", "field_path": "/title",
                                                "value_json": '"x"', "reason": "r"})
        self.assertEqual(response.status_code, 403)
        self.csrf = await self._login("operator")
        response = await self._post(f"/admin/opportunities/{opportunity}/overrides",
                                    field_path="/title", value_json='"x"', reason="r")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(await self._scalar(
            "SELECT count(*) FROM inha_policy.manual_overrides WHERE opportunity_id=:id",
            id=opportunity), 0)

    async def test_override_set_and_remove_are_append_only_and_public(self) -> None:
        target = (await self._opportunities(SEEDED["multi"]["version"]))[-1]
        opportunity = target["opportunity_id"]
        response = await self._post(f"/admin/opportunity-versions/{target['version_id']}/publish",
                                    reason="test")
        self.assertEqual(response.status_code, 303, response.text)
        response = await self._post(f"/admin/opportunities/{opportunity}/overrides",
                                    field_path="/title", value_json='"수정된 제목"', reason="오기 수정")
        self.assertEqual(response.status_code, 303)
        detail = (await self.client.get(f"/v1/opportunities/{opportunity}")).json()
        self.assertEqual(detail["title"], "수정된 제목")
        override_id = await self._scalar("""
            SELECT id FROM inha_policy.manual_overrides WHERE opportunity_id=:id AND action='set'
        """, id=opportunity)
        response = await self._post(f"/admin/overrides/{override_id}/remove", reason="원복")
        self.assertEqual(response.status_code, 303)
        detail = (await self.client.get(f"/v1/opportunities/{opportunity}")).json()
        self.assertEqual(detail["title"], target["title"])
        actions = await self._rows("""
            SELECT action FROM inha_policy.manual_overrides WHERE opportunity_id=:id ORDER BY created_at
        """, id=opportunity)
        self.assertEqual([row["action"] for row in actions], ["set", "remove"])
        missing = await self._post(f"/admin/opportunities/{uuid.uuid4()}/overrides",
                                   field_path="/title", value_json='"x"', reason="r")
        self.assertEqual(missing.status_code, 404)

    async def test_api_keys_issue_use_limit_and_revoke(self) -> None:
        anonymous = await self.client.get("/v1/opportunities", headers={"X-API-Key": ""})
        self.assertEqual(anonymous.status_code, 401)
        wrong = await self.client.get("/v1/opportunities", headers={"X-API-Key": "ph_not-a-key"})
        self.assertEqual(wrong.status_code, 401)
        self.csrf = await self._login("admin")
        page = await self._post("/admin/api-keys", name="연동 테스트", rate_limit_per_minute="2",
                                expires_days="30")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.headers["cache-control"], "no-store")
        key = re.search(r"(ph_[A-Za-z0-9_-]{40,})", page.text).group(1)
        digest = await self._scalar("SELECT key_sha256 FROM inha_policy.api_keys WHERE name='연동 테스트'")
        self.assertEqual(digest, hash_key(key))
        self.assertNotIn(key, json.dumps(await self._rows(
            "SELECT * FROM inha_policy.audit_logs WHERE action='api_key_created'"), default=str))
        statuses = [(await self.client.get("/v1/opportunities", headers={"X-API-Key": key})).status_code
                    for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 429])
        key_id = await self._scalar("SELECT id FROM inha_policy.api_keys WHERE name='연동 테스트'")
        self.assertEqual(await self._scalar(
            "SELECT request_count FROM inha_policy.api_keys WHERE id=:id", id=key_id), 1)
        response = await self._post(f"/admin/api-keys/{key_id}/revoke", reason="테스트 종료")
        self.assertEqual(response.status_code, 303)
        revoked = await self.client.get("/v1/opportunities", headers={"X-API-Key": key})
        self.assertEqual(revoked.status_code, 401)
        self.assertEqual((await self.client.get("/docs")).status_code in (200, 503), True)
        schema = (await self.client.get("/openapi.json")).json()
        self.assertIn("APIKeyHeader", schema["components"]["securitySchemes"])
        self.assertFalse(any(path.startswith("/admin") for path in schema["paths"]))

    async def test_merge_and_unmerge(self) -> None:
        winner, loser = [row["opportunity_id"]
                         for row in await self._opportunities(SEEDED["multi"]["version"])][:2]
        other = (await self._one("previous_year"))["opportunity_id"]
        response = await self._post(f"/admin/opportunities/{loser}/merge",
                                    winner_id=str(winner), reason="병합 테스트")
        self.assertEqual(response.status_code, 303)
        public = await self.client.get(f"/v1/opportunities/{loser}", follow_redirects=False)
        self.assertEqual(public.status_code, 308)
        decisions = await self._scalar("SELECT count(*) FROM inha_policy.identity_decisions")
        statuses = []
        for path, form in ((f"/admin/opportunities/{loser}/merge", {"winner_id": str(other)}),
                           (f"/admin/opportunities/{other}/merge", {"winner_id": str(loser)}),
                           (f"/admin/opportunities/{other}/merge", {"winner_id": str(uuid.uuid4())}),
                           (f"/admin/opportunities/{uuid.uuid4()}/merge/{winner}", {})):
            statuses.append((await self._post(path, reason="거부 확인", **form)).status_code)
        self.assertEqual(statuses, [409, 409, 404, 404])
        self.assertEqual(await self._scalar("SELECT count(*) FROM inha_policy.identity_decisions"),
                         decisions)
        response = await self._post(f"/admin/opportunities/{loser}/unmerge", reason="오병합")
        self.assertEqual(response.status_code, 303)
        state = (await self._rows("""
            SELECT o.lifecycle_status, o.merged_into_id, d.decision_kind
            FROM inha_policy.opportunities o
            JOIN inha_policy.identity_decisions d ON d.id=o.last_identity_decision_id WHERE o.id=:id
        """, id=loser))[0]
        self.assertEqual(state, {"lifecycle_status": "active", "merged_into_id": None,
                                 "decision_kind": "undo"})

    async def test_split_reuses_sealed_extraction(self) -> None:
        original = (await self._one("multi"))["opportunity_id"]
        runs_before = await self._scalar("SELECT count(*) FROM inha_policy.extraction_runs")
        bad = await self._post(f"/admin/opportunities/{original}/split",
                               notice_version_id=str(SEEDED["multi"]["version"]),
                               target_item_index="9", reason="잘못된 항목")
        self.assertEqual(bad.status_code, 422)
        response = await self._post(f"/admin/opportunities/{original}/split",
                                    notice_version_id=str(SEEDED["multi"]["version"]),
                                    target_item_index="1", reason="별도 사업")
        self.assertEqual(response.status_code, 303)
        await self._drain()
        self.assertEqual(await self._scalar("SELECT count(*) FROM inha_policy.extraction_runs"),
                         runs_before, "split must reuse the sealed extraction, not call the model")
        new_id = await self._scalar("""
            SELECT other_opportunity_id FROM inha_policy.identity_decisions
            WHERE opportunity_id=:id AND decision_kind='split'
        """, id=original)
        versions = await self._rows("""
            SELECT edit_kind FROM inha_policy.opportunity_versions WHERE opportunity_id=:id
        """, id=new_id)
        self.assertEqual(versions, [{"edit_kind": "split"}])

    async def test_failed_job_retry(self) -> None:
        seeded = SEEDED["out_of_scope"]
        job_id = uuid.uuid4()
        async with SessionFactory() as session:
            await session.execute(text("""
                INSERT INTO inha_policy.crawl_jobs
                  (id, crawl_run_id, stage, job_key, notice_id, notice_version_id, payload, max_attempts)
                VALUES (:id, :run, 'parse_document', :key, :notice, :version, CAST(:payload AS jsonb), 1)
            """), {"id": job_id, "run": seeded["run"], "key": f"broken:{job_id}",
                     "notice": seeded["notice"], "version": seeded["version"],
                     "payload": json.dumps({"origin": "asset", "asset_occurrence_id": str(uuid.uuid4())})})
            await session.commit()
        await self._drain()
        self.assertEqual(await self._scalar(
            "SELECT status FROM inha_policy.crawl_jobs WHERE id=:id", id=job_id), "failed")
        self.csrf = await self._login("operator")
        response = await self._post(f"/admin/jobs/{job_id}/retry", reason="재시도")
        self.assertEqual(response.status_code, 303)
        row = (await self._rows("""
            SELECT status, attempt_count, error_code FROM inha_policy.crawl_jobs WHERE id=:id
        """, id=job_id))[0]
        self.assertEqual(row, {"status": "queued", "attempt_count": 0, "error_code": None})
        await self._drain()

    # quality and reuse ---------------------------------------------------------------------

    async def test_partial_documents_cap_quality(self) -> None:
        key = "previous_year"
        document_id = await self._new_attempt(SEEDED[key]["version"], "partial")
        response = await self._post(f"/admin/notice-versions/{SEEDED[key]['version']}/extract",
                                    reason="ocr attempt")
        self.assertEqual(response.status_code, 303)
        await self._drain()
        latest = (await self._rows("""
            SELECT ov.data_quality_status, ov.quality_flags,
                   (SELECT count(*) FROM inha_policy.field_evidence e
                    WHERE e.opportunity_version_id=ov.id AND e.document_id<>:document) AS stale
            FROM inha_policy.opportunity_versions ov
            JOIN inha_policy.opportunity_version_sources s ON s.opportunity_version_id=ov.id
            WHERE s.notice_version_id=:id ORDER BY ov.created_at DESC LIMIT 1
        """, id=SEEDED[key]["version"], document=document_id))[0]
        # needs_review (model warnings) outranks partial; complete is never allowed here.
        self.assertIn(latest["data_quality_status"], {"partial", "needs_review"})
        if latest["data_quality_status"] == "partial":
            self.assertIn("provenance_document_partial", latest["quality_flags"])
        self.assertEqual(latest["stale"], 0, "evidence must point at the newest attempt")

    async def test_preserved_output_is_revalidated_without_new_call(self) -> None:
        version_id = SEEDED["repost"]["version"]
        await self._new_attempt(version_id, "succeeded")
        async with SessionFactory() as session:
            _, _, manifest, input_hash = await worker_module.extraction_input(session, version_id)
            target = ProviderRegistry(await resolved_settings(session)).extraction()
            previous = (await session.execute(text("""
                SELECT parsed_output FROM inha_policy.extraction_runs
                WHERE notice_version_id=:id AND status='succeeded' ORDER BY finished_at DESC LIMIT 1
            """), {"id": version_id})).scalar_one()
            # The new attempt has new block ids; remap the stored output onto them.
            current = {item["id"] for item in manifest["blocks"]}
            by_text: dict[str, list[str]] = {}
            for block_id, content in (await session.execute(text("""
                SELECT b.id::text, b.text_content FROM inha_policy.document_blocks b
                JOIN inha_policy.documents d ON d.id=b.document_id WHERE d.notice_version_id=:id
            """), {"id": version_id})).all():
                by_text.setdefault(content, []).append(block_id)
            content = json.dumps(previous, ensure_ascii=False)
            for ids in by_text.values():
                new = next(item for item in ids if item in current)
                for old in ids:
                    content = content.replace(old, new)
            await session.execute(text("""
                INSERT INTO inha_policy.extraction_runs
                  (notice_version_id, schema_version, prompt_version, prompt_sha256, provider_name,
                   model_id, processing_code_version, model_parameters, input_manifest,
                   input_snapshot_sha256, status, raw_response, error_code, started_at,
                   finished_at, sealed_at)
                VALUES (:version, :schema, :prompt, :prompt_sha, :provider, :model, 'fixture', '{}',
                        CAST(:manifest AS jsonb), :input_sha, 'failed', CAST(:raw AS jsonb),
                        'StructuredOutputError', now(), now(), now())
            """), {"version": version_id, "schema": EXTRACTION_SCHEMA_VERSION_V2,
                     "prompt": EXTRACTION_PROMPT_VERSION_V2,
                     "prompt_sha": hashlib.sha256(EXTRACTION_SYSTEM_PROMPT_V2.encode()).hexdigest(),
                     "provider": target.provider_name, "model": target.model,
                     "manifest": json.dumps(manifest), "input_sha": input_hash,
                     "raw": json.dumps({"responses": [{"choices": [{"message": {"content": content}}]}]},
                                       ensure_ascii=False)})
            await session.commit()
        response = await self._post(f"/admin/notice-versions/{version_id}/extract", reason="재검증")
        self.assertEqual(response.status_code, 303)
        await self._drain()
        newest = (await self._rows("""
            SELECT status, raw_response ? 'revalidated_from' AS revalidated
            FROM inha_policy.extraction_runs WHERE notice_version_id=:id
            ORDER BY started_at DESC LIMIT 1
        """, id=version_id))[0]
        self.assertEqual(newest, {"status": "succeeded", "revalidated": True})

    # revisions -----------------------------------------------------------------------------

    async def test_verified_extension_creates_new_immutable_version(self) -> None:
        base = await self._one("original")
        opportunity = base["opportunity_id"]
        windows = await self._rows("""
            SELECT window_key, end_date::text AS end_date, end_time::text AS end_time
            FROM inha_policy.application_windows WHERE opportunity_version_id=:id
        """, id=base["version_id"])
        target = next((row for row in windows if row["end_date"] == "2026-10-01"), None)
        self.assertIsNotNone(target, f"the model did not extract the 10/1 deadline: {windows}")
        response = await self._post(f"/admin/opportunity-versions/{base['version_id']}/publish",
                                    reason="원공고")
        self.assertEqual(response.status_code, 303, response.text)

        reviews = await self._rows("""
            SELECT id FROM inha_policy.review_items
            WHERE review_kind='revision_candidate' AND entity_id=:id
        """, id=SEEDED["extension"]["version"])
        self.assertTrue(reviews, "the model should flag the extension notice as a revision")
        page = await self.client.get(f"/admin/reviews/{reviews[0]['id']}/revision",
                                     params={"opportunity_id": str(opportunity)})
        self.assertEqual(page.status_code, 200)
        run_id = await self._scalar("""
            SELECT id FROM inha_policy.extraction_runs WHERE notice_version_id=:id AND status='succeeded'
            ORDER BY finished_at DESC LIMIT 1
        """, id=SEEDED["extension"]["version"])
        intent = await self._block("extension", r"연장합니다")
        new_value = await self._block("extension", r"2026\. 10\. 8\.\(목\) 17:00")
        form = {"notice_version_id": str(SEEDED["extension"]["version"]),
                "extraction_run_id": str(run_id), "kind": "extension", "review_id": str(reviews[0]["id"]),
                "intent_block_id": str(intent["id"]), "intent_quote": intent["quote"],
                "patches_json": json.dumps([{
                    "field_path": f"/application_windows/{target['window_key']}/end",
                    "value": {"date": "2026-10-08", "time": "17:00"},
                    "block_id": str(new_value["id"]), "quote": new_value["quote"]}]),
                "same_cycle_verified": "on", "same_scope_verified": "on",
                "new_value_verified": "on", "reason": "연장 공고 확인"}
        path = f"/admin/opportunities/{opportunity}/revisions"
        self.assertEqual((await self._post(path, **{**form, "new_value_verified": ""})).status_code, 422)
        wrong = json.loads(form["patches_json"])
        wrong[0]["quote"] = "2026. 12. 31."
        self.assertEqual((await self._post(path, **{**form, "patches_json": json.dumps(wrong)})).status_code,
                         422)
        same_notice = await self._post(path, **{**form, "notice_version_id": str(SEEDED["original"]["version"])})
        self.assertIn(same_notice.status_code, {404, 409, 422})

        response = await self._post(path, **form)
        self.assertEqual(response.status_code, 303, response.text)
        versions = await self._rows("""
            SELECT ov.id, ov.edit_kind, w.end_date::text AS end_date, w.end_time::text AS end_time
            FROM inha_policy.opportunity_versions ov
            JOIN inha_policy.application_windows w ON w.opportunity_version_id=ov.id
            WHERE ov.opportunity_id=:id AND w.window_key=:key ORDER BY ov.version_no
        """, id=opportunity, key=target["window_key"])
        self.assertEqual([(row["end_date"], row["end_time"]) for row in versions[-2:]],
                         [("2026-10-01", "17:00:00"), ("2026-10-08", "17:00:00")])
        self.assertEqual(versions[-1]["edit_kind"], "extension")
        new_version = versions[-1]["id"]
        resolution = (await self._rows("""
            SELECT status, supersession_edges FROM inha_policy.field_resolutions
            WHERE opportunity_version_id=:id
        """, id=new_version))[0]
        self.assertEqual(resolution["status"], "resolved_explicit_update")
        self.assertGreaterEqual(len(resolution["supersession_edges"]), 1)
        self.assertEqual(await self._scalar("""
            SELECT count(*) FROM inha_policy.field_evidence
            WHERE opportunity_version_id=:id AND candidate_status='superseded'
        """, id=base["version_id"]), 0, "the base version must not change")
        self.assertEqual(await self._scalar("SELECT status FROM inha_policy.review_items WHERE id=:id",
                                            id=reviews[0]["id"]), "resolved")

        await self._drain()
        response = await self._post(f"/admin/opportunity-versions/{new_version}/publish", reason="연장 공개")
        self.assertEqual(response.status_code, 303, response.text)
        detail = (await self.client.get(f"/v1/opportunities/{opportunity}")).json()
        self.assertIn("2026-10-08", [row["end_date"] for row in detail["application_windows"]])
        self.assertIn("extension", {row["relation_kind"] for row in detail["source_urls"]})
        kinds = [row["event_kind"] for row in await self._rows("""
            SELECT event_kind FROM inha_policy.change_events WHERE opportunity_id=:id ORDER BY event_seq
        """, id=opportunity)]
        self.assertEqual(kinds[-1], "extended")


if __name__ == "__main__":
    unittest.main()
