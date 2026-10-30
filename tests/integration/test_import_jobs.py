"""Import API/worker integration against a migrated disposable PostgreSQL database.

No network model calls: extraction and callback HTTP are mocked at their boundary.
Run separately from the real-LLM integration suite documented in README.
"""
import asyncio
import io
import json
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image
from sqlalchemy import text

from policy_harvester.api import app
from policy_harvester.apikeys import generate_key
from policy_harvester.db import SessionFactory, engine
from policy_harvester import import_jobs as jobs
from policy_harvester.config import Settings
from pydantic import SecretStr


class ImportJobsIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.key, prefix, digest = generate_key()
        self.owner = uuid.uuid4()
        async with SessionFactory() as session:
            await session.execute(text('''INSERT INTO inha_policy.api_keys
                (id, name, key_prefix, key_sha256) VALUES (:id, 'import-test', :prefix, :digest)'''),
                {'id': self.owner, 'prefix': prefix, 'digest': digest})
            await session.commit()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                       headers={'X-API-Key': self.key})

    async def asyncTearDown(self):
        await self.client.aclose()
        async with SessionFactory() as session:
            await session.execute(text('DELETE FROM inha_policy.import_jobs WHERE api_key_id=:id'), {'id': self.owner})
            await session.execute(text('DELETE FROM inha_policy.api_keys WHERE id=:id'), {'id': self.owner})
            await session.commit()
        await engine.dispose()

    async def submit(self, request_id=None, **extra):
        return await self.client.post('/v1/import-jobs', json={
            'requestId': str(request_id or uuid.uuid4()), 'type': 'url',
            'url': 'https://example.go.kr/notice/123', **extra})

    async def test_immediate_accept_idempotency_ownership_and_polling(self):
        request_id = uuid.uuid4()
        responses = await asyncio.gather(self.submit(request_id), self.submit(request_id))
        self.assertTrue(all(r.status_code == 202 for r in responses), [r.text for r in responses])
        self.assertEqual(responses[0].json(), {'requestId': str(request_id), 'status': 'queued'})
        async with SessionFactory() as session:
            count = await session.scalar(text('SELECT count(*) FROM inha_policy.import_jobs WHERE request_id=:id'),
                                         {'id': request_id})
            self.assertEqual(count, 1)
        poll = await self.client.get(f'/v1/import-jobs/{request_id}')
        self.assertEqual(poll.json()['status'], 'queued')
        other_key, prefix, digest = generate_key()
        other_id = uuid.uuid4()
        async with SessionFactory() as session:
            await session.execute(text('''INSERT INTO inha_policy.api_keys (id,name,key_prefix,key_sha256)
                VALUES (:id,'other-test',:prefix,:digest)'''), {'id': other_id, 'prefix': prefix, 'digest': digest})
            await session.commit()
        try:
            denied = await self.client.get(f'/v1/import-jobs/{request_id}', headers={'X-API-Key': other_key})
            self.assertEqual(denied.status_code, 404)
            denied = await self.client.post('/v1/import-jobs', headers={'X-API-Key': other_key}, json={
                'requestId': str(request_id), 'type': 'url', 'url': 'https://example.go.kr/other'})
            self.assertEqual(denied.status_code, 404)
        finally:
            async with SessionFactory() as session:
                await session.execute(text('DELETE FROM inha_policy.api_keys WHERE id=:id'), {'id': other_id})
                await session.commit()

    async def test_auth_bad_requests_and_image_size(self):
        response = await self.client.post('/v1/import-jobs', headers={'X-API-Key': 'wrong'}, json={})
        self.assertEqual(response.status_code, 401)
        for body in ({}, {'requestId': 'bad', 'type': 'url', 'url': 'https://example.com'},
                     {'requestId': str(uuid.uuid4()), 'type': 'url', 'url': 'http://127.0.0.1/'}):
            self.assertEqual((await self.client.post('/v1/import-jobs', json=body)).status_code, 400)
        data = {'requestId': str(uuid.uuid4()), 'type': 'image'}
        response = await self.client.post('/v1/import-jobs', data=data,
                                         files={'image': ('x.gif', b'not image', 'image/gif')})
        self.assertEqual(response.status_code, 400)
        response = await self.client.post('/v1/import-jobs', data=data,
                                         files={'image': ('x.png', b'x' * (jobs.MAX_IMAGE + 1), 'image/png')})
        self.assertEqual(response.status_code, 413)
        image = io.BytesIO()
        Image.new('RGB', (300, 200), 'white').save(image, format='PNG')
        response = await self.client.post('/v1/import-jobs', data=data,
                                         files={'image': ('x.png', image.getvalue(), 'image/png')})
        self.assertEqual(response.status_code, 202, response.text)

    async def test_worker_terminal_result_and_expiry(self):
        request_id = uuid.uuid4()
        await self.submit(request_id)
        result = {'requestId': str(request_id), 'status': 'succeeded', 'result': {
            'title': '청년 문화패스', 'organization': None, 'applyEndDate': '2026-11-20',
            'applyEndTime': '18:00', 'category': '복지문화', 'memo': None,
            'sourceUrl': 'https://example.go.kr/notice/123'}}
        with patch.object(jobs, 'extract', AsyncMock(return_value=result)) as extract:
            self.assertTrue(await jobs.process_one())
            self.assertFalse(await jobs.process_one())
            extract.assert_awaited_once()
        self.assertEqual((await self.client.get(f'/v1/import-jobs/{request_id}')).json(), result)
        self.assertEqual((await self.submit(request_id)).json()['status'], 'succeeded')
        async with SessionFactory() as session:
            await session.execute(text("UPDATE inha_policy.import_jobs SET expires_at=now()-interval '1 second' WHERE request_id=:id"), {'id': request_id})
            await session.commit()
        self.assertEqual((await self.client.get(f'/v1/import-jobs/{request_id}')).status_code, 404)
        await jobs.process_one()
        async with SessionFactory() as session:
            self.assertEqual(await session.scalar(text('SELECT count(*) FROM inha_policy.import_jobs WHERE request_id=:id'), {'id': request_id}), 0)

    async def test_stale_claim_becomes_timeout_after_restart(self):
        request_id = uuid.uuid4()
        await self.submit(request_id)
        async with SessionFactory() as session:
            await session.execute(text("UPDATE inha_policy.import_jobs SET status='running', started_at=now()-interval '61 seconds' WHERE request_id=:id"), {'id': request_id})
            await session.commit()
        await jobs.process_one()
        response = (await self.client.get(f'/v1/import-jobs/{request_id}')).json()
        self.assertEqual(response['error']['code'], 'TIMEOUT')

    async def test_callback_signs_exact_body_retries_and_poll_result_survives(self):
        request_id = uuid.uuid4()
        await self.submit(request_id)
        response = jobs.failed(request_id, 'NOT_A_NOTICE')
        callback = f'https://backend.example/api/v1/internal/import-jobs/{request_id}/result'
        async with SessionFactory() as session:
            await session.execute(text('''UPDATE inha_policy.import_jobs SET status='failed',
                response=CAST(:body AS jsonb), callback_url=:callback, callback_due_at=now()
                WHERE request_id=:id'''), {'id': request_id, 'body': json.dumps(response), 'callback': callback})
            await session.commit()
        settings = Settings(import_callback_base_url='https://backend.example', import_callback_secret=SecretStr('shared'))
        requests = []
        statuses = iter([500, 200])
        def handler(request):
            requests.append(request)
            self.assertEqual(request.headers['X-Signature'], jobs.signature('shared', request.headers['X-Timestamp'], request.content))
            self.assertEqual(json.loads(request.content), response)
            return httpx.Response(next(statuses))
        client_type = httpx.AsyncClient
        with patch.object(jobs, 'get_settings', return_value=settings), \
             patch.object(jobs, 'public_destination', AsyncMock()), \
             patch.object(jobs.httpx, 'AsyncClient', side_effect=lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw)):
            await jobs.deliver_one()
            async with SessionFactory() as session:
                row = (await session.execute(text('SELECT callback_attempts, callback_due_at-now() AS delay FROM inha_policy.import_jobs WHERE request_id=:id'), {'id': request_id})).mappings().one()
                self.assertEqual(row['callback_attempts'], 1)
                self.assertGreater(row['delay'].total_seconds(), 8)
                await session.execute(text('UPDATE inha_policy.import_jobs SET callback_due_at=now() WHERE request_id=:id'), {'id': request_id})
                await session.commit()
            await jobs.deliver_one()
        self.assertEqual(len(requests), 2)
        async with SessionFactory() as session:
            self.assertIsNone(await session.scalar(text('SELECT callback_due_at FROM inha_policy.import_jobs WHERE request_id=:id'), {'id': request_id}))
        self.assertEqual((await self.client.get(f'/v1/import-jobs/{request_id}')).json(), response)

    async def test_callback_stops_after_three_retries(self):
        request_id = uuid.uuid4()
        await self.submit(request_id)
        callback = f'https://backend.example/api/v1/internal/import-jobs/{request_id}/result'
        async with SessionFactory() as session:
            await session.execute(text('''UPDATE inha_policy.import_jobs SET status='failed',
                response=CAST(:body AS jsonb), callback_url=:callback, callback_due_at=now()
                WHERE request_id=:id'''), {'id': request_id, 'body': json.dumps(jobs.failed(request_id, 'TIMEOUT')), 'callback': callback})
            await session.commit()
        settings = Settings(import_callback_base_url='https://backend.example', import_callback_secret=SecretStr('shared'))
        client_type = httpx.AsyncClient
        sent = []
        def handler(request):
            sent.append(request)
            return httpx.Response(503)
        with patch.object(jobs, 'get_settings', return_value=settings), \
             patch.object(jobs, 'public_destination', AsyncMock()), \
             patch.object(jobs.httpx, 'AsyncClient', side_effect=lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw)):
            for attempt, delay in enumerate((10, 60, 300, None), 1):
                self.assertTrue(await jobs.deliver_one())
                async with SessionFactory() as session:
                    row = (await session.execute(text('SELECT callback_attempts, callback_due_at-now() AS delay FROM inha_policy.import_jobs WHERE request_id=:id'), {'id': request_id})).mappings().one()
                    self.assertEqual(row['callback_attempts'], attempt)
                    if delay is None:
                        self.assertIsNone(row['delay'])
                    else:
                        self.assertGreater(row['delay'].total_seconds(), delay-2)
                        self.assertLessEqual(row['delay'].total_seconds(), delay)
                        await session.execute(text('UPDATE inha_policy.import_jobs SET callback_due_at=now() WHERE request_id=:id'), {'id': request_id})
                        await session.commit()
            self.assertFalse(await jobs.deliver_one())
        self.assertEqual(len(sent), 4)
