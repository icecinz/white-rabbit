import concurrent.futures
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid

from chat_store import (ChatStore, BusyError, ContextSelectionConflict, SourceUpdateConflict,
                        source_update_comparison)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='one-ai-store-')
        self.path = Path(self.temp.name) / 'chat.sqlite'
        self.store = ChatStore(self.path)
        self.chat = self.store.create()['id']

    def tearDown(self):
        self.temp.cleanup()

    def test_concurrent_duplicate_submission_is_one_round(self):
        def submit(_):
            return self.store.submit(self.chat, '共同聊天：紫色纸船', 'codex', 'same-id')[0]['id']
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(submit, range(16)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.store.get(self.chat)['messages']), 2)
        self.assertEqual(self.store.active_count(), 1)
        with self.assertRaises(BusyError):
            self.store.submit(self.chat, '第二个请求', 'hermes', 'different')

    def test_recovery_never_replays_uncertain_submission(self):
        run, _ = self.store.submit(self.chat, '测试请求', 'codex', 'key')
        self.store.update_run(run['id'], status='running', text='未完成部分')
        restarted = ChatStore(self.path)
        self.assertEqual(restarted.recover_interrupted(), 1)
        old, created = restarted.submit(self.chat, '测试请求', 'codex', 'key')
        self.assertFalse(created)
        self.assertEqual(old['status'], 'interrupted')
        self.assertEqual(restarted.get(self.chat)['messages'][-1]['content'], '未完成部分')
        self.assertEqual(restarted.active_count(), 0)

    def test_backend_switch_retains_one_conversation_and_provenance(self):
        first, _ = self.store.submit(self.chat, '代号是纸船', 'codex', 'first')
        self.store.update_run(first['id'], status='completed', text='我记住了。')
        second, _ = self.store.submit(self.chat, '代号是什么？', 'hermes', 'second')
        self.store.update_run(second['id'], status='completed', text='纸船')
        reopened = ChatStore(self.path).get(self.chat)
        self.assertEqual(len(reopened['messages']), 4)
        self.assertEqual([m['backend'] for m in reopened['messages'] if m['role'] == 'assistant'], ['codex', 'hermes'])
        self.assertEqual(len(self.store.list()['chats']), 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_cancel_cannot_be_overwritten_by_late_completion(self):
        run, _ = self.store.submit(self.chat, '测试', 'codex', 'cancel')
        self.store.update_run(run['id'], status='cancelled', text='')
        self.store.update_run(run['id'], status='completed', text='晚到的答案')
        self.assertEqual(self.store.run(run['id'])['status'], 'cancelled')
        self.assertEqual(self.store.get(self.chat)['messages'][-1]['content'], '')

    def test_import_preserves_long_text_and_original_source(self):
        content = '原文' * 40000
        source = {'agent': 'hermes', 'session_id': 'original'}
        imported = [{'agent': 'hermes', 'session_id': 'original', 'message_id': 'm1',
                     'role': 'user', 'content': content, 'timestamp': '2026-01-01T00:00:00Z'}]
        chat = self.store.create('旧聊天', source, imported, creation_key='same-import')
        again = self.store.create('旧聊天', source, imported, creation_key='same-import')
        self.assertEqual(chat['id'], again['id'])
        message = self.store.get(chat['id'])['messages'][0]
        self.assertEqual(message['content'], content)
        self.assertEqual(message['provenance']['session_id'], 'original')

    def test_reuse_source_is_atomic_across_independent_stores_but_original_branches_remain(self):
        source = {'agent': 'codex', 'session_id': 'synthetic-original'}
        imported = [{'agent': 'codex', 'session_id': source['session_id'], 'message_id': 'm1',
                     'role': 'user', 'content': '合成原文', 'timestamp': '2026-01-01T00:00:00Z'}]
        stores = [ChatStore(self.path) for _ in range(6)]
        def create(index):
            return stores[index % len(stores)].create('复用原历史', source, imported,
                creation_key='entry-' + str(index), reuse_source=True)['id']
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            identifiers = list(pool.map(create, range(18)))
        self.assertEqual(len(set(identifiers)), 1)
        chat_id = identifiers[0]
        self.assertEqual(len(self.store.get(chat_id)['messages']), 1)
        self.assertTrue(all(self.store.find_creation('entry-' + str(index))['id'] == chat_id for index in range(18)))
        # The opt-in flag does not change deliberate independent branch creation.
        branch = self.store.create('独立分支', source, imported, creation_key='deliberate-branch')['id']
        self.assertNotEqual(branch, chat_id)
        self.assertEqual(self.store.find_source(source)['id'], branch)
        self.assertEqual(self.store.create(source=source, creation_key='new-reuse', reuse_source=True)['id'], branch)
        self.assertEqual(self.store.find_creation('entry-5')['id'], chat_id)
        self.assertEqual(ChatStore(self.path).find_creation('new-reuse')['id'], branch)

    def source_fixture(self, count=2):
        source = {'agent': 'hermes', 'session_id': 'synthetic-native-tail'}
        messages = [{'agent': source['agent'], 'session_id': source['session_id'],
                     'message_id': 'synthetic-' + str(index), 'role': 'user' if index % 2 else 'assistant',
                     'content': '合成原文完整编号' + str(index), 'timestamp': '2026-01-01T00:00:0' + str(index) + 'Z',
                     'source_path': 'synthetic-only', 'source_line': index, 'phase': 'final' if index % 2 == 0 else ''}
                    for index in range(1, count + 1)]
        return source, messages

    def test_source_tail_is_atomic_across_two_stores_and_retry_is_a_noop(self):
        source, original = self.source_fixture()
        chat_id = self.store.create('合成来源分支', source, original)['id']
        run, _ = self.store.submit(chat_id, '本机共同续聊', 'codex', 'local-branch')
        self.store.update_run(run['id'], status='completed', text='共同分支已有回答')
        view = self.store.get(chat_id)
        pinned = self.store.set_context_pin(chat_id, view['messages'][0]['id'], True, 0)
        before = self.store.get(chat_id)
        source, current = self.source_fixture(4)
        plan = source_update_comparison(source, before['messages'], current)
        other = ChatStore(self.path)
        barrier = threading.Barrier(2)
        def append(store):
            barrier.wait()
            return store.append_source_update(chat_id, source, current, plan['source_version'], int(before['revision']))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(append, store) for store in (self.store, other)]
            self.assertEqual(sorted(future.result() for future in futures), [0, 2])
        after = self.store.get(chat_id)
        self.assertEqual(after['messages'][:4], before['messages'])
        self.assertEqual(after['context_selection'], pinned)
        self.assertEqual([record['provenance']['message_id'] for record in after['messages'][-2:]],
                         ['synthetic-3', 'synthetic-4'])
        self.assertEqual(other.append_source_update(chat_id, source, current, plan['source_version'], int(before['revision'])), 0)
        self.assertEqual(self.store.get(chat_id), after)
        self.assertEqual(len(self.store.list()['chats']), 2)  # fixture's initial chat plus this source copy
        # A successful old receipt cannot hide subsequent changes in the chat.
        self.store.set_context_pin(chat_id, after['messages'][-1]['id'], True, 1)
        with self.assertRaisesRegex(SourceUpdateConflict, '共同聊天已变化'):
            other.append_source_update(chat_id, source, current, plan['source_version'], int(before['revision']))

    def test_source_tail_preserves_original_timestamp_with_current_added_date(self):
        from unittest import mock
        source, original = self.source_fixture(1)
        chat_id = self.store.create(source=source, imported=original)['id']
        before = self.store.get(chat_id)
        source, current = self.source_fixture(2)
        plan = source_update_comparison(source, before['messages'], current)
        with mock.patch('chat_store.now', return_value='2026-10-11T01:02:03Z'):
            self.store.append_source_update(chat_id, source, current, plan['source_version'], int(before['revision']))
        added = self.store.get(chat_id)['messages'][-1]
        self.assertEqual(added['created_at'], '2026-10-11T01:02:03Z')
        self.assertEqual(added['provenance']['timestamp'], current[-1]['timestamp'])
        self.assertEqual(added['phase'], current[-1]['phase'])
        self.assertEqual(added['content'], current[-1]['content'])

    def test_source_tail_rejects_every_native_prefix_mutation_without_changes(self):
        source, original = self.source_fixture()
        chat_id = self.store.create(source=source, imported=original)['id']
        before = self.store.get(chat_id)
        source, current = self.source_fixture(3)
        mutations = [current[1:], [current[1], current[0], current[2]]]
        for field in ('content', 'role', 'timestamp', 'message_id', 'phase', 'source_path', 'source_line'):
            changed = [dict(message) for message in current]
            changed[0][field] = 'assistant' if field == 'role' else 999 if field == 'source_line' else 'changed-native-field'
            mutations.append(changed)
        for changed in mutations:
            comparison = source_update_comparison(source, before['messages'], changed)
            self.assertEqual(comparison['status'], 'diverged')
            with self.assertRaisesRegex(SourceUpdateConflict, '删除、改动或顺序变化'):
                self.store.append_source_update(chat_id, source, changed, comparison['source_version'], int(before['revision']))
            self.assertEqual(self.store.get(chat_id), before)

    def test_source_tail_rejects_stale_hash_other_source_and_busy_statuses(self):
        source, original = self.source_fixture()
        source, current = self.source_fixture(3)
        for status in ('queued', 'running', 'cancelling'):
            chat_id = self.store.create(source=source, imported=original)['id']
            run, _ = self.store.submit(chat_id, '合成忙状态', 'codex', 'busy-' + status)
            if status != 'queued':
                self.store.update_run(run['id'], status=status)
            before = self.store.get(chat_id)
            plan = source_update_comparison(source, before['messages'], current)
            with self.assertRaisesRegex(BusyError, '正在回答或停止'):
                self.store.append_source_update(chat_id, source, current, plan['source_version'], int(before['revision']))
            self.assertEqual(self.store.get(chat_id), before)
        chat_id = self.store.create(source=source, imported=original)['id']
        before = self.store.get(chat_id)
        with self.assertRaisesRegex(SourceUpdateConflict, '原生来源已变化'):
            self.store.append_source_update(chat_id, source, current, '0' * 64, int(before['revision']))
        with self.assertRaisesRegex(SourceUpdateConflict, '来源已变化'):
            self.store.append_source_update(chat_id, dict(source, session_id='another-session'), current,
                                           '0' * 64, int(before['revision']))
        self.assertEqual(self.store.get(chat_id), before)

    def test_source_projection_ignores_index_display_fields_and_unrelated_shared_messages(self):
        source, original = self.source_fixture()
        chat_id = self.store.create(source=source, imported=original)['id']
        view = self.store.get(chat_id)
        decorated = [dict(message, display_content='清理后的展示', ordinal=999, message_index=99,
                          phase_label='展示') for message in original]
        self.assertEqual(source_update_comparison(source, view['messages'], decorated),
                         source_update_comparison(source, view['messages'], original))
        # Shared messages never pretend to extend the native prefix.
        unrelated = {'role': 'user', 'content': '另一来源', 'status': 'imported',
                     'provenance': {'agent': 'codex', 'session_id': 'other'}}
        self.assertEqual(source_update_comparison(source, view['messages'] + [unrelated], decorated)['status'], 'unchanged')

    def test_source_update_external_change_rolls_back_messages_revision_and_receipt(self):
        source, original = self.source_fixture()
        chat_id = self.store.create(source=source, imported=original)['id']
        before = self.store.get(chat_id)
        changes = self.store.changes(chat_id)
        source, current = self.source_fixture(3)
        plan = source_update_comparison(source, before['messages'], current)
        calls = []
        def changed_before_commit():
            calls.append(True)
            if len(calls) == 2:
                raise SourceUpdateConflict('历史索引刚刚变化，请重新查看来源更新再追加。')
        with self.assertRaises(SourceUpdateConflict):
            self.store.append_source_update(chat_id, source, current, plan['source_version'],
                                            int(before['revision']), changed_before_commit)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.get(chat_id), before)
        self.assertEqual(self.store.changes(chat_id), changes)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM source_update_receipts').fetchone()[0], 0)
        self.assertEqual(self.store.append_source_update(chat_id, source, current, plan['source_version'], int(before['revision'])), 1)

    def test_reuse_alias_tracks_its_selected_branch_even_when_another_branch_becomes_recent(self):
        source = {'agent': 'hermes', 'session_id': 'synthetic-original'}
        first = self.store.create('最初副本', source, creation_key='original-key')['id']
        reused = self.store.create(source=source, creation_key='lost-response', reuse_source=True)
        self.assertEqual(reused['id'], first)
        later = self.store.create('新的独立分支', source, creation_key='later-key')['id']
        self.assertNotEqual(later, first)
        self.assertEqual(self.store.find_source(source)['id'], later)
        self.assertEqual(self.store.create(source=source, creation_key='lost-response', reuse_source=True)['id'], first)
        # Reusing changes request metadata only, never the dialogue revision.
        before = self.store.changes()
        self.store.create(source=source, creation_key='one-more-alias', reuse_source=True)
        self.assertEqual(self.store.changes(), before)

    def test_each_reply_retains_its_model_after_restart_and_configuration_change(self):
        first, _ = self.store.submit(self.chat, '纸船', 'hermes', 'model1', model='cached-model', provider='cached-provider')
        self.store.update_run(first['id'], status='completed', text='答复一', model='actual-model-a', provider='deepseek')
        second, _ = self.store.submit(self.chat, '继续', 'hermes', 'model2', model='actual-model-b', provider='another-provider')
        self.store.update_run(second['id'], status='completed', text='答复二')
        messages = ChatStore(self.path).get(self.chat)['messages']
        replies = [m for m in messages if m['role'] == 'assistant']
        self.assertEqual([(m['model'], m['provider']) for m in replies],
                         [('actual-model-a', 'deepseek'), ('actual-model-b', 'another-provider')])
        self.assertTrue(all(m['model'] is None for m in messages if m['role'] == 'user'))

    def test_requested_route_is_frozen_durable_and_retry_cannot_replace_it(self):
        route = {'backend': 'hermes', 'model_id': 'local/hermes/model-a', 'model': 'model-a',
                 'provider': 'synthetic', 'base_url': 'https://synthetic.invalid', 'route_fingerprint': 'same-route'}
        first, created = self.store.submit(self.chat, '合成冻结', 'hermes', 'frozen',
                                           model='model-a', provider='synthetic', route=route)
        self.assertTrue(created)
        route['model'] = 'mutated-after-accept'
        loaded = ChatStore(self.path).run(first['id'])
        self.assertEqual(loaded['route']['model'], 'model-a')
        retried, created = self.store.submit(self.chat, '另一条文字', 'codex', 'frozen',
                                           model='wrong', route={'model': 'wrong'})
        self.assertFalse(created)
        self.assertEqual(retried['route'], loaded['route'])
        self.assertEqual(len(self.store.get(self.chat)['messages']), 2)

    def test_older_database_schema_upgrades_without_changing_existing_text(self):
        import sqlite3
        run, _ = self.store.submit(self.chat, '旧版用户文字', 'codex', 'old-schema')
        self.store.update_run(run['id'], status='completed', text='旧版回答' * 2000)
        old = Path(self.temp.name) / 'older.sqlite'
        # Remove new metadata columns to reproduce the first prototype schema.
        original = sqlite3.connect(str(self.path))
        try:
            with sqlite3.connect(str(old)) as destination:
                original.backup(destination)
                destination.execute('ALTER TABLE messages DROP COLUMN model')
                destination.execute('ALTER TABLE messages DROP COLUMN provider')
                destination.execute('ALTER TABLE runs DROP COLUMN route')
                destination.execute('ALTER TABLE shared_drafts DROP COLUMN model_id')
                destination.execute('ALTER TABLE messages DROP COLUMN attachment_ids')
                destination.execute('ALTER TABLE shared_drafts DROP COLUMN attachment_ids')
                destination.execute('ALTER TABLE runs DROP COLUMN input_images')
                destination.execute('DROP TABLE chat_revisions')
                destination.execute('DROP TABLE store_meta')
        finally:
            original.close()
        upgraded = ChatStore(old)
        self.assertEqual(upgraded.get(self.chat)['chat']['id'], self.chat)
        messages = upgraded.get(self.chat)['messages']
        self.assertEqual([m['content'] for m in messages], ['旧版用户文字', '旧版回答' * 2000])
        self.assertTrue(all(m['model'] is None and m['provider'] is None for m in messages))
        self.assertEqual(upgraded.changes(self.chat)['selected']['revision'], '0')
        self.assertIsNone(upgraded.run(run['id'])['route'])
        self.assertEqual(upgraded.run(run['id'])['input_images'], [])
        self.assertTrue(all(message['attachment_ids'] == [] for message in messages))

    def test_changes_are_small_atomic_and_stable_for_reads_and_duplicate_writes(self):
        before = self.store.changes(self.chat)
        self.store.get(self.chat)
        self.store.list()
        self.assertEqual(self.store.changes(self.chat), before)
        run, _ = self.store.submit(self.chat, '这是正文，不应出现在变化接口', 'codex', 'change')
        submitted = self.store.changes(self.chat)
        self.assertGreater(int(submitted['revision']), int(before['revision']))
        self.assertTrue(submitted['selected']['running'])
        self.assertNotIn('正文', str(submitted))
        self.assertLess(len(str(submitted)), 256)
        self.store.submit(self.chat, '相同提交', 'codex', 'change')
        self.assertEqual(self.store.changes(self.chat), submitted)
        other = self.store.create('别的对话')['id']
        other_change = self.store.changes(self.chat)
        self.assertGreater(int(other_change['revision']), int(submitted['revision']))
        self.assertEqual(other_change['selected'], submitted['selected'])
        self.store.update_run(run['id'], status='completed', text='完成')
        completed = self.store.changes(self.chat)
        self.assertFalse(completed['selected']['running'])
        self.assertEqual(completed['selected']['revision'], self.store.get(self.chat)['revision'])
        self.store.update_run(run['id'], text='迟到回调')
        self.assertEqual(self.store.changes(self.chat), completed)
        pending, _ = self.store.submit(other, '不确定结果', 'hermes', 'uncertain')
        old_revision = self.store.changes(other)['selected']['revision']
        self.store.recover_interrupted()
        recovered = self.store.changes(other)
        self.assertGreater(int(recovered['selected']['revision']), int(old_revision))
        self.assertFalse(recovered['selected']['running'])
        self.assertEqual(self.store.run(pending['id'])['status'], 'interrupted')
        self.assertIsNone(self.store.changes('missing')['selected'])

    def test_revision_tables_do_not_break_legacy_program_inserts_on_rollback(self):
        import sqlite3
        with sqlite3.connect(self.path) as db:
            self.assertEqual(len(list(db.execute('PRAGMA table_info(chats)'))), 6)
            db.execute('INSERT INTO chats VALUES(?,?,?,?,?,?)',
                       ('legacy-chat', '旧程序新建', '2026-01-01', '2026-01-01', None, None))
        restarted = ChatStore(self.path)
        self.assertEqual(restarted.get('legacy-chat')['chat']['title'], '旧程序新建')
        self.assertEqual(restarted.changes('legacy-chat')['selected']['revision'], '0')
        run, _ = restarted.submit('legacy-chat', '升级后继续', 'codex', 'legacy-next')
        self.assertTrue(restarted.changes('legacy-chat')['selected']['running'])
        restarted.update_run(run['id'], status='completed', text='完成')


class ContextPinStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='one-ai-context-pins-')
        self.path = Path(self.temp.name) / 'chat.sqlite'
        self.store = ChatStore(self.path)
        self.chat = self.store.create('合成固定测试', imported=[
            {'role': 'user' if index % 2 == 0 else 'assistant', 'content': '合成正文-' + str(index)}
            for index in range(22)])['id']
        self.messages = self.store.get(self.chat)['messages']

    def tearDown(self):
        self.temp.cleanup()

    def pin(self, index, revision, pinned=True):
        return self.store.set_context_pin(self.chat, self.messages[index]['id'], pinned, revision)

    def test_read_initial_selection_is_pure_and_upgrade_is_backward_compatible(self):
        before = self.path.read_bytes()
        self.assertEqual(self.store.get_context_selection(self.chat), {'revision': 0, 'pinned_ids': []})
        self.assertEqual(self.store.get(self.chat)['context_selection'], {'revision': 0, 'pinned_ids': []})
        self.assertEqual(self.path.read_bytes(), before)
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TABLE context_selection')
            db.execute('ALTER TABLE runs DROP COLUMN context_pins')
        upgraded = ChatStore(self.path)
        self.assertEqual(upgraded.get(self.chat)['messages'], self.messages)
        self.assertEqual(upgraded.get_context_selection(self.chat), {'revision': 0, 'pinned_ids': []})
        run, _ = upgraded.submit(self.chat, '升级后的本轮', 'codex', 'after-upgrade')
        self.assertEqual(run['context_pins'], [])

    def test_pin_unpin_changes_observable_revision_without_touching_transcript_or_time(self):
        before = self.store.get(self.chat)
        changes = self.store.changes(self.chat)
        pinned = self.pin(4, 0)
        self.assertEqual(pinned, {'revision': 1, 'pinned_ids': [self.messages[4]['id']]})
        after = self.store.get(self.chat)
        self.assertEqual(after['messages'], before['messages'])
        self.assertEqual(after['chat']['updated_at'], before['chat']['updated_at'])
        self.assertEqual(after['chat']['title'], before['chat']['title'])
        self.assertGreater(int(after['revision']), int(changes['revision']))
        self.assertEqual(self.pin(4, 1, False), {'revision': 2, 'pinned_ids': []})
        self.assertEqual(ChatStore(self.path).get_context_selection(self.chat), {'revision': 2, 'pinned_ids': []})

    def test_same_state_is_idempotent_but_stale_revision_always_conflicts(self):
        saved = self.pin(0, 0)
        before = self.path.read_bytes()
        self.assertEqual(self.pin(0, 1), saved)
        self.assertEqual(self.pin(1, 1, False), saved)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(ContextSelectionConflict) as error:
            self.pin(0, 0)
        self.assertEqual(error.exception.context_selection, saved)

    def test_first_pin_is_atomic_across_independent_stores(self):
        other = ChatStore(self.path)
        barrier = threading.Barrier(2)
        def save(store, index):
            barrier.wait()
            try:
                return 'saved', store.set_context_pin(self.chat, self.messages[index]['id'], True, 0)
            except ContextSelectionConflict as error:
                return 'conflict', error.context_selection
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(save, self.store, 0), pool.submit(save, other, 1)
            values = [first.result(), second.result()]
        self.assertEqual(sorted(value[0] for value in values), ['conflict', 'saved'])
        self.assertEqual(values[0][1], values[1][1])
        self.assertEqual(self.store.get_context_selection(self.chat)['revision'], 1)

    def test_twenty_limit_message_scope_and_invalid_states(self):
        for index in range(20):
            self.pin(index, index)
        with self.assertRaisesRegex(ValueError, '20'):
            self.pin(20, 20)
        self.pin(0, 20, False)
        self.assertEqual(len(self.pin(20, 21)['pinned_ids']), 20)
        other = self.store.create(imported=[{'role': 'user', 'content': '另一段'}])['id']
        foreign = self.store.get(other)['messages'][0]['id']
        with self.assertRaises(ValueError):
            self.store.set_context_pin(self.chat, foreign, True, 22)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE messages SET status='failed' WHERE id=?", (self.messages[21]['id'],))
        with self.assertRaises(ValueError):
            self.pin(21, 22)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE messages SET status='completed',content='  ' WHERE id=?", (self.messages[21]['id'],))
        with self.assertRaises(ValueError):
            self.pin(21, 22)

    def test_revisions_and_pin_state_require_exact_json_types(self):
        before = self.path.read_bytes()
        for revision in (True, False, -1, 0.0, '0', None, 2**53):
            with self.assertRaises(ValueError):
                self.pin(0, revision)
        for pinned in (0, 1, 'true', None):
            with self.assertRaises(ValueError):
                self.pin(0, 0, pinned)
        for message_id in ('', 'a' * 257, 'bad\x00', None):
            with self.assertRaises(ValueError):
                self.store.set_context_pin(self.chat, message_id, True, 0)
        with self.assertRaises(KeyError):
            self.store.set_context_pin('missing', self.messages[0]['id'], True, 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_run_freezes_pins_and_old_duplicate_does_not_revalidate(self):
        self.pin(3, 0)
        validated = []
        def validator(messages, pins):
            # Transactional validation receives exactly the same historical
            # projection as preview/worker preparation, including parsed origin.
            self.assertEqual(messages[:-1], self.messages)
            self.assertIsInstance(messages[0]['provenance'], dict)
            validated.append((messages[-1]['content'], list(pins)))
        run, created = self.store.submit(self.chat, '首次问题', 'hermes', 'frozen', context_validator=validator)
        self.assertTrue(created)
        self.assertEqual(run['context_pins'], [self.messages[3]['id']])
        self.pin(3, 1, False)
        old, created = self.store.submit(self.chat, '不应重发', 'hermes', 'frozen', context_validator=validator)
        self.assertFalse(created)
        self.assertEqual(old['context_pins'], run['context_pins'])
        self.assertEqual(validated, [('首次问题', [self.messages[3]['id']])])
        self.assertEqual(ChatStore(self.path).run(run['id'])['context_pins'], run['context_pins'])

    def test_failed_validator_rolls_back_ordinary_and_shared_creation_completely(self):
        def validator(messages, pins):
            raise ValueError('合成容量溢出')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, '容量'):
            self.store.submit(self.chat, '问题', 'codex', 'overflow', context_validator=validator)
        self.assertEqual(self.path.read_bytes(), before)

class ImageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='one-ai-images-synthetic-')
        self.path = Path(self.temp.name) / 'chat.sqlite'
        self.store = ChatStore(self.path)
        self.chat = self.store.create()['id']
        self.a, self.b = ['img_' + format(i, '032x') for i in (1, 2)]

    def tearDown(self):
        self.temp.cleanup()

    def validator(self, messages, pins):
        from rabbit_preparation.context import build_context
        metadata = {item: {'mime': 'image/png', 'status': 'ready', 'size_bytes': 1000,
                           'width': 100, 'height': 100} for item in (self.a, self.b)}
        _, info = build_context(messages, pinned_ids=pins, attachment_metadata=metadata)
        return {'context_info': info,
                'input_images': [{'id': item, 'sha256': format(i + 1, '064x')}
                                 for i, item in enumerate(info['image_input_order'])]}

    def test_image_only_round_is_durable_and_receipt_precedes_invalid_retry(self):
        run, created = self.store.submit(self.chat, '', 'codex', 'image-only', attachment_ids=[self.a],
                                         context_validator=self.validator)
        self.assertTrue(created)
        self.assertEqual(run['input_images'], [{'id': self.a, 'sha256': format(1, '064x')}])
        state = self.store.get(self.chat)
        self.assertEqual(state['messages'][0]['content'], '')
        self.assertEqual(state['messages'][0]['attachment_ids'], [self.a])
        self.assertEqual(state['messages'][1]['attachment_ids'], [])
        self.assertEqual(state['chat']['title'], '图片对话')
        self.assertEqual(ChatStore(self.path).run(run['id'])['input_images'], run['input_images'])
        revision = self.store.changes(self.chat)
        retried, created = self.store.submit(self.chat, None, 'hermes', 'image-only', attachment_ids=['invalid'],
                                            context_validator=lambda *args: self.fail('receipt must not revalidate'))
        self.assertFalse(created)
        self.assertEqual(retried, run)
        self.assertEqual(self.store.changes(self.chat), revision)
        self.assertEqual(len(self.store.get(self.chat)['messages']), 2)

    def test_next_agent_receives_previous_selected_images_and_json_is_decoded(self):
        seen = []
        def validator(messages, pins):
            seen.append(messages)
            return self.validator(messages, pins)
        first, _ = self.store.submit(self.chat, '看图', 'codex', 'first-image', attachment_ids=[self.b, self.a],
                                     context_validator=validator)
        self.store.update_run(first['id'], status='completed', text='第一轮回答')
        second, _ = self.store.submit(self.chat, '换 Hermes 继续看刚才的图', 'hermes', 'second-image',
                                      context_validator=validator)
        self.assertEqual([item['id'] for item in second['input_images']], [self.b, self.a])
        self.assertEqual(seen[-1][0]['attachment_ids'], [self.b, self.a])
        self.assertEqual(seen[-1][-1]['attachment_ids'], [])
        self.assertTrue(seen[-1][-1]['is_draft'])
        self.assertEqual(second['context_info']['previous_images'], 2)
        self.assertEqual(second['context_info']['draft_images'], 0)
        self.assertEqual(second['backend'], 'hermes')

    def test_validator_snapshot_is_copied_and_invalid_result_rolls_back(self):
        result = {'context_info': {'can_send': True, 'image_input_order': [self.a]},
                  'input_images': [{'id': self.a, 'sha256': 'a' * 64}]}
        run, _ = self.store.submit(self.chat, '说明', 'codex', 'snapshot', attachment_ids=[self.a],
                                   context_validator=lambda *args: result)
        result['input_images'][0]['sha256'] = 'b' * 64
        result['context_info']['image_input_order'].clear()
        self.assertEqual(self.store.run(run['id'])['input_images'][0]['sha256'], 'a' * 64)
        self.assertEqual(self.store.run(run['id'])['context_info']['image_input_order'], [self.a])
        self.store.update_run(run['id'], status='completed', text='回答')
        for invalid in (False, {}, {'context_info': {} , 'input_images': [{'id': self.a, 'sha256': 'invalid'}]},
                        {'context_info': {}, 'input_images': [{'id': self.a, 'sha256': 'a' * 64, 'path': '/synthetic/private'}]}):
            before = self.store.get(self.chat)
            with self.subTest(result_type=type(invalid).__name__), self.assertRaises(ValueError):
                self.store.submit(self.chat, '不应创建半条消息', 'hermes', str(uuid.uuid4()), attachment_ids=[self.a],
                                   context_validator=lambda *args: invalid)
            self.assertEqual(self.store.get(self.chat), before)

    def test_attachment_reference_validation_and_legacy_none_validator(self):
        self.assertEqual(self.store.validate_attachment_ids(None), [])
        self.assertEqual(ChatStore.validate_attachment_ids([self.a]), [self.a])
        for invalid in ('bad', {}, [self.a, self.a], ['img_' + 'A' * 32], ['img_' + '0' * 31],
                        ['img_' + format(i, '032x') for i in range(5)], [None]):
            before = self.store.get(self.chat)
            with self.subTest(reference_type=type(invalid).__name__), self.assertRaises(ValueError):
                self.store.submit(self.chat, '测试', 'codex', str(uuid.uuid4()), attachment_ids=invalid)
            self.assertEqual(self.store.get(self.chat), before)
        run, _ = self.store.submit(self.chat, '旧文本调用', 'codex', 'legacy', context_validator=lambda *args: None)
        self.assertEqual(run['input_images'], [])
        self.assertIsNone(run['context_info'])
        self.assertTrue(all(message['attachment_ids'] == [] for message in self.store.get(self.chat)['messages']))

    def test_import_and_source_tail_keep_attachment_identity_and_allow_image_only_pin(self):
        source = {'agent': 'codex', 'session_id': 'synthetic-images'}
        original = dict(source, role='user', content='', message_id='native-image', attachment_ids=[self.a],
                        timestamp='2026-01-01T00:00:00Z')
        chat = self.store.create('有原图的分支', source=source, imported=[original])
        state = self.store.get(chat['id'])
        self.assertEqual(state['messages'][0]['attachment_ids'], [self.a])
        pin = self.store.set_context_pin(chat['id'], state['messages'][0]['id'], True, 0)
        tail = dict(original, message_id='native-image-2', attachment_ids=[self.b])
        comparison = source_update_comparison(source, state['messages'], [original, tail])
        self.assertEqual(comparison['new_messages'], 1)
        added = self.store.append_source_update(chat['id'], source, [original, tail], comparison['source_version'],
                                               int(self.store.get(chat['id'])['revision']))
        self.assertEqual(added, 1)
        state = self.store.get(chat['id'])
        self.assertEqual([message['attachment_ids'] for message in state['messages']], [[self.a], [self.b]])
        self.assertEqual(source_update_comparison(source, state['messages'], [original, tail])['status'], 'unchanged')
        run, _ = self.store.submit(chat['id'], '继续讨论原图', 'hermes', 'continued', context_validator=self.validator)
        self.assertEqual(run['context_pins'], pin['pinned_ids'])
        self.assertEqual([item['id'] for item in run['input_images']], [self.a, self.b])
        mutated = dict(original, attachment_ids=[self.b])
        self.assertEqual(source_update_comparison(source, state['messages'], [mutated, tail])['status'], 'diverged')

    def test_empty_attachments_keep_the_legacy_text_source_hash(self):
        import hashlib
        source = {'agent': 'codex', 'session_id': 'synthetic-text'}
        message = dict(source, role='user', content='旧版纯文字', message_id='old-text', phase='', attachment_ids=[])
        legacy = [{'role': message['role'], 'content': message['content'], 'phase': '',
                   'provenance': {key: message.get(key) for key in
                                  ('agent', 'session_id', 'message_id', 'timestamp', 'source_path', 'source_line')}}]
        expected = hashlib.sha256(json.dumps(legacy, ensure_ascii=False, sort_keys=True,
                                             separators=(',', ':')).encode('utf-8')).hexdigest()
        self.assertEqual(source_update_comparison(source, [], [message])['source_version'], expected)
        changed = dict(message, attachment_ids=[self.a])
        self.assertNotEqual(source_update_comparison(source, [], [changed])['source_version'], expected)


class NativeImageBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rabbit-native-common-synthetic-')
        self.path = Path(self.temp.name) / 'chat.sqlite'
        self.store = ChatStore(self.path)
        self.source = {'agent': 'codex', 'session_id': 'synthetic-native-images'}
        self.a, self.b = ['img_' + format(i, '032x') for i in (1, 2)]

    def tearDown(self):
        self.temp.cleanup()

    def descriptor(self, image=None, ordinal=0, fingerprint=None):
        import hashlib
        fingerprint = fingerprint or format(ordinal + 1, '064x')
        token = 'natimg_' + hashlib.sha256(json.dumps(['content', ordinal, fingerprint],
                                                    separators=(',', ':')).encode()).hexdigest()
        return {'attachment_ordinal': ordinal, 'content_index': ordinal, 'field': 'content', 'token': token,
                'fingerprint': fingerprint, 'kind': 'input_image', 'mime': 'image/png', 'size_bytes': 100,
                'state': 'embedded', 'reason': '', 'omitted_count': 0, 'attachment_id': image,
                'material_state': 'ready' if image else 'unavailable',
                'material_reason': None if image else 'source_missing'}

    def native(self, identity, text='', descriptors=None, position=1):
        record = dict(self.source, message_id=identity, role='user', content=text, phase='',
                      timestamp='2026-01-01T00:00:00Z', source_path='synthetic-native.jsonl', source_line=position)
        if descriptors is not None:
            record['native_attachments'] = descriptors
            record['attachment_ids'] = list(dict.fromkeys(i['attachment_id'] for i in descriptors if i['material_state'] == 'ready'))
        return record

    def plan(self, chat, indexed):
        from chat_store import source_image_backfill_plan
        view = self.store.get(chat)
        return source_image_backfill_plan(self.source, view['messages'], indexed, chat_revision=view['revision'])

    def apply(self, chat, indexed, plan):
        return self.store.backfill_source_images(chat, self.source, indexed, plan['plan_version'], plan['chat_revision'])

    def original_columns(self, chat):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT ordinal,id,chat_id,role,content,created_at,backend,status,provenance,phase,model,provider '
                              'FROM messages WHERE chat_id=? ORDER BY ordinal', (chat,)).fetchall()

    def test_messages_in_projects_main_and_attached_snapshot_with_anchor_order_and_file_filter(self):
        from unittest.mock import patch
        original = [self.native('t1', '原文字一', position=2), self.native('t2', '原文字二', position=4)]
        chat = self.store.create(source=self.source, imported=original)['id']
        indexed = [self.native('p0', descriptors=[self.descriptor(self.a)], position=1),
            original[0], self.native('p1', descriptors=[self.descriptor(self.b)], position=3), original[1]]
        self.apply(chat, indexed, self.plan(chat, indexed))
        file_id = str(uuid.uuid4())
        frozen = [{'id': file_id, 'sha256': 'a' * 64}]
        read, _ = self.store.submit(chat, '只读文件', 'codex', 'read-projection',
            task_intent={'mode': 'chat', 'artifact_ids': [file_id]}, file_inputs=frozen)
        self.store.update_run(read['id'], status='completed', text='只读回答')
        generate, _ = self.store.submit(chat, '生成文件', 'codex', 'files-projection',
            task_intent={'mode': 'files', 'artifact_ids': [file_id]}, file_inputs=frozen)
        self.store.update_run(generate['id'], status='completed', text='生成回答')
        before = self.store.get(chat)
        class CallerConnection(sqlite3.Connection):
            def commit(self):
                raise AssertionError('Projection committed caller transaction')
            def rollback(self):
                raise AssertionError('Projection rolled back caller transaction')
        results = []
        for schema in ('main', 'common'):
            with self.subTest(schema=schema):
                db = sqlite3.connect(self.path if schema == 'main' else ':memory:', factory=CallerConnection)
                db.row_factory = sqlite3.Row
                try:
                    if schema == 'common':
                        db.execute('ATTACH DATABASE ? AS common', (str(self.path),))
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('UPDATE ' + schema + '.messages SET content=? WHERE id=?',
                        ('仅调用者事务可见的消息', read['user_message_id']))
                    physical_ids = [row[0] for row in db.execute(
                        'SELECT id FROM ' + schema + '.messages WHERE chat_id=? ORDER BY ordinal', (chat,))]
                    with patch('sqlite3.connect', side_effect=AssertionError('Projection opened another connection')):
                        plain = ChatStore.messages_in(db, chat, schema=schema)
                        complete = ChatStore.messages_in(db, chat, schema=schema, include_file_task_inputs=True)
                    self.assertEqual([row['provenance']['message_id'] for row in plain[:4]], ['p0', 't1', 'p1', 't2'])
                    self.assertNotEqual([row['id'] for row in plain], physical_ids)
                    self.assertTrue(any(row.get('source_anchor') for row in plain))
                    by_id = {row['id']: row for row in plain}
                    full_by_id = {row['id']: row for row in complete}
                    self.assertEqual(by_id[read['user_message_id']]['content'], '仅调用者事务可见的消息')
                    self.assertEqual(by_id[read['user_message_id']]['text_file_inputs'], frozen)
                    self.assertNotIn('text_file_inputs', by_id[generate['user_message_id']])
                    self.assertEqual(full_by_id[generate['user_message_id']]['text_file_inputs'], frozen)
                    self.assertNotIn('file_task_run_id', by_id[generate['message_id']])
                    self.assertEqual(full_by_id[generate['message_id']]['file_task_run_id'], generate['id'])
                    self.assertTrue(db.in_transaction)
                    traced = []
                    db.set_trace_callback(traced.append)
                    for invalid_schema in ('temp', 'main.messages', 'common; DROP TABLE messages', None):
                        with self.assertRaises(ValueError):
                            ChatStore.messages_in(db, chat, schema=invalid_schema)
                    self.assertEqual(traced, [])
                    results.append((plain, complete))
                finally:
                    sqlite3.Connection.rollback(db)
                    db.close()
        self.assertEqual(results[0], results[1])
        self.assertFalse(hasattr(ChatStore, '_messages_in'))
        self.assertEqual(self.store.get(chat), before)

    def test_backfill_preserves_all_original_columns_branch_and_pin_and_orders_gaps(self):
        original = [self.native('t1', '原文字一', position=2), self.native('t2', '原文字二', position=5)]
        chat = self.store.create(source=self.source, imported=original)['id']
        run, _ = self.store.submit(chat, '后来共同分支', 'hermes', 'synthetic-branch')
        self.store.update_run(run['id'], status='completed', text='后来回答')
        before = self.store.get(chat)
        pins = self.store.set_context_pin(chat, before['messages'][0]['id'], True, 0)
        baseline = self.original_columns(chat)
        indexed = [self.native('p0', descriptors=[self.descriptor(self.a)], position=1),
                   self.native('t1', '原文字一', [self.descriptor(self.b)], position=2),
                   self.native('p1', descriptors=[self.descriptor(self.a)], position=3),
                   self.native('p2', descriptors=[self.descriptor(self.b)], position=4), original[1]]
        plan = self.plan(chat, indexed)
        self.assertEqual(plan['status'], 'backfillable')
        self.assertIsInstance(plan['chat_revision'], int)
        self.assertEqual(source_update_comparison(self.source, self.store.get(chat)['messages'], indexed)['status'], 'diverged')
        result = self.apply(chat, indexed, plan)
        self.assertEqual(result, {'backfilled_messages': 1, 'backfilled_images': 4, 'inserted_image_messages': 3, 'recovered': False})
        after = self.store.get(chat)
        self.assertEqual([m['provenance']['message_id'] for m in after['messages'][:5]], ['p0', 't1', 'p1', 'p2', 't2'])
        self.assertEqual(after['messages'][5:], before['messages'][2:])
        self.assertEqual(self.original_columns(chat)[:len(baseline)], baseline)
        self.assertEqual(after['context_selection'], pins)
        self.assertEqual(source_update_comparison(self.source, after['messages'], indexed)['status'], 'unchanged')
        self.assertEqual(self.plan(chat, indexed)['status'], 'unchanged')

    def test_material_availability_does_not_change_source_version_and_repair_is_explicit(self):
        missing = self.native('t1', '同源图', [self.descriptor()])
        chat = self.store.create(source=self.source, imported=[missing])['id']
        ready = self.native('t1', '同源图', [self.descriptor(self.a)])
        first_version = source_update_comparison(self.source, [], [missing])['source_version']
        self.assertEqual(source_update_comparison(self.source, [], [ready])['source_version'], first_version)
        plan = self.plan(chat, [ready])
        self.assertEqual(plan['counts']['backfilled_images'], 1)
        self.apply(chat, [ready], plan)
        self.assertEqual(self.plan(chat, [missing])['status'], 'unchanged')
        self.assertEqual(self.store.get(chat)['messages'][0]['attachment_ids'], [self.a])
        changed = self.native('t1', '同源图', [self.descriptor(self.b, fingerprint='f' * 64)])
        self.assertEqual(self.plan(chat, [changed])['status'], 'diverged')

    def test_only_picture_tail_anchors_after_original_but_normal_tail_uses_append(self):
        original = self.native('t1', '原文字')
        chat = self.store.create(source=self.source, imported=[original])['id']
        run, _ = self.store.submit(chat, '共同分支', 'codex', 'branch')
        self.store.update_run(run['id'], status='completed', text='回答')
        picture = self.native('p2', descriptors=[self.descriptor(self.a)], position=2)
        plan = self.plan(chat, [original, picture])
        self.assertEqual(plan['new_image_messages'][0]['source_anchor'], 'after:' + self.store.get(chat)['messages'][0]['id'])
        self.apply(chat, [original, picture], plan)
        self.assertEqual(self.store.get(chat)['messages'][1]['provenance']['message_id'], 'p2')
        other = self.store.create(source=self.source, imported=[original])['id']
        tail = self.native('t3', '新增普通文字', position=3)
        self.assertEqual(self.plan(other, [original, picture, tail])['status'], 'unchanged')
        state = self.store.get(other)
        comparison = source_update_comparison(self.source, state['messages'], [original, picture, tail])
        self.assertEqual(comparison['status'], 'appendable')
        self.assertEqual(self.store.append_source_update(other, self.source, [original, picture, tail],
            comparison['source_version'], int(state['revision'])), 2)

    def test_receipt_recovery_survives_later_changes_without_revalidating_or_overwriting(self):
        original = self.native('t1', '旧原文')
        chat = self.store.create(source=self.source, imported=[original])['id']
        indexed = [self.native('t1', '旧原文', [self.descriptor(self.a)])]
        plan = self.plan(chat, indexed)
        first = self.apply(chat, indexed, plan)
        run, _ = self.store.submit(chat, '后来运行中的分支', 'codex', 'later-branch')
        before = self.store.get(chat)
        recovered = ChatStore(self.path).backfill_source_images(chat, self.source, [],
            plan['plan_version'], plan['chat_revision'], source_guard=lambda: self.fail('old receipt cannot revalidate'))
        self.assertEqual(recovered, dict(first, recovered=True))
        self.assertEqual(self.store.get(chat), before)
        self.assertEqual(self.store.run(run['id'])['status'], 'queued')

    def test_readonly_image_receipt_is_explicit_and_preserves_later_active_branch(self):
        from unittest.mock import patch
        original = self.native('t1', '旧原文')
        chat = self.store.create(source=self.source, imported=[original])['id']
        indexed = [self.native('t1', '旧原文', [self.descriptor(self.a)])]
        plan = self.plan(chat, indexed)
        before = self.store.get(chat)
        self.assertIsNone(self.store.find_source_image_backfill_receipt(chat, plan['plan_version'], plan['chat_revision']))
        self.assertEqual(self.store.get(chat), before)
        result = self.apply(chat, indexed, plan)
        run, _ = self.store.submit(chat, '后来运行中的新分支', 'hermes', 'active-after-readonly-receipt')
        latest, changes = self.store.get(chat), self.store.changes(chat)
        with patch('chat_store.source_image_backfill_plan', side_effect=AssertionError('receipt cannot reread source')):
            recovered = ChatStore(self.path).find_source_image_backfill_receipt(chat, plan['plan_version'], plan['chat_revision'])
        self.assertEqual(recovered, dict(result, recovered=True))
        self.assertEqual(self.store.get(chat), latest)
        self.assertEqual(self.store.changes(chat), changes)
        self.assertEqual(self.store.run(run['id'])['status'], 'queued')
        self.assertIsNone(self.store.find_source_image_backfill_receipt(chat, '0' * 64, plan['chat_revision']))
        with self.assertRaises(KeyError):
            self.store.find_source_image_backfill_receipt(str(uuid.uuid4()), plan['plan_version'], plan['chat_revision'])
        for version, revision in (('invalid', plan['chat_revision']), (plan['plan_version'], True)):
            with self.assertRaises(ValueError):
                self.store.find_source_image_backfill_receipt(chat, version, revision)

    def test_two_stores_one_mutation_stale_plan_active_and_source_guard_rollback(self):
        original = self.native('t1', '旧原文')
        chat = self.store.create(source=self.source, imported=[original])['id']
        indexed = [self.native('t1', '旧原文', [self.descriptor(self.a)])]
        plan = self.plan(chat, indexed)
        other = ChatStore(self.path)
        barrier = threading.Barrier(2)
        def perform(store):
            barrier.wait()
            return store.backfill_source_images(chat, self.source, indexed, plan['plan_version'], plan['chat_revision'])
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(perform, store) for store in (self.store, other)]
            self.assertEqual(sorted(f.result()['recovered'] for f in futures), [False, True])
        self.assertEqual(len(self.store.get(chat)['messages']), 1)
        # A different unresolved repair exercises guard rollback, stale CAS and active refusal.
        second = self.store.create(source=self.source, imported=[original])['id']
        second_plan = self.plan(second, indexed)
        before = self.store.get(second)
        calls = []
        def guard():
            calls.append(1)
            if len(calls) == 2:
                raise SourceUpdateConflict('synthetic external index changed')
        with self.assertRaises(SourceUpdateConflict):
            self.store.backfill_source_images(second, self.source, indexed, second_plan['plan_version'],
                second_plan['chat_revision'], source_guard=guard)
        self.assertEqual(self.store.get(second), before)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM source_image_backfill_receipts WHERE chat_id=?', (second,)).fetchone()[0], 0)
        self.store.set_context_pin(second, before['messages'][0]['id'], True, 0)
        with self.assertRaises(SourceUpdateConflict):
            self.apply(second, indexed, second_plan)
        run, _ = self.store.submit(second, '运行中', 'hermes', 'active')
        fresh = self.plan(second, indexed)
        with self.assertRaises(BusyError):
            self.apply(second, indexed, fresh)

    def test_failed_native_image_is_durable_pinable_and_history_limit_is_separate(self):
        missing = self.native('p1', descriptors=[self.descriptor()])
        chat = self.store.create(source=self.source, imported=[missing])['id']
        message = self.store.get(chat)['messages'][0]
        self.assertEqual(message['attachment_ids'], [])
        self.assertEqual(message['native_attachments'][0]['material_reason'], 'source_missing')
        self.store.set_context_pin(chat, message['id'], True, 0)
        five = [self.descriptor('img_' + format(i + 1, '032x'), ordinal=i) for i in range(5)]
        imported = self.store.create(source=self.source, imported=[self.native('five', descriptors=five)])['id']
        self.assertEqual(len(self.store.get(imported)['messages'][0]['attachment_ids']), 5)
        with self.assertRaises(ValueError):
            self.store.submit(imported, '五张草稿仍拒绝', 'codex', 'five', attachment_ids=[i['attachment_id'] for i in five])


class ProfileSnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        from profiles import AIProfiles, make_profile_snapshot
        self.temp = tempfile.TemporaryDirectory(prefix='rabbit-profile-snapshot-synthetic-', dir='/private/tmp')
        self.path = Path(self.temp.name) / 'chat.sqlite'
        self.store = ChatStore(self.path)
        self.profiles = AIProfiles(self.path, capability_validator=lambda _fields: True)
        self.codex = self.profiles.create('开发', 'local/codex', model_id='local/codex/synthetic-sol', reasoning_effort='ultra')
        self.hermes = self.profiles.create('爱马仕', 'local/hermes')
        self.make_snapshot = make_profile_snapshot
        self.chat = self.store.create()['id']

    def tearDown(self):
        self.temp.cleanup()

    def route(self, profile):
        backend = profile['agent_instance_id'].split('/', 1)[1]
        return {'backend': backend, 'requested_model_id': profile['model_id'],
                'model_id': profile['model_id'] or 'local/' + backend + '/synthetic-resolved-default',
                'reasoning_effort': profile['reasoning_effort'] or 'medium'}

    def submit(self, profile, client_id='profile-request', **changes):
        body = dict(chat_id=self.chat, text='合成配置请求', backend=profile['agent_instance_id'].split('/', 1)[1],
                    client_id=client_id, route=self.route(profile), profile_snapshot=self.make_snapshot(profile))
        body.update(changes)
        return self.store.submit(**body)

    def test_selection_default_readonly_independent_cas_profile_exists_and_active_guard(self):
        from chat_store import ProfileSelectionConflict
        baseline = self.store.get(self.chat)
        original_bytes = self.path.read_bytes()
        self.assertEqual(self.store.get_profile_selection(self.chat), {'revision': 0, 'ai_profile_id': None, 'memory_profile_id': 'none'})
        self.assertEqual(self.path.read_bytes(), original_bytes)
        selected = self.store.set_profile_selection(self.chat, self.codex['id'], revision=0)
        self.assertEqual(selected, {'revision': 1, 'ai_profile_id': self.codex['id'], 'memory_profile_id': 'none'})
        self.assertEqual(self.store.set_profile_selection(self.chat, memory_profile_id='none', revision=1), selected)
        with self.assertRaises(ProfileSelectionConflict) as conflict:
            self.store.set_profile_selection(self.chat, self.hermes['id'], revision=0)
        self.assertEqual(conflict.exception.profile_selection, selected)
        for operation in (lambda: self.store.set_profile_selection(self.chat, str(uuid.uuid4()), revision=1),
                          lambda: self.store.get_profile_selection(str(uuid.uuid4()))):
            with self.assertRaises(KeyError):
                operation()
        before = self.store.get(self.chat)
        with self.assertRaises(ValueError):
            self.store.set_profile_selection(self.chat, memory_profile_id='other-bank', revision=1)
        self.assertEqual(self.store.get(self.chat), before)
        run, _ = self.submit(self.codex, profile_selection_revision=1)
        with self.assertRaises(BusyError):
            self.store.set_profile_selection(self.chat, self.hermes['id'], revision=1)
        self.assertEqual(self.store.get_profile_selection(self.chat), selected)
        self.assertEqual(baseline['messages'], [])

    def test_first_acceptance_freezes_parameters_and_history_does_not_follow_rename(self):
        snapshot = self.make_snapshot(self.codex)
        run, created = self.submit(self.codex, profile_snapshot=snapshot)
        self.assertTrue(created)
        self.assertEqual(run['request_snapshot'], snapshot)
        self.assertEqual(self.store.get_profile_selection(self.chat)['ai_profile_id'], self.codex['id'])
        snapshot['ai_profile']['name'] = '调用方不能改已保存快照'
        self.profiles.update(self.codex['id'], 1, name='后来名称', reasoning_effort='low')
        persisted = self.store.run(run['id'])['request_snapshot']
        self.assertEqual(persisted['ai_profile']['name'], '开发')
        self.assertEqual(persisted['ai_profile']['revision'], 1)
        self.assertEqual(persisted['ai_profile']['reasoning_effort'], 'ultra')
        # The historical map works even if current profile lookup is unavailable.
        from unittest.mock import patch
        with patch('chat_store.AIProfiles.get_in', side_effect=AssertionError('history cannot reread current profile')):
            self.assertEqual(self.store.profile_message_snapshots(self.chat), {run['message_id']: persisted})
        before = self.store.get(self.chat)
        recovered, created = self.store.submit(self.chat, None, 'hermes', 'profile-request',
            profile_snapshot={'invalid': 'old retry'}, route=None, profile_selection_revision=False)
        self.assertFalse(created)
        self.assertEqual(recovered['request_snapshot'], persisted)
        self.assertEqual(self.store.get(self.chat), before)

    def test_ordinary_selected_id_and_revision_require_explicit_choice_first(self):
        from chat_store import ProfileSelectionConflict
        first, _ = self.submit(self.codex)
        self.store.update_run(first['id'], status='completed', text='第一轮')
        before = self.store.get(self.chat)
        for options in ({}, {'profile_selection_revision': 0}):
            with self.assertRaises(ProfileSelectionConflict):
                self.submit(self.codex, 'must-select-version', **options)
        with self.assertRaises(ProfileSelectionConflict):
            self.submit(self.hermes, 'other-profile', profile_selection_revision=1)
        with self.assertRaises(ProfileSelectionConflict):
            self.store.submit(self.chat, '不能降回隐式旧入口', 'codex', 'legacy-selected')
        self.assertEqual(self.store.get(self.chat), before)
        self.store.set_profile_selection(self.chat, self.hermes['id'], revision=1)
        second, _ = self.submit(self.hermes, 'second-profile', profile_selection_revision=2)
        self.assertEqual(second['request_snapshot']['ai_profile']['id'], self.hermes['id'])
        self.assertEqual(second['request_snapshot']['ai_profile']['model_id'], '')
        self.assertEqual(second['route']['model_id'], 'local/hermes/synthetic-resolved-default')

    def test_profile_edit_fake_parameters_wrong_projection_and_context_failure_leave_no_partial_state(self):
        from profiles import ProfileConflict
        before = self.store.get(self.chat)
        snapshot = self.make_snapshot(self.codex)
        fake = json.loads(json.dumps(snapshot))
        fake['ai_profile']['name'] = '伪造名字'
        invalid_routes = [dict(self.route(self.codex), requested_model_id=''),
                          dict(self.route(self.codex), model_id='local/codex/other-model'),
                          dict(self.route(self.codex), reasoning_effort='low')]
        with self.assertRaises(ProfileConflict):
            self.submit(self.codex, profile_snapshot=fake)
        for route in invalid_routes:
            with self.assertRaises(ValueError):
                self.submit(self.codex, route=route)
            self.assertEqual(self.store.get(self.chat), before)
        with self.assertRaises(ValueError):
            self.submit(self.codex, context_validator=lambda *_args: (_ for _ in ()).throw(ValueError('synthetic context error')))
        self.assertEqual(self.store.get(self.chat), before)
        self.profiles.update(self.codex['id'], 1, name='新版本')
        with self.assertRaises(ProfileConflict):
            self.submit(self.codex)
        self.assertEqual(self.store.get(self.chat), before)

    def test_same_database_transaction_serializes_profile_edit_after_acceptance(self):
        entered, update_started, release = threading.Event(), threading.Event(), threading.Event()
        def context_validator(_messages, _pins):
            entered.set()
            if not release.wait(5):
                raise AssertionError('synthetic release timeout')
        def edit():
            update_started.set()
            return self.profiles.update(self.codex['id'], 1, name='事务后的新版本')
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            accepted = pool.submit(self.submit, self.codex, context_validator=context_validator)
            self.assertTrue(entered.wait(5))
            edited = pool.submit(edit)
            self.assertTrue(update_started.wait(5))
            release.set()
            run, _ = accepted.result(timeout=5)
            updated = edited.result(timeout=5)
        self.assertEqual(run['request_snapshot']['ai_profile']['revision'], 1)
        self.assertEqual(run['request_snapshot']['ai_profile']['name'], '开发')
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(self.store.run(run['id'])['request_snapshot'], run['request_snapshot'])

    def test_selection_cas_two_stores_and_model_source_is_nullable_then_only_new_callback_records_it(self):
        from chat_store import ProfileSelectionConflict
        other, barrier = ChatStore(self.path), threading.Barrier(2)
        def select(store, profile):
            barrier.wait()
            try:
                return 'saved', store.set_profile_selection(self.chat, profile['id'], revision=0)
            except ProfileSelectionConflict as error:
                return 'conflict', error.profile_selection
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(select, store, profile) for store, profile in
                       ((self.store, self.codex), (other, self.hermes))]
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(sorted(k for k, _v in results), ['conflict', 'saved'])
        self.assertEqual(results[0][1], results[1][1])
        chosen = self.codex if results[0][1]['ai_profile_id'] == self.codex['id'] else self.hermes
        run, _ = self.submit(chosen, profile_selection_revision=1)
        self.assertIsNone(run['model_source'])
        reply = next(m for m in self.store.get(self.chat)['messages'] if m['id'] == run['message_id'])
        self.assertIsNone(reply['model_source'])
        self.store.update_run(run['id'], status='completed', text='新来源说明', model='synthetic-model',
                              model_source='requested_route')
        self.assertEqual(self.store.run(run['id'])['model_source'], 'requested_route')
        reply = next(m for m in self.store.get(self.chat)['messages'] if m['id'] == run['message_id'])
        self.assertEqual(reply['model_source'], 'requested_route')
        self.store.update_run(run['id'], model_source='adapter_reported')
        self.assertEqual(self.store.run(run['id'])['model_source'], 'requested_route')

    def test_old_rows_and_shared_parameters_are_not_backfilled_or_rewritten_by_migration(self):
        run, _ = self.store.submit(self.chat, '旧入口', 'codex', 'legacy-run')
        self.store.update_run(run['id'], status='completed', text='旧回答', model='old-model')
        draft_id = '00000000-0000-4000-8000-000000005267'
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO shared_drafts(id,revision,text,backend,target,updated_at) VALUES(?,?,?,?,?,?)',
                       (draft_id, 7, '旧草稿', 'hermes', '{"type":"new"}', '2026-01-01T00:00:00Z'))
            db.execute('ALTER TABLE runs DROP COLUMN request_snapshot')
            db.execute('ALTER TABLE runs DROP COLUMN model_source')
            db.execute('ALTER TABLE messages DROP COLUMN model_source')
            db.execute('ALTER TABLE shared_drafts DROP COLUMN profile_snapshot')
            baseline = {table: db.execute('SELECT * FROM ' + table).fetchall() for table in ('runs', 'messages', 'shared_drafts')}
            columns = {table: [row[1] for row in db.execute('PRAGMA table_info(' + table + ')')]
                       for table in baseline}
        reopened = ChatStore(self.path)
        with sqlite3.connect(self.path) as db:
            for table in baseline:
                self.assertEqual(db.execute('SELECT ' + ','.join(columns[table]) + ' FROM ' + table).fetchall(), baseline[table])
            self.assertIsNone(db.execute('SELECT profile_snapshot FROM shared_drafts WHERE id=?',
                                        (draft_id,)).fetchone()[0])
        self.assertIsNone(reopened.run(run['id'])['request_snapshot'])
        self.assertIsNone(reopened.run(run['id'])['model_source'])
        self.assertEqual(reopened.profile_message_snapshots(self.chat), {})


if __name__ == '__main__':
    unittest.main()
