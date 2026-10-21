"""Local workflow application service, independent of HTTP and browser state."""

import base64
import json
import re
import secrets
import shutil
import threading
from collections.abc import Callable
from pathlib import Path

from ..config import preset_config, validate_api_key
from ..core import run_pair
from ..errors import safe_error
from ..adapters.documents.extract import sha
from ..adapters.documents.report import save_report, prepare_report
from .diagnostics import diagnostic_metadata
from .ports import (
    DecisionClient,
    DocumentPreflight,
    ModelClient,
    ProcessorPort,
    QuestionnaireResolver,
    WorkflowStateStore,
)


MAX_FILE = 10 * 1024 * 1024
UPLOAD_NAMES = ('questionnaire', 'source')
UPLOAD_COPIES = ('questionnaire.pdf', 'questionnaire.docx', 'questionnaire.doc', 'source.pdf', 'source.docx')
INLINE_CONFIG_FIELDS = ('api_key', 'provider', 'model', 'base_url', 'output_mode', 'protocol')


class WorkflowApplicationService:
    def __init__(self, root, *, store: WorkflowStateStore,
                 client_factory: Callable[..., ModelClient],
                 processor_factory: Callable[..., ProcessorPort],
                 questionnaire_resolver: QuestionnaireResolver,
                 preflight: DocumentPreflight,
                 decision_client_factory: Callable[..., DecisionClient] | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store
        self._submit_lock = threading.Lock()
        self._workers = []
        self._summaries = {}
        self._closing = False
        self._resolver = questionnaire_resolver
        self.preflight = preflight
        self._client_factory = client_factory
        self._processor_factory = processor_factory
        self._decision_client_factory = decision_client_factory
        try:
            self.store.import_legacy(self.root)
            for job in self.store.runs():
                if job['status'] == 'interrupted':
                    directory = self.root / 'persistent' / job['id']
                    for name in UPLOAD_COPIES:
                        (directory / name).unlink(missing_ok=True)
            from .tasks import GeneralTasks
            self.tasks = GeneralTasks(self)
        except Exception:
            self.store.close()
            raise

    def configurations(self):
        """Read public metadata only; readiness does not test model credentials."""
        profiles = self.store.profiles()
        return {'configs': profiles, 'configured': any(profile['has_key'] and profile['protocol'] == 'openai_chat' for profile in profiles)}

    def _configuration(self, body, *, protocol='openai_chat'):
        if 'config_id' in body:
            if any(name in body for name in INLINE_CONFIG_FIELDS):
                raise ValueError('UPLOAD_FIELDS')
            config, key, profile = self.store.resolve_profile_for_use(body['config_id'])
            snapshot = {name: profile[name] for name in ('id', 'name', 'provider', 'model', 'base_url', 'output_mode', 'protocol')}
            snapshot['max_tokens'] = config.max_tokens
            if config.protocol != protocol:
                raise ValueError('CONFIG_MODEL_ROLE')
            return config, key, snapshot
        key = validate_api_key(body.pop('api_key', None))
        config = preset_config(body.get('provider'), model=body.get('model'),
                               base_url=body.get('base_url') or None, output_mode=body.get('output_mode'), protocol=body.get('protocol'))
        if config.protocol != protocol:
            raise ValueError('CONFIG_MODEL_ROLE')
        if config.max_tokens > 65536 or len(config.model) > 200:
            raise ValueError('CONFIG_LIMIT')
        return config, key, config.metadata() | {'name': '单次配置'}

    @staticmethod
    def _uploads(body):
        uploads = []
        for name in UPLOAD_NAMES:
            item = body[name]
            if (not isinstance(item, dict) or set(item) != {'name', 'data'}
                    or not isinstance(item['name'], str) or not isinstance(item['data'], str)):
                raise ValueError('INVALID_UPLOAD')
            suffix = Path(item['name']).suffix.lower()
            allowed = ('.pdf', '.docx', '.doc') if name == 'questionnaire' else ('.pdf', '.docx')
            if suffix not in allowed:
                raise ValueError('UNSUPPORTED_FORMAT')
            data = base64.b64decode(item['data'], validate=True)
            if not 0 < len(data) <= MAX_FILE:
                raise ValueError('UPLOAD_SIZE')
            uploads.append((name, suffix, data))
        return uploads

    def submit(self, body):
        """Accept once, then retain the worker even if the HTTP response disconnects."""
        if isinstance(body, dict) and 'sources' in body:
            return self.tasks.submit(body)
        allowed = set(UPLOAD_NAMES) | {'config_id', 'request_id'} | set(INLINE_CONFIG_FIELDS)
        if not isinstance(body, dict) or not set(UPLOAD_NAMES) <= set(body) or set(body) - allowed:
            raise ValueError('UPLOAD_FIELDS')
        request_id = body.get('request_id') or secrets.token_hex(16)
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id):
            raise ValueError('REQUEST_ID')
        directory = None
        job_id = None
        key = None
        with self._submit_lock:
            try:
                if self._closing:
                    raise ValueError('SERVICE_CLOSING')
                previous = self.store.by_request(request_id)
                if previous:
                    return previous['id']
                if any(job['status'] == 'running' for job in self.store.runs()):
                    raise ValueError('BUSY')
                config, key, snapshot = self._configuration(body)
                uploads = self._uploads(body)
                job_id = secrets.token_hex(16)
                directory = self.root / 'persistent' / job_id
                directory.mkdir(parents=True, mode=0o700)
                paths = []
                for name, suffix, data in uploads:
                    path = directory / (name + suffix)
                    path.write_bytes(data)
                    paths.append(path)
                self.store.create_run(job_id, request_id, {
                    'source_name': Path(body['source']['name']).name[:240],
                    'questionnaire_name': Path(body['questionnaire']['name']).name[:240],
                    'config': snapshot,
                })
                self.start_worker(self._work, job_id, *paths, config, key)
                return job_id
            except Exception as error:
                if directory:
                    shutil.rmtree(directory, ignore_errors=True)
                    if job_id and self.store.run(job_id):
                        self.store.finish(job_id, {'status': 'error', 'error': safe_error(error)}, 'failed')
                raise
            finally:
                key = None

    def start_worker(self, target, *args, **kwargs):
        """Register a worker while the caller holds the shared submission lock."""
        def work():
            try:
                target(*args, **kwargs)
            finally:
                with self._submit_lock:
                    if self._closing and not any(thread.is_alive() and thread is not threading.current_thread()
                                                 for thread in self._workers):
                        self.store.close()
        worker = threading.Thread(target=work, daemon=True)
        self._workers = [thread for thread in self._workers if thread.is_alive()]
        self._workers.append(worker)
        try:
            worker.start()
        except Exception:
            self._workers.remove(worker)
            raise

    def _work(self, job_id, questionnaire, source, config, key):
        client = None
        hashes = {}

        def progress(stage, **details):
            self.store.progress(job_id, stage, **details)

        try:
            hashes = {'questionnaire': sha(questionnaire), 'source': sha(source)}
            progress('checking')
            normalized, template, conversion = self._resolver(questionnaire, questionnaire.parent)
            client = self._client_factory(config, key)
            from ..workflow.compatibility import LegacyProgressSink
            result = run_pair(normalized, source, template, config, questionnaire.parent / 'result',
                              client=client, progress=progress,
                              workflow_progress=LegacyProgressSink(self.store, job_id))
            files = [result.report_json, result.report_markdown]
            if result.output_path:
                files.insert(0, result.output_path)
            report = json.loads(result.report_json.read_text(encoding='utf-8'))
            report['input_original_questionnaire_sha256'] = hashes['questionnaire']
            report['questionnaire_conversion'] = conversion
            save_report(report, result.report_json.parent)
            html_report = result.report_json.with_suffix('.html')
            if html_report.is_file():
                files.append(html_report)
            self.complete_task(job_id, report, result.outcome,
                                     {path.name: str(path) for path in files}, conversion=conversion)
        except Exception as error:
            self.fail_task(job_id, questionnaire.parent, error, config, hashes)
        finally:
            if hasattr(client, 'api_key'):
                client.api_key = None
            key = None
            for path in (questionnaire, source):
                path.unlink(missing_ok=True)

    def public_job(self, identity):
        return self._with_summary(self.store.run(identity))

    def jobs(self):
        return [self._with_summary(job) for job in self.store.runs()]

    def _with_summary(self, job):
        # Derive historical views without modifying customer outputs or metadata.
        if not job or job.get('summary') or job['status'] == 'running' or 'report.json' not in job['files']:
            return job
        identity = job['id']
        if identity not in self._summaries:
            try:
                path = Path(self.store.run(identity, True)['files']['report.json']).resolve()
                if not path.is_relative_to(self.root):
                    return job
                self._summaries[identity] = prepare_report(json.loads(path.read_text()))['result_summary']
            except (OSError, ValueError, KeyError, TypeError):
                self._summaries[identity] = None
        if self._summaries[identity]:
            job['summary'] = self._summaries[identity]
        return job

    def complete_task(self, identity, report, outcome, files, **extra):
        """Both entry points archive the report's authoritative result summary."""
        prepared = prepare_report(report); failed = outcome == 'failed'
        self.store.finish(identity, dict(extra, status='error' if failed else 'done', review=None, cancelling=False,
            outcome=outcome, counts=prepared['counts'], summary=prepared['result_summary'],
            processing_warnings=prepared['processing_warnings'], layout_check=prepared['verification']['layout_check'], files=files,
            error={'code': 'VERIFICATION_FAILED', 'message': '填写文件未通过校验，请查看报告。'} if failed else None),
            'failed' if failed else 'success')

    def fail_task(self, job_id, directory, error, config, hashes):
        """Both entry points retain the same safe diagnostic before cleanup."""
        job = self.store.run(job_id)
        cancelled = str(error).split(':', 1)[0] in ('TASK_CANCELLED', 'CANCELLED')
        details = next((node for node in job['nodes'] if node['stage'] == job.get('stage')), {})
        role = getattr(error, 'model_role', getattr(error, 'diagnostics', {}).get('model_role', 'llm'))
        diagnostic = {
            'error': safe_error(ValueError('TASK_CANCELLED') if cancelled else error), 'stage': job.get('stage'),
            'batch': details.get('batch'), 'batches': details.get('batches'),
            'progress': {name: details[name] for name in ('phase', 'completed', 'total', 'attempt') if name in details},
            'progress_log': job.get('progress_log', []),
            'created_at': job['created_at'], 'model': config.model, 'provider': config.provider,
            'input_sha256': hashes, 'automatic_retry': False,
            'model_diagnostics': diagnostic_metadata(getattr(error, 'diagnostics', {})),
            'answer_mode': job.get('answer_mode', 'grounded'),
            'model_role': role if role in ('llm', 'jev') else 'llm',
        }
        if job.get('decision_config'):
            diagnostic['decision_model'] = {name: job['decision_config'][name]
                for name in ('provider', 'model', 'protocol') if name in job['decision_config']}
        artifact = directory / 'failure.json'
        artifact.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding='utf-8')
        self.store.finish(job_id, diagnostic | {'status': 'cancelled' if cancelled else 'error',
                          'review': None, 'cancelling': False, 'files': {artifact.name: str(artifact)}},
                          'cancelled' if cancelled else 'failed')

    def close(self):
        self.tasks.close()
        with self._submit_lock:
            self._closing = True
            workers = list(self._workers)
        for worker in workers:
            worker.join(timeout=5)
        with self._submit_lock:
            if not any(worker.is_alive() for worker in workers):
                self.store.close()
