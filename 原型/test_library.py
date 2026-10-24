"""Synthetic-only coverage for unified ordering, search and source identity."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import uuid

from chat_store import BusyError, ChatStore
from history_view import HistoryView
from library import LibraryView
from native_catalogue import MAX_CHATS, NativeChatCatalogue
from test_history_view import create_native_tables, insert_native_images


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rabbit-library-')
        self.root = Path(self.temp.name)
        self.index = self.root / 'history.sqlite'
        # A small version-1 native index; no real chat paths or model calls.
        with sqlite3.connect(self.index) as db:
            db.executescript('''
                CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO meta VALUES('schema_version','1');
                CREATE TABLE sessions(agent TEXT,session_id TEXT,title TEXT,cwd TEXT,
                    model TEXT,kind TEXT,parent_id TEXT,archived INTEGER,
                    started_at TEXT,updated_at TEXT,message_count INTEGER,source_path TEXT,
                    PRIMARY KEY(agent,session_id));
                CREATE TABLE messages(agent TEXT,session_id TEXT,message_id TEXT,role TEXT,
                    timestamp TEXT,content TEXT,source_path TEXT,source_line INTEGER,
                    ordinal INTEGER,phase TEXT,PRIMARY KEY(agent,session_id,message_id));
                CREATE INDEX messages_session_order ON messages(agent,session_id,timestamp,ordinal);
            ''')
        self.store = ChatStore(self.root / 'chat.sqlite')
        self.history = HistoryView(self.index)
        self.library = LibraryView(self.store, self.history)

    def tearDown(self):
        self.temp.cleanup()

    def native(self, agent, session, title, timestamp, texts=(), kind='root', updated_at=None):
        with sqlite3.connect(self.index) as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (agent, session, title, '', '', kind, '', 0, timestamp,
                 updated_at or timestamp, len(texts), 'synthetic'))
            for index, text in enumerate(texts):
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (agent, session, session + '-' + str(index), 'user', timestamp,
                     text, 'synthetic', index + 1, index * 5, ''))

    def imported(self, agent, session, title, timestamp, text='导入正文'):
        return self.store.create(title, {'agent': agent, 'session_id': session}, [{
            'agent': agent, 'session_id': session, 'message_id': 'source-one',
            'role': 'user', 'timestamp': timestamp, 'content': text}])['id']

    def test_excluded_native_pair_is_exact_and_independent_of_internal_toggle(self):
        for agent,session in [('codex','bound'),('codex','other'),('hermes','bound')]:
            self.native(agent,session,'小白兔专员同名','2026-01-01T00:00:00Z',['serviceName=xiaobaitu'])
        self.library=LibraryView(self.store,self.history,excluded_source_getter=lambda:('codex','bound'))
        for internal in (False,True):
            result=self.library.list(include_internal=internal)
            self.assertEqual(result['total'],2)
            self.assertEqual({(row['agent'],row['session_id']) for row in result['items']},
                             {('codex','other'),('hermes','bound')})

    def test_excluded_native_pair_is_removed_before_total_and_every_page(self):
        for index in range(7):
            self.native('codex',str(index),'普通历史','2026-01-%02dT00:00:00Z'%(index+1),['正文'])
        expected=self.library.list(limit=100)['items']
        for removed in ('6','3','0'):
            with self.subTest(removed=removed):
                library=LibraryView(self.store,self.history,excluded_source_getter=lambda:('codex',removed))
                actual,offset=[],0
                while offset is not None:
                    page=library.list(offset=offset,limit=2)
                    self.assertEqual(page['total'],6)
                    self.assertEqual(len(page['items']),2)
                    actual.extend(page['items']);offset=page['next_offset']
                self.assertEqual([row['key'] for row in actual],
                                 [row['key'] for row in expected if row['session_id']!=removed])

    def test_excluded_native_pair_preserves_filter_search_and_match_position(self):
        self.native('codex','bound','专员标题','2026-01-01T00:00:00Z',['只在专员这里'])
        self.native('codex','other','普通标题','2026-01-01T00:00:00Z',['开头','正常 needle'])
        self.native('hermes','bound','普通标题','2026-01-01T00:00:00Z',['正常 needle'])
        self.library=LibraryView(self.store,self.history,excluded_source_getter=lambda:('codex','bound'))
        for agent in ('all','codex','hermes'):
            self.assertEqual(self.library.list(query='只在专员这里',agent=agent)['total'],0)
        self.assertEqual(self.library.list(query='专员标题',include_internal=True)['total'],0)
        for agent,total in [('all',2),('codex',1),('hermes',1)]:
            result=self.library.list(query='正常 needle',agent=agent)
            self.assertEqual(result['total'],total)
        row=self.library.list(query='needle',agent='codex')['items'][0]
        self.assertEqual((row['match']['message_id'],row['match']['index']),('other-1',1))

    def test_excluded_native_pair_keeps_explicit_shared_branches_and_source_read(self):
        self.native('codex','bound','原专员文字','2026-01-01T00:00:00Z',['独立保留原文'])
        first=self.imported('codex','bound','用户副本一','2026-01-01T00:00:00Z','独立保留原文')
        second=self.imported('codex','bound','用户副本二','2026-01-01T00:00:00Z','独立保留原文')
        self.library=LibraryView(self.store,self.history,excluded_source_getter=lambda:('codex','bound'))
        result=self.library.list(query='独立保留原文',agent='codex')
        self.assertEqual(result['total'],2)
        self.assertEqual({row['id'] for row in result['items']},{first,second})
        self.assertEqual(self.history.get_session('codex','bound')['messages'][0]['content'],'独立保留原文')

    def test_exclusion_binding_changes_revision_and_is_read_twice_per_list(self):
        source=[None];calls=[]
        def getter():calls.append(source[0]);return source[0]
        original=self.library.revision()
        self.library=LibraryView(self.store,self.history,excluded_source_getter=getter)
        self.assertEqual(self.library.revision(),original)
        calls.clear();self.library.list();self.assertEqual(calls,[None,None])
        source[0]=('codex','bound')
        changed=self.library.revision();self.assertNotEqual(changed,original)
        reopened=LibraryView(self.store,self.history,excluded_source_getter=getter)
        self.assertEqual(reopened.revision(),changed)
        source[0]=('codex','other');self.assertNotEqual(self.library.revision(),changed)

    def test_binding_change_between_query_and_final_snapshot_is_busy(self):
        self.native('codex','bound','普通历史','2026-01-01T00:00:00Z',['正文'])
        calls=[]
        def getter():
            calls.append(1)
            return None if len(calls)==1 else ('codex','bound')
        self.library=LibraryView(self.store,self.history,excluded_source_getter=getter)
        with self.assertRaises(BusyError):self.library.list()
        self.assertEqual(len(calls),2)

    def test_binding_read_failure_is_not_treated_as_absence(self):
        def getter():raise RuntimeError('synthetic binding unavailable')
        self.library=LibraryView(self.store,self.history,excluded_source_getter=getter)
        with mock.patch.object(self.history,'_open') as opened:
            with self.assertRaisesRegex(RuntimeError,'synthetic binding unavailable'):self.library.list()
            opened.assert_not_called()
        with self.assertRaises(RuntimeError):self.library.revision()

    def test_precise_exclusion_is_read_only_and_missing_history_keeps_common_chat(self):
        self.native('codex','bound','原文','2026-01-01T00:00:00Z',['原文'])
        common=self.store.create('用户自己的对话')['id']
        self.library=LibraryView(self.store,self.history,excluded_source_getter=lambda:('codex','bound'))
        before={path:path.read_bytes() for path in (self.index,self.store.path)}
        self.library.list();self.library.list(query='原文',include_internal=True);self.library.revision()
        self.assertEqual({path:path.read_bytes() for path in before},before)
        self.index.unlink()
        self.assertEqual([row['id'] for row in self.library.list()['items']],[common])

    def test_order_uses_actual_message_time_not_import_or_index_refresh_time(self):
        self.native('hermes', 'middle', '中间历史', '2026-02-01T00:00:00Z', ['历史'],
                    updated_at='2026-12-31T00:00:00Z')
        self.native('codex', 'old', '旧历史', '2026-01-01T00:00:00Z', ['原文'])
        chat = self.imported('codex', 'old', '旧历史', '2026-01-01T00:00:00Z')
        rows = self.library.list()['items']
        self.assertEqual([r['key'] for r in rows], ['history:hermes:middle', 'chat:' + chat])
        self.assertEqual(rows[-1]['timestamp'], '2026-01-01T00:00:00.000000Z')
        # Touching the chat operational timestamp must not change its order.
        with self.store._open() as db:
            db.execute("UPDATE chats SET updated_at='2030-01-01T00:00:00Z' WHERE id=?", (chat,))
        self.assertEqual(self.library.list()['items'][-1]['id'], chat)
        run, _ = self.store.submit(chat, '现在接续', 'hermes', 'continue')
        self.store.update_run(run['id'], status='completed', text='新的回答')
        top = self.library.list()['items'][0]
        self.assertEqual(top['id'], chat)
        self.assertEqual(top['source_agents'], ['codex', 'hermes'])

    def test_date_normalization_orders_fractional_seconds_and_timezones(self):
        self.native('codex', 'a', '稍早', '2026-01-01T08:00:00+08:00', ['A'])
        self.native('codex', 'b', '稍后', '2026-01-01T00:00:00.100000Z', ['B'])
        self.assertEqual([r['session_id'] for r in self.library.list()['items']], ['b', 'a'])

    def test_native_de_duplication_keeps_independent_shared_branches_and_source_view(self):
        self.native('codex', 'old', '原始历史', '2026-01-01T00:00:00Z', ['原文'])
        first = self.imported('codex', 'old', '分支一', '2026-01-01T00:00:00Z')
        second = self.imported('codex', 'old', '分支二', '2026-01-01T00:00:00Z')
        response = self.library.list()
        self.assertEqual(response['total'], 2)
        self.assertEqual({r['id'] for r in response['items']}, {first, second})
        self.assertEqual(self.history.get_session('codex', 'old')['messages'][0]['content'], '原文')

    def test_imported_provenance_without_chat_source_still_suppresses_original(self):
        self.native('hermes', 'old', '原历史', '2026-01-01T00:00:00Z', ['原文'])
        chat = self.store.create('仅保存逐条来源', imported=[{'agent': 'hermes', 'session_id': 'old',
            'message_id': 'source', 'timestamp': '2026-01-01T00:00:00Z', 'role': 'user', 'content': '原文'}])
        self.assertEqual([r['key'] for r in self.library.list()['items']], ['chat:' + chat['id']])

    def test_missing_source_never_gets_an_invented_agent_badge(self):
        chat = self.store.create('未知来源', imported=[{'role': 'user', 'content': '旧字',
            'timestamp': '2020-01-01T00:00:00Z'}])['id']
        row = self.library.list()['items'][0]
        self.assertEqual(row['id'], chat)
        self.assertEqual(row['source_agents'], [])
        self.assertEqual(self.library.list(agent='codex')['total'], 0)
        self.assertEqual(self.library.list(agent='hermes')['total'], 0)

    def test_title_body_search_and_source_filter_intersect_for_both_sources(self):
        self.native('codex', 'c', '青色纸船', '2026-01-01T00:00:00Z', ['普通文字', 'PASSWORD café'])
        self.native('hermes', 'h', '青色纸船', '2026-01-02T00:00:00Z', ['PASSWORD café'])
        mixed = self.imported('codex', 'c', '青色共同聊天', '2026-01-01T00:00:00Z', 'PASSWORD café')
        run, _ = self.store.submit(mixed, '接续的 Hermes 问题', 'hermes', 'mix')
        self.store.update_run(run['id'], status='completed', text='保持原文字')
        response = self.library.list(query='青色 password', agent='hermes')
        self.assertEqual({r['key'] for r in response['items']}, {'chat:' + mixed, 'history:hermes:h'})
        self.assertEqual(self.library.list(query='不存在', agent='hermes')['total'], 0)
        self.assertEqual(self.library.list(query='青色', agent='codex')['total'], 1)
        self.assertTrue(all('match' in r for r in response['items']))
        self.assertEqual(self.library.list(query='青色共同')['items'][0].get('match'), None)

    def test_search_returns_exact_message_indices_in_each_underlying_order(self):
        self.native('codex', 'c', '标题', '2026-01-01T00:00:00Z', ['一', '二', 'target'])
        row = self.library.list(query='target')['items'][0]
        self.assertEqual(row['match']['index'], 2)
        self.assertEqual(row['match']['message_id'], 'c-2')
        chat = self.imported('hermes', 'elsewhere', '另一个标题', '2026-01-01T00:00:00Z', '先前')
        run, _ = self.store.submit(chat, 'target', 'codex', 'later')
        self.store.update_run(run['id'], status='completed', text='答复')
        row = next(r for r in self.library.list(query='target')['items'] if r['kind'] == 'chat')
        messages = self.store.get(chat)['messages']
        self.assertEqual(row['match']['index'], 1)
        self.assertEqual(row['match']['message_id'], messages[1]['id'])

    def test_pagination_has_stable_tie_order_without_duplicate_or_missing_rows(self):
        for index in range(7):
            self.native('codex', str(index), '同一时刻', '2026-01-01T00:00:00Z', ['原文'])
        for index in range(3):
            self.imported('hermes', 'unindexed-' + str(index), '共同对话', '2026-01-01T00:00:00Z')
        expected = self.library.list(limit=100)['items']
        actual, offset = [], 0
        while offset is not None:
            page = self.library.list(offset=offset, limit=3)
            actual.extend(page['items'])
            offset = page['next_offset']
        self.assertEqual([r['key'] for r in actual], [r['key'] for r in expected])
        self.assertEqual(len({r['key'] for r in actual}), 10)

    def test_child_and_approval_history_remain_hidden_by_default(self):
        self.native('codex', 'child', '子代理', '2026-01-01T00:00:00Z', ['协作'], kind='child')
        self.native('codex', 'approval', '自动审批', '2026-01-01T00:00:00Z',
                    ['The following is the Codex agent history added since your last approval assessment.\nContext'])
        self.native('codex', 'root', '正常提到 agent', '2026-01-01T00:00:00Z', ['agent'])
        self.assertEqual(self.library.list()['total'], 1)
        self.assertEqual(self.library.list(include_internal=True)['total'], 3)

    def test_generated_native_title_and_revision_follow_index_changes(self):
        before = self.library.revision()
        self.native('codex', 'generated', '', '2026-01-01T00:00:00Z',
            ['<environment_context>环境</environment_context>\n去做一只纸船'])
        result = self.library.list(query='纸船')
        self.assertEqual(result['items'][0]['title'], '去做一只纸船')
        self.assertNotEqual(before, result['revision'])
        self.assertEqual(result['revision'], self.library.revision())
        self.store.create('新增')
        self.assertNotEqual(result['revision'], self.library.revision())

    def test_missing_native_index_keeps_shared_chats_usable_without_creating_index(self):
        self.index.unlink()
        chat = self.store.create('保留共同聊天')['id']
        result = self.library.list()
        self.assertEqual([r['id'] for r in result['items']], [chat])
        self.assertIn('notice', result)
        self.assertFalse(self.index.exists())

    def test_invalid_inputs_are_rejected_and_search_is_literal(self):
        self.native('codex', 'literal', '100%_done', '2026-01-01T00:00:00Z', ['正文'])
        self.assertEqual(self.library.list(query='%_')['total'], 1)
        for kwargs in ({'agent': 'other'}, {'limit': 0}, {'limit': 101}, {'offset': -1},
                       {'query': 'x' * 401}, {'include_internal': 'yes'}, {'limit': True}):
            with self.assertRaises(ValueError):
                self.library.list(**kwargs)

    def linked_native(self, texts=('原约定', '原回答')):
        self.native('codex', 'linked', '原生来源', '2026-01-01T00:00:00Z', texts)
        source = {'agent': 'codex', 'session_id': 'linked'}
        messages = [dict(message, **source) for message in self.history.get_session(**source)['messages']]
        return self.store.create('共同接续', source, messages)['id']

    def test_linked_source_tail_is_searchable_without_importing_or_duplicating_list_rows(self):
        chat = self.linked_native()
        before = self.store.path.read_bytes()
        self.assertFalse(self.library.list()['items'][0]['source_update']['has_changes'])
        with sqlite3.connect(self.index) as db:
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('codex', 'linked', 'linked-new', 'assistant', '2026-03-01T00:00:00Z',
                 '新约定紫色风车9286', 'synthetic', 3, 10, 'final'))
            db.execute("UPDATE sessions SET updated_at='2030-01-01T00:00:00Z',message_count=3 WHERE session_id='linked'")
        result = self.library.list(query='紫色风车9286')
        self.assertEqual(result['total'], 1)
        row = result['items'][0]
        self.assertEqual((row['kind'], row['id']), ('chat', chat))
        self.assertEqual(row['timestamp'], '2026-03-01T00:00:00.000000Z')
        self.assertEqual(row['message_count'], 2)
        self.assertEqual(row['source_update']['indexed_messages'], 3)
        self.assertEqual(row['source_update']['imported_messages'], 2)
        self.assertTrue(row['source_update']['has_changes'])
        self.assertEqual(row['match']['kind'], 'history')
        self.assertEqual((row['match']['agent'], row['match']['session_id']), ('codex', 'linked'))
        self.assertEqual((row['match']['index'], row['match']['message_id']), (2, 'linked-new'))
        self.assertIn('紫色风车9286', row['match']['excerpt'])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.library.list()['total'], 1)

    def test_same_count_native_edit_is_visible_but_existing_common_text_is_preserved(self):
        chat = self.linked_native()
        before = self.store.path.read_bytes()
        with sqlite3.connect(self.index) as db:
            db.execute("UPDATE messages SET content='原生改成蓝色船锚7521' WHERE session_id='linked' AND message_id='linked-1'")
        row = self.library.list(query='蓝色船锚7521')['items'][0]
        self.assertEqual(row['id'], chat)
        self.assertTrue(row['source_update']['has_changes'])
        self.assertEqual((row['match']['kind'], row['match']['index']), ('history', 1))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.get(chat)['messages'][1]['content'], '原回答')

    def test_search_can_combine_shared_title_and_new_native_word(self):
        chat = self.linked_native()
        with sqlite3.connect(self.index) as db:
            db.execute("UPDATE messages SET content='原生新词红色树叶4917' WHERE session_id='linked' AND message_id='linked-1'")
        row = self.library.list(query='共同接续 红色树叶4917')['items'][0]
        self.assertEqual(row['id'], chat)
        self.assertEqual((row['match']['kind'], row['match']['index']), ('history', 1))

    def test_search_old_and_new_words_prioritizes_actual_missing_native_tail(self):
        chat = self.linked_native(('旧词蓝纸', '原回答'))
        with sqlite3.connect(self.index) as db:
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',
                       ('codex', 'linked', 'linked-new', 'user', '2026-03-01T00:00:00Z',
                        '新词紫伞', 'synthetic', 3, 3, ''))
        row = self.library.list(query='蓝纸 紫伞')['items'][0]
        self.assertEqual(row['id'], chat)
        self.assertEqual((row['match']['kind'], row['match']['index'], row['match']['message_id']),
                         ('history', 2, 'linked-new'))
        self.assertIn('紫伞', row['match']['excerpt'])
        self.assertEqual(row['match']['query'], '蓝纸 紫伞')

    def test_native_title_change_remains_searchable_at_original_source(self):
        chat = self.linked_native()
        with sqlite3.connect(self.index) as db:
            db.execute("UPDATE sessions SET title='后来命名的银色月亮6082' WHERE session_id='linked'")
        row = self.library.list(query='银色月亮6082')['items'][0]
        self.assertEqual(row['id'], chat)
        self.assertEqual(row['title'], '共同接续')
        self.assertEqual(row['match']['kind'], 'history')
        self.assertTrue(row['match']['title_only'])
        self.assertIn('银色月亮6082', row['source_update']['source_title'])

    def test_missing_source_keeps_copy_and_does_not_claim_new_content(self):
        chat = self.linked_native()
        with sqlite3.connect(self.index) as db:
            db.execute("DELETE FROM sessions WHERE session_id='linked'")
            db.execute("DELETE FROM messages WHERE session_id='linked'")
        row = self.library.list(query='原约定')['items'][0]
        self.assertEqual(row['id'], chat)
        self.assertFalse(row['source_update']['has_changes'])
        self.assertIsNone(row['source_update']['indexed_messages'])
        self.assertNotIn('kind', row['match'])

    def test_native_pure_images_update_application_count_time_and_search_location(self):
        self.native('codex', 'with-image', '合成图片历史', '2026-01-01T00:00:00Z', ['前文', 'target'])
        self.native('hermes', 'middle', '中间', '2026-02-01T00:00:00Z', ['正文'])
        create_native_tables(self.index)
        insert_native_images(self.index, 'codex', 'with-image', 'between-image', ordinal=2,
                             timestamp='2026-01-01T00:00:00Z', count=9)
        insert_native_images(self.index, 'codex', 'with-image', 'last-image', ordinal=10,
                             timestamp='2026-03-01T00:00:00Z')
        before_index, before_common = self.index.read_bytes(), self.store.path.read_bytes()
        rows = self.library.list()['items']
        self.assertEqual(rows[0]['session_id'], 'with-image')
        self.assertEqual(rows[0]['message_count'], 4)
        self.assertEqual(rows[0]['timestamp'], '2026-03-01T00:00:00.000000Z')
        match = self.library.list(query='target')['items'][0]['match']
        self.assertEqual((match['message_id'], match['index']), ('with-image-1', 2))
        self.assertEqual(self.history.get_session('codex', 'with-image', offset=match['index'], limit=1)['messages'][0]['message_id'], match['message_id'])
        self.assertEqual(self.index.read_bytes(), before_index)
        self.assertEqual(self.store.path.read_bytes(), before_common)

    def test_linked_attachment_fingerprints_change_hint_without_materializing_the_library(self):
        chat = self.linked_native()
        create_native_tables(self.index)
        descriptors = insert_native_images(self.index, 'codex', 'linked', 'linked-0', pure=False)
        class NeverResolve:
            def materialize(self, *_args):
                raise AssertionError('A library query must not materialize images')
        self.history.materializer = NeverResolve()
        before = self.store.path.read_bytes()
        row = self.library.list()['items'][0]
        self.assertEqual(row['id'], chat)
        self.assertTrue(row['source_update']['has_changes'])
        self.assertEqual((row['source_update']['indexed_messages'], row['source_update']['imported_messages']), (2, 2))
        self.assertEqual(self.store.path.read_bytes(), before)
        # Source descriptors are unchanged whether or not their material copy
        # is ready. A materialization status is not a native source edit.
        public = dict(descriptors[0], attachment_id='img_' + '1' * 32,
                      material_state='ready', material_reason=None)
        with self.store._open() as db:
            db.execute("UPDATE messages SET native_attachments=? WHERE chat_id=? AND content='原约定'",
                       (json.dumps([public]), chat))
        self.assertFalse(self.library.list()['items'][0]['source_update']['has_changes'])
        public.update(attachment_id=None, material_state='unavailable', material_reason='source_missing')
        with self.store._open() as db:
            db.execute("UPDATE messages SET native_attachments=? WHERE chat_id=? AND content='原约定'",
                       (json.dumps([public]), chat))
        self.assertFalse(self.library.list()['items'][0]['source_update']['has_changes'])
        with sqlite3.connect(self.index) as db:
            db.execute("UPDATE native_attachments SET fingerprint=? WHERE session_id='linked'", ('d' * 64,))
        self.assertTrue(self.library.list()['items'][0]['source_update']['has_changes'])

    def test_linked_source_pure_image_insertion_is_an_update_without_duplicate_list_row(self):
        chat = self.linked_native()
        create_native_tables(self.index)
        insert_native_images(self.index, 'codex', 'linked', 'linked-image', ordinal=1,
                             timestamp='2026-03-01T00:00:00Z')
        row = self.library.list()['items'][0]
        self.assertEqual((row['kind'], row['id']), ('chat', chat))
        self.assertEqual(self.library.list()['total'], 1)
        self.assertEqual(row['message_count'], 2)
        self.assertEqual(row['timestamp'], '2026-03-01T00:00:00.000000Z')
        self.assertEqual(row['source_update']['indexed_messages'], 3)
        self.assertEqual(row['source_update']['imported_messages'], 2)
        self.assertTrue(row['source_update']['has_changes'])

    def test_common_search_index_uses_source_anchor_logical_order(self):
        chat = self.store.create('共同顺序', imported=[{'role': 'user', 'content': '第一条'},
            {'role': 'assistant', 'content': 'target reply'}])['id']
        original = self.store.get(chat)['messages']
        with self.store._open() as db:
            db.execute("""INSERT INTO messages(id,chat_id,role,content,created_at,backend,status,provenance,phase,
                attachment_ids,native_attachments,source_anchor) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ('synthetic-later-pure-image', chat, 'user', '', '2026-01-01T00:00:00Z', 'codex', 'imported',
                 '{}', '', '[]', '[]', original[1]['id']))
            physical = [row['id'] for row in db.execute('SELECT id FROM messages WHERE chat_id=? ORDER BY ordinal', (chat,))]
        self.assertEqual(physical, [original[0]['id'], original[1]['id'], 'synthetic-later-pure-image'])
        logical = self.store.get(chat)['messages']
        self.assertEqual([row['id'] for row in logical], [original[0]['id'], 'synthetic-later-pure-image', original[1]['id']])
        found = self.library.list(query='target')['items'][0]['match']
        self.assertEqual((found['message_id'], found['index']), (original[1]['id'], 2))
        self.assertEqual(logical[found['index']]['id'], found['message_id'])

    def test_legacy_common_schema_without_native_descriptors_keeps_list_and_search_working(self):
        chat = self.linked_native()
        create_native_tables(self.index)
        insert_native_images(self.index, 'codex', 'linked', 'linked-0', pure=False)
        with self.store._open() as db:
            # Recreate only this synthetic table using the pre-image columns;
            # no SQLite DROP COLUMN version assumption is needed.
            db.executescript("""CREATE TABLE legacy_messages AS SELECT ordinal,id,chat_id,role,content,created_at,
                backend,status,provenance,phase FROM messages;
                DROP TABLE messages; ALTER TABLE legacy_messages RENAME TO messages;""")
        row = self.library.list(query='原回答')['items'][0]
        self.assertEqual(row['id'], chat)
        self.assertTrue(row['source_update']['has_changes'])
        self.assertEqual(row['match']['index'], 1)
        self.assertEqual(row['match']['message_id'], self.store.get(chat)['messages'][1]['id'])


class NativeBindingLibraryTests(unittest.TestCase):
    """Exact existing-index links, with only invented catalogue/binding metadata."""
    setUp = LibraryTests.setUp
    tearDown = LibraryTests.tearDown
    native = LibraryTests.native
    imported = LibraryTests.imported

    def catalogued(self, title='原生目录', *, created_at='2026-06-01T00:00:00Z', identity=None, agent='codex'):
        catalogue = NativeChatCatalogue(self.store.path, agent=agent)
        row = catalogue.create(str(uuid.uuid4()), title, lambda _identity: None, fixed_id=identity)
        with sqlite3.connect(self.store.path) as db:
            db.execute(f'UPDATE {catalogue.table} SET created_at=? WHERE id=?', (created_at, row['id']))
        return row

    @staticmethod
    def binding(row, session, *, adopted=False):
        return {'agent': row['agent'], 'chat_id': row['id'], 'session_id': session, 'adopted': adopted}

    def linked_library(self, bindings, *, excluded=None, enabled=(), single=None):
        return LibraryView(self.store, self.history, native_bindings_getter=lambda: bindings,
            excluded_sources_getter=lambda: (excluded if excluded is not None else
                [(row['agent'], row['session_id']) for row in bindings]),
            excluded_source_getter=single, enabled_native_agents_getter=lambda: enabled)

    def test_same_title_different_native_sids_and_agents_do_not_cross_match(self):
        first, second = self.catalogued('同名目录'), self.catalogued('同名目录')
        self.native('codex', 'source-a', '索引A', '2026-01-01T00:00:00Z', ['开头', 'Alpha 独有'])
        self.native('codex', 'source-b', '索引B', '2026-01-01T00:00:00Z', ['Beta 独有'])
        self.native('hermes', 'source-a', '其他Agent', '2026-01-01T00:00:00Z', ['Alpha 独有'])
        bindings = [self.binding(first, 'source-a', adopted=True), self.binding(second, 'source-b')]
        library = self.linked_library(bindings)
        result = library.list(query='alpha', agent='codex')
        self.assertEqual(result['total'], 1)
        row = result['items'][0]
        self.assertEqual((row['kind'], row['id'], row['key']),
                         ('native', first['id'], 'native:codex:' + first['id']))
        self.assertNotIn('session_id', row)
        self.assertNotIn('match', row)
        self.assertEqual(row['search_scope'], 'indexed_text')
        self.assertEqual(row['index_source'], {'agent': 'codex', 'session_id': 'source-a'})
        self.assertEqual(row['indexed_message_count'], 2)
        self.assertEqual(row['timestamp_kind'], 'catalogued')
        self.assertFalse(row['available'])
        self.assertIsNone(row['message_count'])
        self.assertIsNone(row['running'])
        self.assertEqual(row['indexed_match'], {'agent': 'codex', 'session_id': 'source-a',
            'message_id': 'source-a-1', 'index': 1, 'query': 'alpha', 'excerpt': 'Alpha 独有'})
        self.assertEqual({item['key'] for item in library.list(query='alpha')['items']},
                         {row['key'], 'history:hermes:source-a'})
        self.assertEqual(library.list(query='同名 alpha')['items'][0]['id'], first['id'])
        titled = library.list(query='同名', agent='codex')['items']
        self.assertEqual(len(titled), 2)
        self.assertTrue(all(item['match']['title_only'] and 'indexed_match' not in item for item in titled))
        self.assertEqual(next(item for item in titled if item['id'] == second['id'])['timestamp_kind'], 'created')

    def test_index_body_filter_count_and_pagination_keep_native_rows_and_explicit_common_branch(self):
        first = self.catalogued('新目录', created_at='2026-06-01T00:00:00Z')
        second = self.catalogued('旧目录', created_at='2026-05-01T00:00:00Z')
        for sid, date in [('a', '2026-01-01'), ('b', '2026-01-02'),
                          ('unmapped', '2026-04-01'), ('orphan', '2026-02-01')]:
            self.native('codex', sid, '索引 ' + sid, date + 'T00:00:00Z', ['needle text'])
        common = self.imported('codex', 'a', '用户独立保存副本', '2026-03-01T00:00:00Z', 'needle text')
        bindings = [self.binding(first, 'a'), self.binding(second, 'b'),
                    {'agent': 'codex', 'chat_id': str(uuid.uuid4()), 'session_id': 'orphan', 'adopted': True}]
        library = self.linked_library(bindings)
        expected = ['native:codex:' + first['id'], 'native:codex:' + second['id'],
                    'history:codex:unmapped', 'chat:' + common, 'history:codex:orphan']
        actual, offset, revision = [], 0, library.revision()
        while offset is not None:
            page = library.list(query='needle', agent='codex', offset=offset, limit=2)
            self.assertEqual(page['total'], 5)
            self.assertEqual(page['revision'], revision)
            actual.extend(row['key'] for row in page['items'])
            offset = page['next_offset']
        self.assertEqual(actual, expected)
        self.assertEqual(len(set(actual)), 5)
        self.assertEqual(self.store.get(common)['messages'][0]['content'], 'needle text')

    def test_unpublished_binding_keeps_history_until_fixed_directory_is_published(self):
        self.native('codex', 'reserved', '旧历史', '2026-01-01T00:00:00Z', ['原文字词'])
        identity = str(uuid.uuid4())
        bindings = [{'agent': 'codex', 'chat_id': identity, 'session_id': 'reserved', 'adopted': True}]
        library = self.linked_library(bindings)
        common_revision = self.store.changes()['revision']
        before = library.list(query='原文字词')
        self.assertEqual([row['key'] for row in before['items']], ['history:codex:reserved'])
        # An omitted callback deliberately preserves the previous contract.
        legacy = LibraryView(self.store, self.history,
                             excluded_sources_getter=lambda: [('codex', 'reserved')])
        self.assertEqual(legacy.list()['total'], 0)
        self.catalogued('采用的旧会话', identity=identity)
        published = library.list(query='原文字词')
        self.assertEqual([row['key'] for row in published['items']], ['native:codex:' + identity])
        self.assertNotEqual(before['revision'], published['revision'])
        self.assertEqual(self.store.changes()['revision'], common_revision)
        with sqlite3.connect(self.store.path) as db:
            db.execute('DELETE FROM native_chat_catalogue WHERE id=?', (identity,))
        restored = library.list(query='原文字词')
        self.assertEqual([row['key'] for row in restored['items']], ['history:codex:reserved'])
        self.assertNotEqual(restored['revision'], published['revision'])

    def test_supplied_empty_mapping_keeps_unpublished_supported_agents_and_preserves_other_exclusions(self):
        for agent, sid in [('codex', 'unknown'), ('codex', 'specialist'), ('hermes', 'owned'), ('pi', 'owned'), ('dsh', 'owned')]:
            self.native(agent, sid, '历史', '2026-01-01T00:00:00Z', ['正文'])
        library = self.linked_library([], excluded=[('codex', 'unknown'), ('hermes', 'owned'), ('pi', 'owned'), ('dsh', 'owned')],
                                      single=lambda: ('codex', 'specialist'))
        self.assertEqual([row['key'] for row in library.list(include_internal=True)['items']],
                         ['history:codex:unknown', 'history:hermes:owned', 'history:pi:owned'])

    def test_missing_index_source_or_index_keeps_disabled_native_metadata_without_false_index_claim(self):
        row = self.catalogued()
        bindings = [self.binding(row, 'later-source', adopted=True)]
        library = self.linked_library(bindings)
        original = library.list()['items'][0]
        self.assertEqual(original['search_scope'], 'title')
        self.assertEqual(original['timestamp_kind'], 'catalogued')
        self.assertNotIn('index_source', original)
        self.assertNotIn('indexed_message_count', original)
        self.native('codex', 'later-source', '原生索引', '2026-01-01T00:00:00Z', ['after-indexing'])
        indexed = library.list(query='after-indexing')['items'][0]
        self.assertEqual(indexed['id'], row['id'])
        self.assertEqual(indexed['search_scope'], 'indexed_text')
        self.assertFalse(indexed['available'])
        with sqlite3.connect(self.index) as db:
            # Stray message rows alone do not prove the indexed source exists.
            db.execute('DELETE FROM sessions WHERE agent=? AND session_id=?', ('codex', 'later-source'))
        self.assertEqual(library.list(query='after-indexing')['total'], 0)
        self.assertEqual(library.list()['items'][0]['search_scope'], 'title')
        self.index.unlink()
        result = library.list(query='原生目录')
        self.assertEqual(result['items'][0]['id'], row['id'])
        self.assertEqual(result['items'][0]['search_scope'], 'title')
        self.assertIn('notice', result)
        self.assertFalse(self.index.exists())

    def test_bindings_callback_is_lock_free_canonical_and_part_of_final_revision_fence(self):
        first, second = self.catalogued('A'), self.catalogued('B')
        bindings = [self.binding(first, 'a'), self.binding(second, 'b')]
        calls = []
        def observed():
            self.assertFalse(self.store.lock._is_owned())
            calls.append(1)
            return bindings
        library = LibraryView(self.store, self.history, native_bindings_getter=observed)
        initial = library.list()['revision']
        self.assertEqual(len(calls), 2)
        bindings[:] = [bindings[1], bindings[0], dict(bindings[0])]
        self.assertEqual(library.revision(), initial)
        bindings[0] = dict(bindings[0], adopted=True)
        self.assertNotEqual(library.revision(), initial)
        calls.clear()
        def changing():
            self.assertFalse(self.store.lock._is_owned())
            calls.append(1)
            return bindings if len(calls) == 1 else [dict(bindings[0], session_id='changed')]
        with self.assertRaises(BusyError):
            LibraryView(self.store, self.history, native_bindings_getter=changing).list()
        self.assertEqual(len(calls), 2)
        orphan = [{'agent': 'codex', 'chat_id': str(uuid.uuid4()), 'session_id': 'unpublished', 'adopted': True}]
        self.assertNotEqual(self.linked_library(orphan).revision(), self.linked_library([]).revision())

    def test_invalid_or_ambiguous_binding_metadata_is_not_treated_as_empty(self):
        row = self.catalogued()
        valid = self.binding(row, 'source')
        invalid = [None, (), {}, 'codex', [dict(valid, agent='unknown-agent')], [dict(valid, chat_id='bad')],
            [dict(valid, session_id='with space')], [dict(valid, adopted=1)], [dict(valid, extra=True)],
            [valid, dict(valid, session_id='other')],
            [valid, dict(valid, chat_id=str(uuid.uuid4()))]]
        for values in invalid:
            with self.subTest(values=repr(values)), mock.patch.object(self.history, '_open') as opened:
                library = self.linked_library(values)
                # Keep the older scope callback valid while validating this one.
                library._excluded_sources_getter = lambda: []
                with self.assertRaises(ValueError): library.list()
                with self.assertRaises(ValueError): library.revision()
                opened.assert_not_called()
        def unavailable():
            raise RuntimeError('invented binding metadata unavailable')
        library = LibraryView(self.store, self.history, native_bindings_getter=unavailable)
        with mock.patch.object(self.history, '_open') as opened:
            with self.assertRaisesRegex(RuntimeError, 'invented binding metadata unavailable'):
                library.list()
            opened.assert_not_called()
        with self.assertRaises(ValueError):
            LibraryView(self.store, self.history, native_bindings_getter=[])

    def test_linked_index_search_never_materializes_or_copies_source_content(self):
        row = self.catalogued()
        self.native('codex', 'with-native-image', '历史', '2026-01-01T00:00:00Z', ['body-only-needle'])
        create_native_tables(self.index)
        insert_native_images(self.index, 'codex', 'with-native-image', 'image-only', ordinal=2)
        self.history.materializer = mock.Mock()
        library = self.linked_library([self.binding(row, 'with-native-image')])
        before = {path: path.read_bytes() for path in (self.store.path, self.index)}
        with mock.patch.object(self.history, 'get_session', side_effect=AssertionError('No source body RPC.')):
            result = library.list(query='body-only-needle')
            library.revision()
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['indexed_message_count'], 2)
        self.assertEqual(result['items'][0]['indexed_match']['message_id'], 'with-native-image-0')
        self.history.materializer.materialize.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM chats').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)

    def test_hermes_compression_chain_is_one_native_row_with_all_actual_index_sources(self):
        row = self.catalogued('同名目录', agent='hermes')
        self.native('hermes', 'chain-a', '较新索引', '2026-02-01T00:00:00Z', ['开头', 'Alpha'])
        self.native('hermes', 'chain-b', '较旧索引', '2026-01-01T00:00:00Z', ['Beta'])
        self.native('hermes', 'independent', '同名目录', '2026-01-01T00:00:00Z', ['独立正文'])
        bindings = [self.binding(row, sid, adopted=True) for sid in ('chain-b', 'not-indexed', 'chain-a')]
        bindings.append(dict(bindings[0]))
        library = self.linked_library(bindings, enabled=('hermes',))
        result = library.list(query='Alpha Beta', agent='hermes')
        self.assertEqual(result['total'], 1)
        item = result['items'][0]
        sources = [{'agent': 'hermes', 'session_id': sid} for sid in ('chain-a', 'chain-b')]
        self.assertEqual(item['index_sources'], sources)
        self.assertEqual(item['index_source'], sources[0])
        self.assertEqual(item['indexed_message_count'], 3)
        self.assertEqual(item['indexed_match'], {'agent': 'hermes', 'session_id': 'chain-b',
            'message_id': 'chain-b-0', 'index': 0, 'query': 'Alpha Beta', 'excerpt': 'Beta'})
        self.assertEqual(item['timestamp_kind'], 'catalogued')
        self.assertTrue(item['available'])
        self.assertIsNone(item['message_count'])
        self.assertIsNone(item['running'])
        self.assertEqual({entry['key'] for entry in library.list()['items']},
                         {'native:hermes:' + row['id'], 'history:hermes:independent'})
        self.assertEqual(library.list(query='同名', agent='hermes')['total'], 2)

    def test_multi_source_hit_uses_history_position_and_stable_sid_tie_break(self):
        row = self.catalogued(agent='hermes')
        self.native('hermes', 'a', '索引', '2026-02-01T00:00:00Z', ['之前', 'needle A'])
        self.native('hermes', 'z', '索引', '2026-01-01T00:00:00Z', ['之前', 'needle Z'])
        bindings = [self.binding(row, 'z'), self.binding(row, 'a')]
        library = self.linked_library(bindings)
        match = library.list(query='needle')['items'][0]['indexed_match']
        self.assertEqual((match['session_id'], match['message_id'], match['index']), ('z', 'z-1', 1))
        # Equal source-local positions are resolved by SID, independent of the
        # callback's order. The returned ordinal remains local to that source.
        with sqlite3.connect(self.index) as db:
            db.execute("UPDATE messages SET timestamp='2026-01-01T00:00:00Z'")
            db.execute("UPDATE messages SET message_id='same-hit' WHERE ordinal=5")
        revision = library.revision()
        first = library.list(query='needle')['items'][0]['indexed_match']
        bindings.reverse()
        second = library.list(query='needle')['items'][0]['indexed_match']
        self.assertEqual(first, second)
        self.assertEqual((first['session_id'], first['message_id'], first['index']), ('a', 'same-hit', 1))
        self.assertEqual(library.revision(), revision)

    def test_same_app_uuid_and_same_sid_across_agents_have_independent_binding_namespaces(self):
        identity = str(uuid.uuid4())
        codex = self.catalogued('同名目录', identity=identity)
        hermes = self.catalogued('同名目录', identity=identity, agent='hermes')
        self.native('codex', 'shared', '索引', '2026-01-01T00:00:00Z', ['Codex-only'])
        self.native('hermes', 'shared', '索引', '2026-01-01T00:00:00Z', ['Hermes-only'])
        library = self.linked_library([self.binding(codex, 'shared', adopted=True), self.binding(hermes, 'shared')])
        self.assertEqual(library.list()['total'], 2)
        for agent, expected_stamp in [('codex', 'catalogued'), ('hermes', 'created')]:
            item = library.list(query=agent + '-only')['items'][0]
            self.assertEqual((item['key'], item['timestamp_kind']),
                             ('native:' + agent + ':' + identity, expected_stamp))
            self.assertEqual(item['index_sources'], [{'agent': agent, 'session_id': 'shared'}])
            self.assertEqual(item['indexed_match']['agent'], agent)

    def test_pi_original_index_is_one_searchable_native_entry_without_copying_body(self):
        row = self.catalogued('Pi目录', agent='pi')
        self.native('pi', 'pi.original', '原Pi', '2026-01-01T00:00:00Z', ['开头', 'needle Pi'])
        before = self.store.path.read_bytes()
        library = self.linked_library([self.binding(row, 'pi.original', adopted=True)])
        result = library.list(query='needle', agent='pi')
        self.assertEqual(result['total'], 1)
        item = result['items'][0]
        self.assertEqual((item['kind'], item['agent'], item['id'], item['timestamp_kind']),
                         ('native', 'pi', row['id'], 'catalogued'))
        self.assertEqual(item['index_sources'], [{'agent':'pi','session_id':'pi.original'}])
        self.assertEqual((item['indexed_match']['session_id'],item['indexed_match']['index']),('pi.original',1))
        self.assertEqual(item['indexed_message_count'], 2)
        self.assertIsNone(item['message_count'])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_pi_unpublished_source_is_preserved_and_only_publication_hides_the_raw_index(self):
        identity = str(uuid.uuid4())
        self.native('pi','orphan','原Pi','2026-01-01T00:00:00Z',['needle'])
        binding = {'agent':'pi','chat_id':identity,'session_id':'orphan','adopted':True}
        library = self.linked_library([binding])
        before = library.list(query='needle')
        self.assertEqual([r['key'] for r in before['items']], ['history:pi:orphan'])
        self.catalogued('Pi目录', agent='pi', identity=identity)
        after = library.list(query='needle')
        self.assertEqual([r['key'] for r in after['items']], ['native:pi:'+identity])
        self.assertNotEqual(before['revision'], after['revision'])

    def test_pi_bindings_are_single_key_and_agent_namespaces_remain_independent(self):
        identity = str(uuid.uuid4())
        rows = [self.catalogued(agent=agent, identity=identity) for agent in ('codex','hermes','pi')]
        for agent in ('codex','hermes','pi'):
            self.native(agent,'shared','索引','2026-01-01T00:00:00Z',[agent+' needle'])
        bindings = [self.binding(row,'shared',adopted=True) for row in rows]
        self.assertEqual(self.linked_library(bindings).list()['total'], 3)
        found = self.linked_library(bindings).list(query='pi needle')['items']
        self.assertEqual([r['key'] for r in found], ['native:pi:'+identity])
        with self.assertRaises(ValueError):
            self.linked_library([bindings[-1],dict(bindings[-1],session_id='another')]).revision()

    def test_hermes_chain_pagination_counts_once_and_orphan_history_stays_navigable(self):
        first = self.catalogued('新目录', agent='hermes', created_at='2026-06-01T00:00:00Z')
        second = self.catalogued('旧目录', agent='hermes', created_at='2026-05-01T00:00:00Z')
        for sid, date in [('a', '2026-01-01'), ('b', '2026-01-02'), ('c', '2026-01-03'),
                          ('unmapped', '2026-04-01'), ('orphan', '2026-02-01')]:
            self.native('hermes', sid, '同名索引', date + 'T00:00:00Z', ['needle text'])
        common = self.imported('hermes', 'a', '用户独立保存副本', '2026-03-01T00:00:00Z', 'needle text')
        orphan_id = str(uuid.uuid4())
        bindings = [self.binding(first, 'a', adopted=True), self.binding(first, 'b', adopted=True),
                    self.binding(second, 'c'),
                    {'agent': 'hermes', 'chat_id': orphan_id, 'session_id': 'orphan', 'adopted': True}]
        library = self.linked_library(bindings)
        actual, offset = [], 0
        while offset is not None:
            page = library.list(query='needle', agent='hermes', limit=2, offset=offset)
            self.assertEqual(page['total'], 5)
            actual.extend(item['key'] for item in page['items'])
            offset = page['next_offset']
        self.assertEqual(actual, ['native:hermes:' + first['id'], 'native:hermes:' + second['id'],
                                 'history:hermes:unmapped', 'chat:' + common, 'history:hermes:orphan'])
        self.catalogued('已发布', agent='hermes', identity=orphan_id)
        published = library.list(query='needle', agent='hermes')
        self.assertEqual(published['total'], 5)
        self.assertNotIn('history:hermes:orphan', {item['key'] for item in published['items']})
        self.assertIn('native:hermes:' + orphan_id, {item['key'] for item in published['items']})

    def test_hermes_binding_limits_adoption_consistency_and_source_ownership(self):
        row = self.catalogued(agent='hermes')
        valid = self.binding(row, 'source')
        invalid = [[valid, dict(valid, session_id='next', adopted=True)],
                   [valid, dict(valid, chat_id=str(uuid.uuid4()))],
                   [self.binding(row, 'key-' + str(i)) for i in range(129)],
                   [valid] * (MAX_CHATS * 128 + 1)]
        for values in invalid:
            with self.subTest(length=len(values)):
                with self.assertRaises(ValueError):
                    self.linked_library(values)._native_bindings()
        bindings = [self.binding(row, 'key-' + str(i), adopted=True) for i in range(128)]
        library = self.linked_library(bindings)
        revision = library.revision()
        bindings[:] = [dict(bindings[-1]), *reversed(bindings)]
        self.assertEqual(library.revision(), revision)
        self.assertEqual(library.list()['total'], 1)
        self.assertEqual(len(library._native_bindings()), 128)

    def test_missing_chain_keys_and_index_changes_do_not_claim_unindexed_messages(self):
        row = self.catalogued(agent='hermes')
        self.native('hermes', 'present', '索引', '2026-01-01T00:00:00Z', ['已读 needle'])
        bindings = [self.binding(row, 'present'), self.binding(row, 'missing')]
        library = self.linked_library(bindings)
        before = library.list()['items'][0]
        revision = library.revision()
        self.assertEqual(before['index_sources'], [{'agent': 'hermes', 'session_id': 'present'}])
        self.assertEqual(before['indexed_message_count'], 1)
        self.native('hermes', 'missing', '稍后索引', '2025-01-01T00:00:00Z', ['新增 needle', '补充'])
        after = library.list(query='needle')['items'][0]
        self.assertNotEqual(library.revision(), revision)
        self.assertEqual(after['indexed_message_count'], 3)
        self.assertEqual(after['indexed_match']['session_id'], 'missing')
        with sqlite3.connect(self.index) as db:
            db.execute("DELETE FROM sessions WHERE agent='hermes' AND session_id='missing'")
        remaining = library.list(query='新增')
        self.assertEqual(remaining['total'], 0)
        self.assertEqual(library.list()['items'][0]['indexed_message_count'], 1)

    def test_hermes_chain_callback_fence_is_complete_and_outside_chat_store_lock(self):
        row = self.catalogued(agent='hermes')
        original = [self.binding(row, 'first', adopted=True), self.binding(row, 'second', adopted=True)]
        for changed in [original[:-1], [*original, self.binding(row, 'third', adopted=True)],
                        [dict(item, adopted=False) for item in original]]:
            calls = []
            def getter():
                self.assertFalse(self.store.lock._is_owned())
                calls.append(1)
                return original if len(calls) == 1 else changed
            library = LibraryView(self.store, self.history, native_bindings_getter=getter)
            with self.subTest(changed=changed), self.assertRaises(BusyError):
                library.list()
            self.assertEqual(len(calls), 2)

    def test_hermes_multi_key_search_never_materializes_or_copies_index_body(self):
        row = self.catalogued(agent='hermes')
        for sid in ('first', 'second'):
            self.native('hermes', sid, '索引', '2026-01-01T00:00:00Z', ['needle ' + sid])
        library = self.linked_library([self.binding(row, 'first'), self.binding(row, 'second')])
        before = {path: path.read_bytes() for path in (self.store.path, self.index)}
        with mock.patch.object(self.history, 'get_session', side_effect=AssertionError('No body reads.')):
            result = library.list(query='needle')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['indexed_message_count'], 2)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual([db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                              for table in ('chats', 'messages', 'runs')], [0, 0, 0])


if __name__ == '__main__':
    unittest.main()
