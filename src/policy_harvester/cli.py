from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import uuid

from argon2 import PasswordHasher
from sqlalchemy import text

from .crawling import CrawlService
from .db import SessionFactory
from .config import get_settings
from .storage import build_object_store


async def init_database(email: str, display_name: str) -> None:
    password = getpass.getpass("Initial admin password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation or len(password) < 12:
        raise SystemExit("passwords must match and contain at least 12 characters")
    digest = PasswordHasher().hash(password)
    async with SessionFactory() as session:
        await session.execute(text("""
            INSERT INTO inha_policy.sources
              (source_key, name, base_url, list_url, board_key, crawl_config)
            VALUES ('inha-kr-8', '인하대학교 장학 게시판', 'https://www.inha.ac.kr',
                    'https://www.inha.ac.kr/bbs/kr/8/artclList.do?bbsClSeq=215', 'kr/8',
                    '{"adapter":"inha_scholarship","category_keys":["215"],
                      "category_names":{"215":"장학"}}'::jsonb)
            ON CONFLICT (source_key) DO NOTHING
        """))
        await session.execute(text("""
            INSERT INTO inha_policy.admin_users
              (email, display_name, password_hash, role)
            VALUES (:email, :name, :password, 'admin')
            ON CONFLICT (email) DO UPDATE SET display_name=EXCLUDED.display_name,
              password_hash=EXCLUDED.password_hash, role='admin', is_active=true,
              updated_at=now()
        """), {"email": email.lower(), "name": display_name, "password": digest})
        await session.commit()


async def crawl(source_key: str, max_pages: int | None = None,
                max_notices: int | None = None) -> None:
    async with SessionFactory() as session:
        service = CrawlService(session)
        try:
            run_id = await service.run(source_key, "manual", max_pages=max_pages,
                                       max_notices=max_notices)
            print(run_id)
        finally:
            await service.close()


async def verify_objects() -> None:
    store = build_object_store(get_settings())
    async with SessionFactory() as session:
        rows = (await session.execute(text(
            "SELECT storage_key, sha256, byte_size FROM inha_policy.binary_assets ORDER BY storage_key"
        ))).mappings().all()
    failed: list[str] = []
    for row in rows:
        try:
            payload = store.get(row["storage_key"])
        except Exception as exc:
            failed.append(f"{row['storage_key']}: unavailable: {exc}")
            continue
        digest = hashlib.sha256(payload).hexdigest()
        if digest != row["sha256"] or len(payload) != row["byte_size"]:
            failed.append(f"{row['storage_key']}: hash or size mismatch")
    print(f"verified={len(rows) - len(failed)} failed={len(failed)} total={len(rows)}")
    if failed:
        for value in failed[:20]:
            print(value)
        raise SystemExit(1)


async def enqueue_unprocessed(limit: int | None, dry_run: bool) -> None:
    """Queue parsing for current in-scope notice versions that never entered the pipeline."""
    async with SessionFactory() as session:
        rows = (await session.execute(text("""
            SELECT nv.id, nv.notice_id, nv.crawl_run_id, nv.title
            FROM inha_policy.notice_versions nv
            JOIN inha_policy.notices n ON n.id=nv.notice_id
            -- The crawler deliberately leaves out-of-scope notices unqueued.
            WHERE n.scope_status='included' AND n.availability_status='available'
              AND n.current_notice_version_id=nv.id
              AND NOT EXISTS (SELECT 1 FROM inha_policy.crawl_jobs j
                              WHERE j.notice_version_id=nv.id)
              AND NOT EXISTS (SELECT 1 FROM inha_policy.documents d
                              WHERE d.notice_version_id=nv.id)
            ORDER BY nv.observed_at
            LIMIT :limit
        """), {"limit": limit})).mappings().all()
        for row in rows:
            print(f"{row['id']} {row['title']}")
        if dry_run:
            print(f"would_queue={len(rows)}")
            return
        service = CrawlService(session)
        try:
            for row in rows:
                await service._enqueue_parse_jobs(row["crawl_run_id"], row["notice_id"], row["id"])
            await session.commit()
        finally:
            await service.close()
        print(f"queued_versions={len(rows)}")


async def propose_identity_candidates(limit: int | None) -> None:
    """Backfill cross-notice candidates for initial versions that were never compared."""
    from .pipeline.identity import IdentityCandidateService

    async with SessionFactory() as session:
        versions = (await session.execute(text("""
            SELECT ov.id FROM inha_policy.opportunity_versions ov
            JOIN inha_policy.opportunities o ON o.id=ov.opportunity_id
            WHERE ov.edit_kind='initial' AND o.lifecycle_status <> 'merged'
              AND NOT EXISTS (SELECT 1 FROM inha_policy.review_items r
                              WHERE r.opportunity_id=ov.opportunity_id
                                AND r.payload->>'operation'='cross_notice_candidates')
            ORDER BY ov.observed_at LIMIT :limit
        """), {"limit": limit})).scalars().all()
        proposals = await IdentityCandidateService(session).propose_for_versions(list(versions))
        await session.commit()
    print(f"compared_versions={len(versions)} proposals={len(proposals)}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="policy-admin")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--email", required=True)
    init.add_argument("--display-name", default="Administrator")
    crawl_command = sub.add_parser("crawl")
    crawl_command.add_argument("--source", default="inha-kr-8")
    crawl_command.add_argument("--max-pages", type=int)
    crawl_command.add_argument("--max-notices", type=int)
    sub.add_parser("verify-objects")
    enqueue = sub.add_parser("enqueue-unprocessed")
    enqueue.add_argument("--limit", type=int)
    enqueue.add_argument("--dry-run", action="store_true")
    propose = sub.add_parser("propose-identity-candidates")
    propose.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.command == "init":
        asyncio.run(init_database(args.email, args.display_name))
    elif args.command == "crawl":
        asyncio.run(crawl(args.source, args.max_pages, args.max_notices))
    elif args.command == "propose-identity-candidates":
        asyncio.run(propose_identity_candidates(args.limit))
    elif args.command == "enqueue-unprocessed":
        asyncio.run(enqueue_unprocessed(args.limit, args.dry_run))
    else:
        asyncio.run(verify_objects())


if __name__ == "__main__":
    main()
