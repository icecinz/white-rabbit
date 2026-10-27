"""Optional Mem0 bridge: synthetic config, fixed SDK-shaped fixture, zero I/O."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from memory_context import validate_recall_package
from rabbit_memory.hindsight_memory import MemoryServiceError
from rabbit_memory.memory_profiles import make_memory_snapshot
from rabbit_memory.mem0_memory import Mem0Memory, SDK_VERSION
import mem0_memory_worker as mem0_worker


class Mem0MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rabbit-mem0-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / 'mem0.json'
        self.value = {'schema_version': 1, 'llm': {'model': 'invented-llm', 'base_url': 'https://llm.invalid/v1', 'api_key': 'FICTIONAL_KEY'},
            'embedder': {'model': 'invented-embedding', 'base_url': 'https://embed.invalid/v1', 'api_key': 'FICTIONAL_EMBED', 'dimensions': 3}}
        self.write_config()
        self.worker = Path(mem0_worker.__file__)
        self.driver = Mem0Memory(os.sys.executable, self.config, self.root / 'owned', worker_path=self.worker)
        self.profile = {'id': str(uuid.uuid4()), 'revision': 1, 'name': '虚构Mem0', 'description': '', 'habits': '',
            'program': 'mem0', 'connection_id': 'local/mem0', 'mode': 'read_only'}
        self.calls, self.rows = [], []
        self.enterContext(patch('subprocess.Popen', side_effect=AssertionError('No process in fixtures')))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('No network')))
        self.enterContext(patch.object(self.driver, '_invoke', side_effect=self.invoke))

    def write_config(self):
        self.config.write_text(json.dumps(self.value)); self.config.chmod(0o600)

    def invoke(self, payload):
        self.calls.append(deepcopy(payload))
        if payload['action'] == 'probe':
            return {'schema_version': 1, 'available': True, 'sdk_version': SDK_VERSION}
        return {'schema_version': 1, 'sdk_version': SDK_VERSION, 'profile_id': payload['profile_id'],
                'query_sha256': hashlib.sha256(payload['query'].encode()).hexdigest(), 'results': deepcopy(self.rows)}

    def row(self, text='  完整虚构事实🐇\r\n保留尾行  ', **changes):
        return dict(id=str(uuid.uuid4()), memory=text, user_id='rabbit-' + self.profile['id']) | changes

    def test_unconfigured_is_available_false_without_process_or_environment_key_fallback(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'DO_NOT_USE', 'HINDSIGHT_LLM_API_KEY': 'DO_NOT_USE'}):
            self.assertFalse(Mem0Memory().health()['available'])
            self.value['embedder']['api_key'] = ''; self.write_config()
            self.assertFalse(self.driver.health()['available'])
        self.assertEqual(self.calls, [])

    def test_metadata_never_searches_and_only_explicit_ensure_creates_owned_scope(self):
        self.assertTrue(self.driver.health()['available']); self.assertFalse(self.driver.health()['write_supported'])
        state = self.driver.bank_state(self.profile, fresh=True)
        self.assertIsNone(state['facts']); self.assertIsNone(state['documents'])
        self.assertFalse((self.root / 'owned').exists())
        self.assertEqual(self.driver.ensure_bank(self.profile), {'created': True})
        self.assertEqual(self.driver.ensure_bank(self.profile), {'created': False})
        self.assertTrue((self.root / 'owned' / self.profile['id']).is_dir())
        self.assertTrue(all(row['action'] == 'probe' for row in self.calls))

    def test_complete_package_uses_exact_query_profile_and_whole_sdk_fact(self):
        self.rows = [self.row()]
        with patch.dict('sys.modules', {'memory_context': None}):
            package = self.driver.recall(self.profile, '  原问题\r\n')
        self.assertEqual(validate_recall_package(package, make_memory_snapshot(self.profile), query='  原问题\r\n'), package)
        self.assertEqual(package['facts'][0]['text'], self.rows[0]['memory'])
        self.assertEqual(package['facts'][0]['type'], 'observation')
        self.assertNotIn('FICTIONAL', json.dumps(package)); self.assertNotIn(str(self.root), json.dumps(package))
        self.assertEqual(self.calls[-1]['scope'], str(self.root / 'owned' / self.profile['id']))

    def test_wrong_user_duplicate_id_wrong_shape_and_invalid_text_never_leak(self):
        good = self.row()
        for rows in ([dict(good, user_id='rabbit-' + str(uuid.uuid4()))], [good, good],
                     [dict(good, api_key='DO_NOT_PROJECT')], [dict(good, memory='x' * 8193)],
                     [dict(good, memory='bad\x00text')]):
            with self.subTest(rows=rows):
                self.rows = rows
                with self.assertRaises(MemoryServiceError): self.driver.recall(self.profile, 'query')

    def test_bounded_whole_facts_have_explicit_omission_counts(self):
        self.rows = [self.row('x' * 500) for _ in range(17)]
        package = self.driver.recall(self.profile, 'query')
        self.assertEqual(len(package['facts']), 16); self.assertEqual(package['omitted_results'], 1)
        self.assertEqual(package['characters'], 8000)
        self.assertTrue(all(len(row['text']) == 500 for row in package['facts']))

    def test_profile_uuid_and_program_isolation_and_all_writes_disabled(self):
        other = dict(self.profile, id=str(uuid.uuid4()))
        self.driver.ensure_bank(self.profile); self.driver.ensure_bank(other)
        self.assertNotEqual((self.root / 'owned' / self.profile['id']), (self.root / 'owned' / other['id']))
        for changes in ({'program': 'hindsight', 'connection_id': 'local/hindsight'}, {'mode': 'read_write'}, {'id': 'not-uuid'}):
            with self.assertRaises((MemoryServiceError, ValueError)):
                self.driver.recall(dict(self.profile, **changes), 'query')
        for name in ('retain', 'retain_payload', 'operation', 'document_receipt'):
            with self.assertRaises(MemoryServiceError): getattr(self.driver, name)()

    def test_private_config_closed_schema_permissions_duplicates_and_path_guards(self):
        self.config.chmod(0o644); self.assertFalse(self.driver.health()['available']); self.config.chmod(0o600)
        self.config.write_text('{"schema_version":1,"schema_version":1,"llm":{},"embedder":{}}')
        self.assertFalse(self.driver.health()['available']); self.write_config()
        self.value['extra'] = 'not-supported'; self.write_config(); self.assertFalse(self.driver.health()['available'])
        del self.value['extra']; self.write_config()
        self.config.unlink(); self.config.symlink_to(self.root / 'missing')
        self.assertFalse(self.driver.health()['available'])

    def test_changed_configuration_and_symlink_sdk_data_fail_closed(self):
        original = self.invoke
        def changed(payload):
            result = original(payload)
            if payload['action'] == 'recall':
                self.value['embedder']['model'] = 'different-model'; self.write_config()
            return result
        with patch.object(self.driver, '_invoke', side_effect=changed):
            with self.assertRaises(MemoryServiceError) as caught: self.driver.recall(self.profile, 'query')
        self.assertEqual(caught.exception.code, 'memory_connection_changed')
        scope = self.root / 'owned' / self.profile['id']
        (scope / 'qdrant').symlink_to(self.root)
        before = len(self.calls)
        with self.assertRaises(MemoryServiceError): self.driver.recall(self.profile, 'query')
        self.assertTrue(all(call['action'] == 'probe' for call in self.calls[before:]))

    def test_worker_uses_pinned_official_signature_and_scoped_explicit_config(self):
        self.driver.ensure_bank(self.profile)
        scope = self.root / 'owned' / self.profile['id']; calls = []
        def factory(config):
            calls.append(config)
            return SimpleNamespace(search=lambda query, **kwargs: self.search_worker(calls, query, kwargs))
        payload = {'schema_version': 1, 'action': 'recall', 'profile_id': self.profile['id'],
                   'query': '虚构查询', 'scope': str(scope), 'config': self.value}
        self.rows = [self.row()]
        with patch.object(mem0_worker, 'version', return_value=SDK_VERSION), \
             patch.dict('sys.modules', {'mem0': SimpleNamespace(Memory=SimpleNamespace(from_config=factory))}), \
             patch.dict(os.environ, {}, clear=False):
            value = mem0_worker.run(payload)
        self.assertEqual(value['results'], self.rows)
        self.assertEqual(calls[0]['vector_store']['config']['path'], str(scope / 'qdrant'))
        self.assertEqual(calls[0]['history_db_path'], str(scope / 'history.sqlite'))
        self.assertEqual(calls[0]['llm']['config']['api_key'], self.value['llm']['api_key'])
        self.assertEqual(calls[1], ('虚构查询', {'filters': {'user_id': 'rabbit-' + self.profile['id']}, 'top_k': 16, 'rerank': False}))

    def search_worker(self, calls, query, kwargs):
        calls.append((query, kwargs)); return {'results': deepcopy(self.rows)}

    def test_probe_only_reads_package_metadata_without_sdk_import(self):
        with patch.object(mem0_worker, 'version', return_value=SDK_VERSION), \
             patch.dict('sys.modules', {'mem0': None}):
            self.assertTrue(mem0_worker.run({'schema_version': 1, 'action': 'probe'})['available'])
        with patch.object(mem0_worker, 'version', return_value='different'):
            with self.assertRaises(ValueError): mem0_worker.run({'schema_version': 1, 'action': 'probe'})


if __name__ == '__main__':
    unittest.main()
