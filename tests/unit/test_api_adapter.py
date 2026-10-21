"""Synthetic contracts for the versioned FastAPI inbound adapter."""

import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from autofill.adapters.http.app import MAX_WEB_UPLOAD_BYTES, create_app
from autofill.adapters.http.export_openapi import rendered_openapi
from autofill.service import WorkflowService
from tests.helpers import pdf


ORIGIN = 'http://127.0.0.1:8765'
API = '/api/v1'


class SyncASGIClient:
    def __init__(self, runner, app, *, base_url=ORIGIN, client=('127.0.0.1', 12345)):
        self.runner = runner
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=client), base_url=base_url,
        )

    def get(self, *args, **kwargs):
        return self.runner.run(self.client.get(*args, **kwargs))

    def post(self, *args, **kwargs):
        return self.runner.run(self.client.post(*args, **kwargs))

    def put(self, *args, **kwargs):
        return self.runner.run(self.client.put(*args, **kwargs))

    def close(self):
        self.runner.run(self.client.aclose())


class FastApiInboundAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / 'runtime'
        self.service = WorkflowService(
            self.root,
            client_factory=lambda *_args, **_kwargs: self.fail('model client must not be called'),
        )
        app = create_app(service=self.service, public_origin=ORIGIN,
                         trusted_proxy_cidrs=('127.0.0.1/32',))
        self.runner = asyncio.Runner()
        self.lifespan = app.router.lifespan_context(app)
        self.runner.run(self.lifespan.__aenter__())
        self.app = app
        self.client = SyncASGIClient(self.runner, app)
        self.csrf = self.client.get(API + '/session').json()['csrf_token']

    def tearDown(self):
        self.client.close()
        self.runner.run(self.lifespan.__aexit__(None, None, None))
        self.runner.close()
        self.service.close()
        self.temporary.cleanup()

    def write_headers(self):
        return {'Origin': ORIGIN, 'X-CSRF-Token': self.csrf}

    def test_versioned_routes_export_as_yaml_and_no_key_is_in_any_read_dto(self):
        specification = json.loads(json.dumps(__import__('yaml').safe_load(rendered_openapi())))
        paths = specification['paths']
        health = self.client.get('/healthz')
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {'status': 'ok'})
        self.assertNotIn('/healthz', paths)
        self.assertIn(API + '/session', paths)
        self.assertIn(API + '/jobs', paths)
        self.assertIn(API + '/jobs/{identity}/download/{filename}', paths)
        self.assertIn(API + '/jobs/{identity}/report.{extension}', paths)
        csrf_parameters = paths[API + '/jobs']['post']['parameters']
        csrf_header = next(item for item in csrf_parameters if item['name'] == 'X-CSRF-Token')
        self.assertTrue(csrf_header['required'])
        self.assertIn('10 MiB per file', paths[API + '/uploads']['put']['description'])

        saved = self.client.post(API + '/configs', headers=self.write_headers(), json={
            'name': 'Local fake', 'provider': 'deepseek', 'api_key': 'FAKE-LOCAL-API-KEY',
        })
        self.assertEqual(saved.status_code, 201, saved.text)
        response = saved.json()
        self.assertNotIn('api_key', response['config'])
        self.assertNotIn('FAKE-LOCAL-API-KEY', json.dumps(response))
        listed = self.client.get(API + '/configs').json()
        self.assertTrue(listed['configured'])
        self.assertNotIn('FAKE-LOCAL-API-KEY', json.dumps(listed))
        self.assertNotIn('api_key', json.dumps(specification['components']['schemas']['ConfigMetadata']))

    def test_openapi_error_responses_match_the_runtime_error_envelope(self):
        specification = json.loads(json.dumps(__import__('yaml').safe_load(rendered_openapi())))
        responses = specification['paths'][API + '/jobs/{identity}']['get']['responses']
        for status in ('400', '403', '404', '409', '413', '422'):
            with self.subTest(status=status):
                schema = responses[status]['content']['application/json']['schema']
                reference = schema['$ref'].rsplit('/', 1)[-1]
                envelope = specification['components']['schemas'][reference]
                self.assertEqual(envelope['required'], ['error'])
                error_schema = envelope['properties']['error']
                error_reference = error_schema['$ref'].rsplit('/', 1)[-1]
                self.assertEqual(error_reference, 'ApiError')
                self.assertEqual(set(specification['components']['schemas'][error_reference]['required']),
                                 {'code', 'message'})

    def test_job_response_schema_preserves_workflow_node_progress_details(self):
        from autofill.adapters.http.schemas import JobView

        job = JobView.model_validate({
            'id': 'd' * 32, 'status': 'running', 'created_at': '2026-10-20T00:00:00+00:00',
            'updated_at': '2026-10-20T00:00:00+00:00',
            'workflow_nodes': [{'node_id': 'answer', 'status': 'running', 'phase': 'deciding',
                                'counts': {'completed': 2, 'total': 4, 'attempt': 2}}],
        })
        node = job.workflow_nodes[0]
        self.assertEqual(node.phase, 'deciding')
        self.assertEqual(node.counts, {'completed': 2, 'total': 4, 'attempt': 2})

    def test_mutations_require_same_origin_and_bootstrapped_csrf(self):
        body = {'name': 'Local fake', 'provider': 'deepseek'}
        bad_origin = self.client.post(API + '/configs', headers={
            'Origin': 'https://attacker.invalid', 'X-CSRF-Token': self.csrf,
        }, json=body)
        self.assertEqual(bad_origin.status_code, 403)

        bad_token = self.client.post(API + '/configs', headers={
            'Origin': ORIGIN, 'X-CSRF-Token': 'not-the-session-token',
        }, json=body)
        self.assertEqual(bad_token.status_code, 403)

        missing_token = self.client.post(API + '/configs', headers={'Origin': ORIGIN}, json=body)
        self.assertEqual(missing_token.status_code, 403)

        invalid_fields = self.client.post(API + '/configs', headers=self.write_headers(),
                                          json=body | {'unexpected': 'value'})
        self.assertEqual(invalid_fields.status_code, 422)
        self.assertNotIn('unexpected', invalid_fields.text)

        response = self.client.get(API + '/session')
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_upload_stream_is_bounded_and_task_submission_is_idempotent(self):
        profile = self.client.post(API + '/configs', headers=self.write_headers(), json={
            'name': 'Local fake', 'provider': 'deepseek', 'api_key': 'FAKE-LOCAL-API-KEY',
        }).json()['config']
        questionnaire = pdf(Path(self.temporary.name) / 'questionnaire.pdf', text='Synthetic form')
        questionnaire_bytes = questionnaire.read_bytes()
        uploaded_questionnaire = self.client.put(
            API + '/uploads', params={'name': 'questionnaire.pdf', 'role': 'questionnaire'},
            headers=self.write_headers() | {'Content-Type': 'application/octet-stream'},
            content=questionnaire_bytes,
        )
        self.assertEqual(uploaded_questionnaire.status_code, 201, uploaded_questionnaire.text)
        async def source_chunks():
            yield b'Company: '
            yield b'SYNTHETIC CO'

        uploaded_source = self.client.put(
            API + '/uploads', params={'name': 'source.txt', 'role': 'source'},
            headers=self.write_headers() | {'Content-Type': 'application/octet-stream'},
            content=source_chunks(),
        )
        self.assertEqual(uploaded_source.status_code, 201, uploaded_source.text)

        oversized = self.client.put(
            API + '/uploads', params={'name': 'large.pdf', 'role': 'questionnaire'},
            headers=self.write_headers() | {'Content-Type': 'application/octet-stream'},
            content=b'x' * (MAX_WEB_UPLOAD_BYTES + 1),
        )
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(oversized.json()['error']['code'], 'WEB_UPLOAD_SIZE')
        self.assertIn('10 MiB', oversized.json()['error']['message'])

        submission = {
            'request_id': 'api-v1-idempotency-0001',
            'config_id': profile['id'],
            'questionnaire': uploaded_questionnaire.json()['token'],
            'sources': [uploaded_source.json()['token']],
        }
        with patch.object(self.service, 'start_worker') as worker:
            first = self.client.post(API + '/jobs', headers=self.write_headers(), json=submission)
            second = self.client.post(API + '/jobs', headers=self.write_headers(), json=submission)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(first.json()['id'], second.json()['id'])
        worker.assert_called_once()
        job = self.client.get(API + '/jobs/' + first.json()['id'])
        self.assertEqual(job.status_code, 200, job.text)
        self.assertEqual(job.json()['status'], 'running')
        cancelled = self.client.post(API + '/jobs/' + first.json()['id'] + '/cancel',
                                     headers=self.write_headers())
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json(), {'accepted': True})
        self.assertEqual(questionnaire.read_bytes(), questionnaire_bytes)
        self.assertTrue((self.root / 'staged-v2').is_dir())

    def test_download_resolves_files_only_inside_the_service_root(self):
        identity = 'b' * 32
        directory = self.root / 'persistent' / identity
        directory.mkdir(parents=True)
        report = directory / 'report.json'
        report.write_text(json.dumps({
            'schema_version': 1, 'mode': 'real', 'question_total': 0, 'counts': {},
            'input_hashes': {}, 'output_sha256': None,
            'verification': {'content_ok': True, 'layout_check': 'not_run', 'needs_review': False, 'details': {}},
            'rows': [], 'note': 'Synthetic local report.',
        }), encoding='utf-8')
        document = directory / 'filled.pdf'
        document.write_bytes(b'%PDF-synthetic')
        outside = Path(self.temporary.name) / 'outside.txt'
        outside.write_text('synthetic outside-root file', encoding='utf-8')
        self.service.store.create_run(identity, 'api-v1-download-00001', {'questionnaire_name': 'synthetic.pdf'})
        self.service.store.finish(identity, {'status': 'done', 'files': {
            'report.json': str(report), 'filled.pdf': str(document), 'outside.txt': str(outside),
        }})

        downloaded = self.client.get(f'{API}/jobs/{identity}/download/filled.pdf')
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(downloaded.content, b'%PDF-synthetic')
        self.assertEqual(downloaded.headers['cache-control'], 'no-store')
        escaped = self.client.get(f'{API}/jobs/{identity}/download/outside.txt')
        self.assertEqual(escaped.status_code, 404)
        report_response = self.client.get(f'{API}/jobs/{identity}/report.json')
        self.assertEqual(report_response.status_code, 200, report_response.text)
        self.assertIn('result_summary', report_response.json())
        markdown = self.client.get(f'{API}/jobs/{identity}/report.md')
        self.assertEqual(markdown.status_code, 200, markdown.text)
        self.assertTrue(markdown.headers['content-type'].startswith('text/markdown'))
        html = self.client.get(f'{API}/jobs/{identity}/report.html')
        self.assertEqual(html.status_code, 200, html.text)
        self.assertTrue(html.headers['content-type'].startswith('text/html'))
        self.assertIn("default-src 'none'", html.text)

    def test_untrusted_peer_and_forwarded_headers_do_not_create_trust(self):
        other_client = SyncASGIClient(self.runner, self.app, client=('10.3.4.5', 3456))
        response = other_client.get(API + '/session', headers={'X-Forwarded-For': '127.0.0.1'})
        self.assertEqual(response.status_code, 403)
        other_client.close()
        bad_host = self.client.get(API + '/session', headers={'Host': 'attacker.invalid'})
        self.assertEqual(bad_host.status_code, 403)


if __name__ == '__main__':
    unittest.main()
