"""Wire rules shared with the attached Spring ImporterClient/ImportJobService."""
import hashlib
import hmac
import unittest
import uuid
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import SecretStr

from policy_harvester.config import Settings
from policy_harvester.import_jobs import Prefill, callback_url, failed, signature, web_url


class ImportContractTests(unittest.TestCase):
    def test_signature_matches_java_utf8_raw_body_contract(self):
        body = '{"requestId":"123","result":{"title":"청년 문화패스"}}'.encode()
        timestamp = '1793362943'
        expected = hmac.new(b'shared', timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        self.assertEqual(signature('shared', timestamp, body), expected)
        self.assertNotEqual(signature('shared', timestamp, body + b' '), expected)

    def test_dates_times_nulls_and_five_backend_categories(self):
        value = Prefill(title='  장학금  ', organization=None, applyEndDate='2026-02-30',
                        applyEndTime='25:00', category='교육', memo=None)
        self.assertEqual(value.title, '장학금')
        self.assertIsNone(value.applyEndDate)
        self.assertIsNone(value.applyEndTime)
        self.assertEqual(value.category, '교육')
        for bad in ('26-1-1', '2026-1-01', '2026-11-01T00:00:00'):
            self.assertIsNone(value.model_copy().model_validate({**value.model_dump(), 'applyEndDate': bad}).applyEndDate)

    def test_callback_only_uses_configured_backend_and_request_id(self):
        request_id = uuid.uuid4()
        settings = Settings(import_callback_base_url='https://backend.example',
                            import_callback_secret=SecretStr('shared'))
        expected = f'https://backend.example/api/v1/internal/import-jobs/{request_id}/result'
        with patch('policy_harvester.import_jobs.get_settings', return_value=settings):
            self.assertEqual(callback_url(expected, request_id), expected)
            self.assertIsNone(callback_url('', request_id))
            for url in ('https://other.example/result', expected + '?redirect=1',
                        f'https://backend.example/api/v1/internal/import-jobs/{uuid.uuid4()}/result'):
                with self.assertRaises(HTTPException):
                    callback_url(url, request_id)

    def test_nonpublic_and_credential_urls_rejected(self):
        for url in ('file:///etc/passwd', 'http://127.0.0.1/x', 'http://[::1]/',
                    'http://169.254.169.254/', 'http://10.1.2.3/', 'http://localhost/',
                    'https://user:pass@example.com/', 'https://example.com:8443/'):
            with self.assertRaises(HTTPException, msg=url):
                web_url(url)
        self.assertEqual(web_url('https://example.go.kr/notice/123'), 'https://example.go.kr/notice/123')

    def test_errors_are_backend_known_codes_and_display_safe(self):
        for code in ('FETCH_FAILED', 'NOT_A_NOTICE', 'UNREADABLE_IMAGE', 'TIMEOUT', 'INTERNAL'):
            result = failed(uuid.uuid4(), code)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['error']['code'], code)
            self.assertLessEqual(len(result['error']['message']), 200)


class ImportExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_url_extraction_preserves_source_and_does_not_invent_time(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock
        from policy_harvester import import_jobs as jobs
        extraction = jobs.Extraction(isNotice=True, result=Prefill(
            title=' 장학 안내 ', organization=None, applyEndDate=None,
            applyEndTime='18:00', category='교육', memo=None))
        provider = SimpleNamespace(structured=AsyncMock(return_value=SimpleNamespace(value=extraction)))
        registry = MagicMock()
        registry.extraction.return_value = SimpleNamespace(provider=provider, model='test', parameters={})
        with patch.object(jobs, 'fetch_text', AsyncMock(return_value='장학금 공고')), \
             patch.object(jobs, 'resolved_settings', AsyncMock(return_value=Settings())), \
             patch.object(jobs, 'ProviderRegistry', return_value=registry):
            result = await jobs.extract({'request_id': uuid.uuid4(), 'input_type': 'url',
                                         'source_url': 'https://example.com/original'})
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['result']['title'], '장학 안내')
        self.assertIsNone(result['result']['applyEndTime'])
        self.assertEqual(result['result']['sourceUrl'], 'https://example.com/original')
        self.assertIn('document', provider.structured.call_args.kwargs['payload'])

    async def test_image_is_transcribed_before_structuring_and_source_is_null(self):
        import io
        from PIL import Image
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock
        from policy_harvester import import_jobs as jobs
        image = io.BytesIO()
        Image.new('RGB', (300, 200)).save(image, 'PNG')
        extraction = jobs.Extraction(isNotice=True, result=Prefill(title='문화패스', organization=None,
            applyEndDate='2026-11-20', applyEndTime='18:00', category='복지문화', memo=None))
        provider = SimpleNamespace(structured=AsyncMock(return_value=SimpleNamespace(value=extraction)),
                                   transcribe_image=AsyncMock(return_value=SimpleNamespace(text='문화패스 공고')))
        registry = MagicMock()
        registry.llm.return_value = provider
        registry.extraction.return_value = SimpleNamespace(provider=provider, model='test', parameters={})
        with patch.object(jobs, 'resolved_settings', AsyncMock(return_value=Settings())), \
             patch.object(jobs, 'ProviderRegistry', return_value=registry):
            result = await jobs.extract({'request_id': uuid.uuid4(), 'input_type': 'image',
                                         'source_url': None, 'image_bytes': image.getvalue()})
        self.assertIsNone(result['result']['sourceUrl'])
        self.assertEqual(provider.structured.call_args.kwargs['payload']['document'], '문화패스 공고')
        provider.transcribe_image.assert_awaited_once()

    async def test_all_captures_are_combined_in_order_before_one_extraction(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock
        from policy_harvester import import_jobs as jobs
        extraction = jobs.Extraction(isNotice=True, result=Prefill(title='장학 안내', organization=None,
            applyEndDate='2026-11-20', applyEndTime='18:00', category='교육', memo='등록금 지원'))
        async def transcribe(**kwargs):
            # Later pages can finish first without changing document order.
            import asyncio
            if kwargs['image'] == b'first':
                await asyncio.sleep(0.01)
                return SimpleNamespace(text='제목: 장학 안내')
            return SimpleNamespace(text='신청 마감: 2026-11-20 18:00')
        provider = SimpleNamespace(structured=AsyncMock(return_value=SimpleNamespace(value=extraction)),
                                   transcribe_image=AsyncMock(side_effect=transcribe))
        registry = MagicMock()
        registry.llm.return_value = provider
        registry.extraction.return_value = SimpleNamespace(provider=provider, model='test', parameters={})
        with patch.object(jobs, 'resolved_settings', AsyncMock(return_value=Settings())), \
             patch.object(jobs, 'ProviderRegistry', return_value=registry), \
             patch.object(jobs, 'prepare_image', side_effect=lambda value: value):
            result = await jobs.extract({'request_id': uuid.uuid4(), 'input_type': 'image',
                'source_url': None, 'image_bytes': b'first', 'extra_images': [b'second']})
        self.assertEqual(result['status'], 'succeeded')
        provider.structured.assert_awaited_once()
        self.assertEqual(provider.transcribe_image.await_count, 2)
        document = provider.structured.call_args.kwargs['payload']['document']
        self.assertLess(document.index('제목: 장학 안내'), document.index('신청 마감:'))
        self.assertIn('[캡처 2/2]', document)

    async def test_unreadable_later_capture_fails_instead_of_silently_dropping_it(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock
        from policy_harvester import import_jobs as jobs
        provider = SimpleNamespace(structured=AsyncMock(), transcribe_image=AsyncMock(
            side_effect=[SimpleNamespace(text='장학 안내'), SimpleNamespace(text='[판독불가]')]))
        registry = MagicMock()
        registry.llm.return_value = provider
        with patch.object(jobs, 'resolved_settings', AsyncMock(return_value=Settings())), \
             patch.object(jobs, 'ProviderRegistry', return_value=registry), \
             patch.object(jobs, 'prepare_image', side_effect=lambda value: value):
            result = await jobs.extract({'request_id': uuid.uuid4(), 'input_type': 'image',
                'source_url': None, 'image_bytes': b'first', 'extra_images': [b'second']})
        self.assertEqual(result['error']['code'], 'UNREADABLE_IMAGE')
        provider.structured.assert_not_awaited()

    async def test_redirect_cannot_fetch_private_destination(self):
        import httpx
        from unittest.mock import AsyncMock
        from policy_harvester import import_jobs as jobs
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
        client_type = httpx.AsyncClient
        async def validate(url):
            jobs.web_url(url)
        with patch.object(jobs, 'public_destination', AsyncMock(side_effect=validate)), \
             patch.object(jobs.httpx, 'AsyncClient', side_effect=lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw)):
            with self.assertRaises(HTTPException):
                await jobs.fetch_text('https://example.com/public')
        self.assertEqual(len(calls), 1)
