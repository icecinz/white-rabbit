"""Exercise exact Hermes scope and the filesystem effects of a manual read."""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import dashboard_hermes_usage as usage
from dashboard_usage import ReadFailure
from native_catalogue import NativeChatCatalogue
from rabbit_runtime.hermes.session_store import HermesNativeStore


@unittest.skipUnless(os.access(usage._SANDBOX, os.X_OK), 'Requires the macOS read-only execution policy.')
class HermesUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='rabbit-hermes-usage-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        catalogue_path = root / 'chat.sqlite'
        catalogue_path.touch()
        catalogue = NativeChatCatalogue(catalogue_path, agent='hermes')
        store = HermesNativeStore(root / 'hermes-native.sqlite')
        self.addCleanup(store.close)
        self.manager = SimpleNamespace(root=root, catalogue=catalogue, store=store,
            _maintenance_catalogue_identity=usage.catalogue_identity(catalogue_path))
        self.profile = root / 'profile'
        self.profile.mkdir()
        self.native = self.profile / 'state.db'

    def publish(self, session_id=None):
        def prepare(identity):
            if session_id is None:
                return
            cwd = self.manager.root / identity
            cwd.mkdir()
            self.manager.store.bind(identity, cwd)
            self.manager.store.anchor_home(identity, self.profile)
            self.manager.store.attach(identity, session_id)

        return self.manager.catalogue.create(str(uuid4()), '同名对话', prepare)['id']

    def write(self, db, identity):
        db.execute('''CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, reasoning_tokens INTEGER, api_call_count INTEGER)''')
        cwd = str(self.manager.root / identity)
        db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)',
            ('session-original', cwd, 2 ** 53 + 1, 7, 0, None, 4, 2))
        db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)',
            ('unrelated', cwd, 100, 100, 100, 100, 100, 100))
        db.commit()

    def test_latest_bound_chat_is_read_only_on_manual_refresh(self):
        identity = self.publish('session-original')
        self.publish()
        with closing(sqlite3.connect(self.native)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            self.write(db, identity)
        self.assertEqual([path.name for path in self.profile.iterdir()], ['state.db'])
        with patch.object(usage.subprocess, 'run', side_effect=AssertionError('GET ran a native read')):
            context = usage.context(self.manager)
        self.assertIn(f'同名对话（{identity[:8]}）', context.scope)
        metrics, message = context.read()
        self.assertEqual({metric['label']: metric['value'] for metric in metrics}, {
            '输入 Token': str(2 ** 53 + 1), '输出 Token': '7',
            '缓存读取 Token': '0', '推理 Token': '4', 'API 调用次数': '2'})
        self.assertTrue(message)
        self.assertEqual([path.name for path in self.profile.iterdir()], ['state.db'])

    def test_reads_wal_commit_without_touching_sidecars(self):
        identity = self.publish('session-original')
        with closing(sqlite3.connect(self.native)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            self.write(db, identity)
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('UPDATE sessions SET output_tokens=19 WHERE id=?', ('session-original',))
            db.commit()
            before = {path.name: (path.stat().st_mtime_ns, path.read_bytes()) for path in self.profile.iterdir()}
            self.assertGreater(len(before['state.db-wal'][1]), 0)
            metrics, _ = usage.context(self.manager).read()
            self.assertEqual(metrics[1]['value'], '19')
            self.assertEqual({path.name: (path.stat().st_mtime_ns, path.read_bytes())
                              for path in self.profile.iterdir()}, before)

    def test_promotes_a_recorded_detail_if_both_main_counters_are_missing(self):
        identity = self.publish('session-original')
        with closing(sqlite3.connect(self.native)) as db:
            self.write(db, identity)
            db.execute('UPDATE sessions SET input_tokens=NULL, output_tokens=NULL WHERE id=?', ('session-original',))
            db.commit()
        metrics, _ = usage.context(self.manager).read()
        self.assertEqual([row['label'] for row in metrics if not row.get('detail')], ['缓存读取 Token'])
        self.assertEqual([row['group'] for row in metrics[1:]], ['会话明细', '会话明细'])

    def test_new_identity_hides_old_read_and_rejects_held_reader(self):
        identity = self.publish('session-original')
        with closing(sqlite3.connect(self.native)) as db:
            self.write(db, identity)
        original = usage.context(self.manager)
        self.native.rename(self.profile / 'first.db')
        with closing(sqlite3.connect(self.native)) as db:
            self.write(db, identity)
        self.assertNotEqual(usage.context(self.manager).fingerprint, original.fingerprint)
        with self.assertRaises(ReadFailure):
            original.read()
        current = usage.context(self.manager)
        self.manager.store.attach(identity, 'new-key', previous='session-original')
        self.assertNotEqual(usage.context(self.manager).fingerprint, current.fingerprint)
        with self.assertRaises(ReadFailure):
            current.read()

    def test_rejects_a_snapshot_if_native_state_changes_during_read(self):
        identity = self.publish('session-original')
        with closing(sqlite3.connect(self.native)) as db:
            self.write(db, identity)
        context = usage.context(self.manager)
        run = usage.subprocess.run

        def change_after_query(*args, **kwargs):
            result = run(*args, **kwargs)
            with closing(sqlite3.connect(self.native)) as db:
                db.execute('UPDATE sessions SET output_tokens=99 WHERE id=?', ('session-original',))
                db.commit()
            return result

        with patch.object(usage.subprocess, 'run', side_effect=change_after_query):
            with self.assertRaisesRegex(ReadFailure, '发生变化'):
                context.read()


if __name__ == '__main__':
    unittest.main()
