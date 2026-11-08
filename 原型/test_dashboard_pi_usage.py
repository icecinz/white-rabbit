"""Exact-source Pi dashboard readings with invented records only."""

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from chat_store import ChatStore
import dashboard_pi_usage as dashboard
from dashboard_usage import ReadFailure
from history_view import HistoryView
from native_catalogue import NativeChatCatalogue
from pi_history_adoption import PiHistoryAdoption
from rabbit_runtime.pi.session_store import PiNativeStore


def _entry(identity, parent, **usage):
    return {'type': 'message', 'id': identity, 'parentId': parent,
            'message': {'role': 'assistant', 'usage': usage, 'content': '未公开的虚构正文'}}


class DashboardPiUsageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='rabbit-pi-usage-', dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.pi_root = self.root/'pi'
        self.sessions = self.pi_root/'sessions'/'encoded-work'
        self.cwd = self.root/'work'
        self.sessions.mkdir(parents=True)
        self.cwd.mkdir()
        self.index = self.root/'history.sqlite'
        with sqlite3.connect(self.index) as db:
            db.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO meta VALUES('schema_version','1');
                CREATE TABLE sessions(agent TEXT,session_id TEXT,title TEXT,cwd TEXT,parent_session_id TEXT,
                    forked_from_id TEXT,kind TEXT,archived INTEGER,source_path TEXT,updated_at TEXT);
                CREATE TABLE messages(content TEXT);''')
        self.coverage = {}
        shared = ChatStore(self.root/'chat.sqlite')
        self.catalogue = NativeChatCatalogue(shared.path, agent='pi')
        self.store = PiNativeStore(self.root/'pi-native.sqlite')
        self.addCleanup(self.store.close)
        self.history = PiHistoryAdoption(HistoryView(self.index), self.pi_root, self.catalogue, self.store,
            workspace_id='aa'*32, excluded_sources=lambda: [])
        self.manager = SimpleNamespace(catalogue=self.catalogue, store=self.store)
        self.session_id = 'native-original-01234567'
        self.path = self.sessions/'original.jsonl'
        self.write_session(self.path, self.session_id, _entry('a', None, input=1, output=2, cacheRead=3, cacheWrite=4))
        self.index_session(self.session_id, self.path, '虚构样本', '2099-11-08')

    def write_session(self, path, session_id, *entries, **header_fields):
        header = dict(type='session', version=3, id=session_id, cwd=str(self.cwd), **header_fields)
        path.write_bytes(b''.join(json.dumps(row, ensure_ascii=False).encode()+b'\n' for row in (header, *entries)))

    def index_session(self, session_id, path, title, updated):
        self.coverage[session_id] = {'source_session_id': session_id, 'source_version': 3, 'branch_kind': 'main',
            'is_exported_branch': False, 'ambiguous_copies': 0, 'source_file_id': hashlib.sha256(str(path).encode()).hexdigest()}
        with sqlite3.connect(self.index) as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,NULL,NULL,?,0,?,?)',
                ('pi', session_id, title, str(self.cwd), 'root', str(path), updated))
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('pi_session_coverage', json.dumps(self.coverage)))

    def test_indexed_context_only_reads_metadata_and_manual_read_projects_saved_branch(self):
        self.index_session('older-example-00000000', self.sessions/'missing-older.jsonl', '较早会话', '2099-10-01')
        big = 9007199254740993
        self.write_session(self.path, self.session_id,
            _entry('a', None, input=big, output=3, cacheRead=5, cacheWrite=7, cost={'total': 42}),
            _entry('unused-branch', 'a', output=30),
            {'type': 'compaction', 'id': 'compacted', 'parentId': 'a', 'tokensBefore': 999},
            _entry('last', 'compacted', input=2, output=4, cacheRead=6, cacheWrite=8))
        with patch.object(dashboard, 'validate_session_file', side_effect=AssertionError('GET opened body')):
            snapshot = dashboard.context(SimpleNamespace(adoption=self.history), self.manager)
        self.assertIn('虚构样本 · 会话 …01234567 · 已保存分支累计', snapshot.scope)
        before = self.path.read_bytes()
        metrics, message = snapshot.read()
        self.assertEqual(message, '')
        self.assertEqual([row['value'] for row in metrics], [str(big+35), str(big+2), '7', '11', '15'])
        self.assertTrue(all(row['unit'] == 'Token' for row in metrics))
        self.assertNotIn('未公开的虚构正文', repr(metrics))
        self.assertEqual(self.path.read_bytes(), before)

    def test_appending_does_not_rename_source_but_replacing_it_invalidates_cache(self):
        initial = dashboard.context(self.history, self.manager)
        with self.path.open('ab') as stream:
            stream.write(json.dumps(_entry('b', 'a', input=5, output=0, cacheRead=0, cacheWrite=0)).encode()+b'\n')
        appended = dashboard.context(self.history, self.manager)
        self.assertEqual(appended.fingerprint, initial.fingerprint)
        self.assertEqual(appended.read()[0][0]['value'], '15')
        replacement = self.path.with_suffix('.replacement')
        self.write_session(replacement, self.session_id, _entry('a', None, input=0, output=0, cacheRead=0, cacheWrite=0))
        replacement.replace(self.path)
        self.assertNotEqual(dashboard.context(self.history, self.manager).fingerprint, initial.fingerprint)
        with self.assertRaises(ReadFailure):
            initial.read()

    def test_current_bound_session_is_selected_without_preparing_an_owner(self):
        bound_path = self.root/'bound.jsonl'
        bound_id = 'bound-example-76543210'
        self.write_session(bound_path, bound_id, _entry('root', None, input=6, output=1, cacheRead=0, cacheWrite=0))
        chat_id = '1adc64c5-b338-4cee-a323-6c7b15ae62b8'
        self.catalogue.create('69221449-ce17-4df2-be15-05778e74f136', '已有小白兔会话',
            lambda identity: self.store.adopt(identity, self.cwd, self.root, bound_path, bound_id), fixed_id=chat_id)
        with patch.object(self.history, 'latest_usage_source', side_effect=AssertionError('Unexpected index fallback')):
            snapshot = dashboard.context(self.history, self.manager)
        self.assertIn('已有小白兔会话 · 会话 …76543210', snapshot.scope)
        self.assertEqual(snapshot.read()[0][0]['value'], '7')

    def test_incomplete_values_exported_headers_and_bad_coverage_fail_closed(self):
        for usage in ({}, {'input': 1, 'output': False, 'cacheRead': 0, 'cacheWrite': 0},
                      {'input': 2**63-1, 'output': 1, 'cacheRead': 0, 'cacheWrite': 0}):
            with self.subTest(usage=usage):
                self.write_session(self.path, self.session_id, _entry('a', None, **usage))
                with self.assertRaisesRegex(ReadFailure, '没有完整可核实'):
                    dashboard.context(self.history, self.manager).read()
        self.write_session(self.path, self.session_id, parentSession='/fictional/exported.jsonl')
        with self.assertRaises(ReadFailure):
            dashboard.context(self.history, self.manager).read()
        with sqlite3.connect(self.index) as db:
            db.execute('DELETE FROM meta WHERE key=?', ('pi_session_coverage',))
        self.assertIsNone(dashboard.context(self.history, self.manager).read)


if __name__ == '__main__':
    unittest.main()
