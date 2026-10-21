"""Contracts for application-owned outbound ports and adapters."""

import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from autofill.adapters.models.openai_chat import OpenAIChatClient
from autofill.adapters.persistence.sqlite_store import SQLiteWorkflowStore
from autofill.application.ports import DecisionClient, ModelClient, ProcessorPort, WorkflowStateStore
from autofill.config import preset_config
from autofill.adapters.models.jev_client import JevClient
from autofill.adapters.models.openai_chat import LLMFailure


SERVER = Path(__file__).resolve().parents[2] / 'server' / 'src' / 'autofill'


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.value


class WorkflowPortTests(unittest.TestCase):
    def test_production_adapters_satisfy_narrow_application_ports(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteWorkflowStore(Path(temporary) / 'state', Path(temporary) / 'key')
            try:
                self.assertIsInstance(store, WorkflowStateStore)
            finally:
                store.close()

        client = OpenAIChatClient(preset_config('deepseek'), api_key='FAKE-LOCAL-ONLY')
        self.assertIsInstance(client, ModelClient)
        decision = JevClient(preset_config('typesafe'), api_key='FAKE-LOCAL-ONLY')
        self.assertIsInstance(decision, DecisionClient)

        # ProcessorPort is structural and can be supplied by an isolated fake.
        class FakeProcessor:
            def run(self, path, operation, destination, format=None):
                raise NotImplementedError

            def convert(self, path, format, destination):
                raise NotImplementedError

            def ocr(self, path, destination):
                raise NotImplementedError

        self.assertIsInstance(FakeProcessor(), ProcessorPort)

    def test_configuration_metadata_never_contains_the_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteWorkflowStore(Path(temporary) / 'state', Path(temporary) / 'key')
            try:
                profile = store.save_profile({
                    'name': 'local fake', 'provider': 'deepseek', 'model': 'fake-model',
                    'api_key': 'FAKE-LOCAL-ONLY-KEY',
                })
                self.assertTrue(profile['has_key'])
                self.assertNotIn('api_key', profile)
                self.assertNotIn('encrypted_key', profile)
                self.assertNotIn('FAKE-LOCAL-ONLY-KEY', json.dumps(store.profiles()))
                config, key, public_profile = store.resolve_profile_for_use(profile['id'])
                self.assertEqual(config.model, 'fake-model')
                self.assertEqual(key, 'FAKE-LOCAL-ONLY-KEY')
                self.assertNotIn('api_key', public_profile)
                self.assertNotIn('FAKE-LOCAL-ONLY-KEY', json.dumps(public_profile))
            finally:
                store.close()

    def test_failed_profile_write_leaves_existing_profiles_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteWorkflowStore(Path(temporary) / 'state', Path(temporary) / 'key')
            try:
                before = store.profiles()
                with self.assertRaisesRegex(ValueError, 'CONFIG_NAME'):
                    store.save_profile({'name': '   ', 'provider': 'deepseek', 'api_key': 'FAKE'})
                self.assertEqual(store.profiles(), before)
                store.db.execute("""
                    CREATE TRIGGER reject_local_profile BEFORE INSERT ON profiles
                    WHEN json_extract(NEW.metadata, '$.name') = 'rollback probe'
                    BEGIN SELECT RAISE(ABORT, 'synthetic transaction failure'); END
                """)
                with self.assertRaises(sqlite3.IntegrityError):
                    store.save_profile({'name': 'rollback probe', 'provider': 'deepseek', 'api_key': 'FAKE'})
                self.assertEqual(store.profiles(), before)
            finally:
                store.close()

    def test_model_adapter_maps_timeout_and_invalid_response_without_network(self):
        config = preset_config('deepseek', base_url='http://127.0.0.1:1/v1', model='fake-model')
        timeout_client = OpenAIChatClient(config, api_key='FAKE-LOCAL-ONLY', transport=lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError()))
        with self.assertRaisesRegex(ValueError, 'LLM_TIMEOUT'):
            timeout_client.generate_json('prompt', {'type': 'object'})

        response = Response(json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': 'not json'}}]}).encode())
        invalid_client = OpenAIChatClient(config, api_key='FAKE-LOCAL-ONLY', transport=lambda *_a, **_k: response)
        with self.assertRaises(LLMFailure) as raised:
            invalid_client.generate_json('prompt', {'type': 'object'})
        self.assertEqual(str(raised.exception), 'LLM_BAD_JSON')
        self.assertNotIn('FAKE-LOCAL-ONLY', json.dumps(raised.exception.diagnostics))

    def test_application_modules_do_not_import_concrete_io_or_provider_adapters(self):
        forbidden = {'sqlite3', 'fastapi', 'httpx', 'requests', 'urllib'}
        bad = []
        for path in sorted((SERVER / 'application').rglob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or '']
                else:
                    continue
                for module in modules:
                    if (module in forbidden or module.startswith('urllib.')
                            or module.startswith('.adapters') or module.startswith('autofill.adapters')
                            or module.endswith('.sqlite_store') or module.endswith('.providers')
                            or module.endswith('.jev')):
                        bad.append(f'{path.relative_to(SERVER)} imports {module}')
        self.assertEqual(bad, [])


if __name__ == '__main__':
    unittest.main()
