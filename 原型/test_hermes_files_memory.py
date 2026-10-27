"""Owned fictitious document scopes; no Hermes Agent, network or private files."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memory_context import validate_recall_package
from rabbit_memory.hermes_files_memory import HermesFilesMemory, MAX_DOCUMENT_BYTES
from rabbit_memory.hindsight_memory import MemoryServiceError
from rabbit_memory.memory_profiles import MemoryProfiles, make_memory_snapshot


class HermesFilesMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.profile = MemoryProfiles(self.base/'profiles.sqlite').create('虚构文档',
            program='hermes_files', connection_id='local/hermes-files')
        self.home = ContextVar('test_home', default=None)
        self.loads = []
        self.on_load = None
        test = self

        class Core:
            @staticmethod
            def _parse_entries(raw):
                return [entry.strip() for entry in raw.split('\n§\n') if entry.strip()]

            def _set_entries(self, target, entries):
                self.entries = entries

            def _char_count(self, target):
                return len('\n§\n'.join(self.entries))

            def _char_limit(self, target):
                return 2200 if target == 'memory' else 1375

            def load_from_disk(self):
                home = test.home.get()
                test.loads.append(home)
                self.texts = [(home/'memories'/name).read_text()
                              for name in ('MEMORY.md', 'USER.md')]
                if test.on_load:
                    test.on_load()

            def format_for_system_prompt(self, target):
                text = self.texts[0 if target == 'memory' else 1]
                return 'native-formatted:' + text if text else None

        @contextmanager
        def scope(home):
            token = self.home.set(home)
            try:
                yield
            finally:
                self.home.reset(token)

        self.root = self.base/'owned-scopes'
        self.driver = HermesFilesMemory(self.root, core_factory=Core, core_scope=scope)
        self.docs = self.root/('rabbit-'+self.profile['id'])/'memories'

    def save(self, name, text):
        old = self.driver.read_document(self.profile, name)
        return self.driver.write_document(self.profile, name, text,
                                         expected_sha256=old['sha256'])

    def test_uninitialized_metadata_does_not_create_files_and_retain_is_never_enabled(self):
        self.assertFalse(self.driver.health()['write_supported'])
        for method in (self.driver.bank_profile, self.driver.bank_state):
            with self.assertRaises(MemoryServiceError) as caught:
                method(self.profile)
            self.assertEqual(caught.exception.status, 404)
        self.assertFalse(self.root.exists())
        self.assertEqual(self.loads, [])
        with self.assertRaises(MemoryServiceError):
            self.driver.retain(self.profile, 'never write automatically')

    def test_manual_save_preserves_exact_text_old_version_and_rejects_stale_or_wrong_names(self):
        self.assertTrue(self.driver.ensure_bank(self.profile)['created'])
        self.assertFalse(self.driver.ensure_bank(self.profile)['created'])
        empty = self.driver.read_document(self.profile, 'MEMORY.md')
        text = '\ufeff虚构纸船\r\n\n§\n完整结尾  '
        saved = self.save('MEMORY.md', text)
        self.assertEqual(saved['text'], text)
        self.assertEqual(saved['sha256'], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(next(self.docs.glob('MEMORY.md.previous-*')).read_bytes(), b'')
        before = (self.docs/'MEMORY.md').read_bytes()
        with self.assertRaises(MemoryServiceError) as caught:
            self.driver.write_document(self.profile, 'MEMORY.md', 'stale', expected_sha256=empty['sha256'])
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(ValueError):
            self.driver.read_document(self.profile, '../USER.md')
        self.assertEqual((self.docs/'MEMORY.md').read_bytes(), before)

    def test_complete_native_formatted_snapshot_and_scope_reset_without_environment_changes(self):
        self.driver.ensure_bank(self.profile)
        self.save('MEMORY.md', '事实甲\n§\n事实乙')
        self.save('USER.md', '用户偏好：简短')
        environment = dict(os.environ)
        before = {name: (self.docs/name).read_bytes() for name in ('MEMORY.md','USER.md')}
        package = self.driver.recall(self.profile, '虚构问题')
        self.assertEqual(validate_recall_package(package, make_memory_snapshot(self.profile), query='虚构问题'), package)
        self.assertEqual([fact['text'] for fact in package['facts']],
                         ['native-formatted:事实甲\n§\n事实乙', 'native-formatted:用户偏好：简短'])
        self.assertEqual(package['omitted_results'], 0)
        self.assertEqual(self.driver.bank_state(self.profile)['documents'], 2)
        self.assertIsNone(self.home.get())
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual({name:(self.docs/name).read_bytes() for name in before}, before)

    def test_two_cooperating_writers_cannot_both_apply_the_same_version(self):
        self.driver.ensure_bank(self.profile)
        sha = self.driver.read_document(self.profile, 'USER.md')['sha256']
        def write(text):
            try:
                return self.driver.write_document(self.profile, 'USER.md', text,
                                                  expected_sha256=sha)['text']
            except MemoryServiceError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, ['甲', '乙']))
        self.assertEqual(sum(value == 409 for value in results), 1)
        self.assertIn(self.driver.read_document(self.profile, 'USER.md')['text'], ('甲','乙'))

    def test_symlinks_invalid_utf8_oversize_and_changed_during_core_load_are_rejected(self):
        self.driver.ensure_bank(self.profile)
        path = self.docs/'MEMORY.md'
        path.unlink()
        outside = self.base/'outside.txt'
        outside.write_text('not part of the scope')
        path.symlink_to(outside)
        with self.assertRaises(MemoryServiceError):
            self.driver.recall(self.profile, 'question')
        path.unlink()
        for raw in (b'\xff', b'a'*(MAX_DOCUMENT_BYTES+1)):
            path.write_bytes(raw)
            with self.assertRaises((MemoryServiceError, ValueError)):
                self.driver.read_document(self.profile, 'MEMORY.md')
        path.write_text('before')
        self.on_load = lambda: path.write_text('after')
        with self.assertRaises(MemoryServiceError):
            self.driver.recall(self.profile, 'question')
        self.assertIsNone(self.home.get())

    def test_formatted_capacity_is_not_silently_truncated_and_replace_failure_keeps_original(self):
        self.driver.ensure_bank(self.profile)
        with self.assertRaises(ValueError):
            self.save('MEMORY.md', 'a'*2201)
        with self.assertRaises(ValueError):
            self.save('USER.md', 'a'*1376)
        (self.docs/'MEMORY.md').write_text('a'*8192)
        with self.assertRaises(MemoryServiceError):
            self.driver.recall(self.profile, 'question')
        original = self.driver.read_document(self.profile, 'MEMORY.md')
        with patch('rabbit_memory.hermes_files_memory.os.replace', side_effect=OSError('fictional failure')):
            with self.assertRaises(MemoryServiceError):
                self.save('MEMORY.md', 'replacement')
        self.assertEqual(self.driver.read_document(self.profile, 'MEMORY.md'), original)
        self.assertTrue(any(p.read_bytes() == original['text'].encode()
                            for p in self.docs.glob('MEMORY.md.previous-*')))


if __name__ == '__main__':
    unittest.main()
