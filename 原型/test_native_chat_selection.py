"""Synthetic app SQLite and pure resolvers only; no native Agent or user data."""
import concurrent.futures
import copy
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid

from native_catalogue import NativeChatCatalogue
from native_chat_selection import (NativeChatSelections, NativeChatSelectionConflict,
                                   NativeChatSelectionCorrupt, assert_selection_in, TABLE)
from native_profile_intents import NativeProfileIntents
from pi_profile_intents import PiProfileIntents
from hermes_profile_intents import HermesProfileIntents
from profiles import AIProfiles, MAX_REVISION, make_profile_snapshot
from rabbit_runtime.native_session_store import request_sha256


AGENTS = ('codex', 'pi', 'hermes')


class NativeChatSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rabbit-native-selection-', dir=Path(tempfile.gettempdir()).resolve())
        self.path = Path(self.temp.name) / 'chat.sqlite'
        self.profiles = AIProfiles(self.path)
        self.workspace = 'a' * 64
        self.store = NativeChatSelections(self.path, workspace_id=self.workspace)
        self.chats, self.saved = {}, {}
        for agent in AGENTS:
            catalogue = NativeChatCatalogue(self.path, agent=agent)
            self.chats[agent] = catalogue.create(str(uuid.uuid4()), '虚构对话', lambda _: None)['id']
            self.saved[agent] = [self.profiles.create('虚构配置 ' + str(i), 'local/' + agent) for i in (1, 2)]

    def tearDown(self):
        self.temp.cleanup()

    def schema(self):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()

    def choose(self, agent, revision, profile=None):
        return self.store.select(agent, self.chats[agent], revision,
            profile['id'] if profile else None, profile['revision'] if profile else None)

    def intent(self, agent):
        cls = {'codex': NativeProfileIntents, 'pi': PiProfileIntents, 'hermes': HermesProfileIntents}[agent]
        return cls(self.path, workspace_id=self.workspace)

    def body(self, agent, profile=None, client=None):
        value = {'client_id': client or str(uuid.uuid4())}
        value.update({'inputs': [{'type': 'text', 'text': '虚构正文不能进选择表'}]} if agent == 'codex'
                     else {'message': '虚构正文不能进选择表'})
        if profile:
            value.update(ai_profile_id=profile['id'], ai_profile_revision=profile['revision'])
        return value

    def resolve(self, agent):
        return copy.deepcopy({'codex': {}, 'pi': {'provider': 'fictional', 'model': 'fictional', 'thinking': 'low'},
            'hermes': {'provider': 'fictional', 'selection_provider': 'fictional', 'model': 'fictional',
                'selection_model': 'fictional', 'reasoning_effort': None, 'reasoning_config': None}}[agent])

    def test_constructor_and_reads_do_not_create_table_or_write_existing_database(self):
        before_schema, before_bytes = self.schema(), self.path.read_bytes()
        store = NativeChatSelections(self.path, workspace_id=self.workspace)
        for agent in AGENTS:
            value = store.read(agent, self.chats[agent])
            self.assertEqual(value, {'workspace_id': self.workspace, 'agent': agent,
                'chat_id': self.chats[agent], 'selection': {'revision': 0, 'ai_profile_id': None,
                    'ai_profile_revision': None}, 'profile': None, 'profile_current': True})
        self.assertEqual(before_schema, self.schema())
        self.assertEqual(before_bytes, self.path.read_bytes())

    def test_explicit_write_adds_only_one_reference_table_without_parameters_or_messages(self):
        before = self.schema()
        profile = self.saved['codex'][0]
        value = self.choose('codex', 0, profile)
        self.assertEqual(value['selection'], {'revision': 1, 'ai_profile_id': profile['id'], 'ai_profile_revision': 1})
        self.assertEqual(value['profile'], make_profile_snapshot(profile)['ai_profile'])
        self.assertTrue(value['profile_current'])
        after = [row for row in self.schema() if row[1] != TABLE and row[1] != 'sqlite_autoindex_' + TABLE + '_1']
        self.assertEqual(before, after)
        with sqlite3.connect(self.path) as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(' + TABLE + ')')]
            self.assertEqual(columns, ['workspace_id', 'agent', 'chat_id', 'revision', 'ai_profile_id', 'ai_profile_revision'])
            raw = repr(db.execute('SELECT * FROM ' + TABLE).fetchall())
            self.assertNotIn(profile['name'], raw)
            self.assertNotIn('虚构正文', raw)

    def test_clear_retains_managed_row_and_first_clear_is_not_legacy_noop(self):
        first = self.choose('codex', 0)
        self.assertEqual(first['selection']['revision'], 1)
        self.assertEqual(self.choose('codex', 1), first)
        chosen = self.choose('codex', 1, self.saved['codex'][0])
        self.assertEqual(chosen['selection']['revision'], 2)
        cleared = self.choose('codex', 2)
        self.assertEqual(cleared['selection'], {'revision': 3, 'ai_profile_id': None, 'ai_profile_revision': None})
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + TABLE).fetchone()[0], 1)
            assert_selection_in(db, self.workspace, 'codex', self.chats['codex'], None, None)
            with self.assertRaises(NativeChatSelectionConflict):
                p = self.saved['codex'][0]
                assert_selection_in(db, self.workspace, 'codex', self.chats['codex'], p['id'], p['revision'])

    def test_reusing_or_copying_a_profile_changes_only_the_named_chat_selection(self):
        for agent in AGENTS:
            with self.subTest(agent=agent):
                shared = self.saved[agent][0]
                other = NativeChatCatalogue(self.path, agent=agent).create(
                    str(uuid.uuid4()), '虚构的另一段对话', lambda _: None)['id']
                self.choose(agent, 0, shared)
                unchanged = self.store.select(agent, other, 0, shared['id'], shared['revision'])
                original = self.profiles.get(shared['id'])
                # Reuse another existing configuration; no profile edit.
                self.choose(agent, 1, self.saved[agent][1])
                self.assertEqual(self.store.read(agent, other), unchanged)
                self.assertEqual(self.profiles.get(shared['id']), original)
                # Copy parameters to an independent configuration, then select
                # it for the target. Another chat's original reference stays live.
                private = self.profiles.copy(shared['id'], shared['revision'], name='仅本段使用')
                self.choose(agent, 2, private)
                self.assertNotEqual(private['id'], shared['id'])
                self.assertEqual(self.store.read(agent, other), unchanged)
                self.assertTrue(self.store.read(agent, other)['profile_current'])
                self.assertEqual(self.profiles.get(shared['id']), original)
                intent = self.intent(agent)
                frozen = intent.freeze(other, self.body(agent, shared), lambda _: self.resolve(agent))
                self.assertEqual(frozen['ai_profile']['id'], shared['id'])

    def test_cas_conflict_returns_current_and_does_not_rewrite_or_follow_new_profile_revision(self):
        profile, other = self.saved['codex']
        first = self.choose('codex', 0, profile)
        with self.assertRaises(NativeChatSelectionConflict) as caught:
            self.choose('codex', 0, other)
        self.assertEqual(caught.exception.current, first)
        self.assertEqual(caught.exception.current_selection, first['selection'])
        updated = self.profiles.update(profile['id'], 1, name='已更新的虚构配置')
        value = self.store.read('codex', self.chats['codex'])
        self.assertFalse(value['profile_current']); self.assertIsNone(value['profile'])
        self.assertEqual(value['selection'], first['selection'])
        with self.assertRaises(NativeChatSelectionConflict):
            self.choose('codex', 1, profile)
        fresh = self.choose('codex', 1, updated)
        self.assertEqual(fresh['selection']['revision'], 2)
        self.assertEqual(fresh['profile']['revision'], 2)

    def test_deleted_profile_is_stale_observation_and_cannot_be_selected_again(self):
        profile = self.saved['pi'][0]
        self.choose('pi', 0, profile)
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM ai_profiles WHERE id=?', (profile['id'],))
        stale = self.store.read('pi', self.chats['pi'])
        self.assertFalse(stale['profile_current']); self.assertIsNone(stale['profile'])
        with self.assertRaises(NativeChatSelectionConflict):
            self.choose('pi', 1, profile)
        self.assertEqual(self.choose('pi', 1)['selection']['revision'], 2)

    def test_cross_agent_profile_and_wrong_chat_are_rejected_without_creating_schema(self):
        before = self.schema()
        with self.assertRaises(ValueError):
            self.choose('codex', 0, self.saved['pi'][0])
        with self.assertRaises(KeyError):
            self.store.select('codex', self.chats['pi'], 0, None, None)
        with self.assertRaises(KeyError):
            self.store.read('claude', self.chats['codex'])
        for agent in ('dsh', 'not-agent'):
            with self.assertRaises(ValueError):
                self.store.read(agent, self.chats['codex'])
        self.assertEqual(before, self.schema())

    def test_separate_workspace_and_agent_namespaces_do_not_claim_each_others_choices(self):
        self.choose('codex', 0, self.saved['codex'][0])
        other = NativeChatSelections(self.path, workspace_id='b' * 64)
        self.assertEqual(other.read('codex', self.chats['codex'])['selection']['revision'], 0)
        self.assertEqual(self.store.read('pi', self.chats['pi'])['selection']['revision'], 0)
        other.select('codex', self.chats['codex'], 0, None, None)
        self.assertEqual(self.store.read('codex', self.chats['codex'])['selection']['revision'], 1)

    def test_validation_rejects_bool_unsafe_revisions_partial_pairs_and_bad_ids(self):
        profile = self.saved['codex'][0]
        for revision in (True, -1, 1.0, str(0), MAX_REVISION + 1):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.choose('codex', revision, profile)
        for pair in ((profile['id'], None), (None, 1), ('bad', 1), (profile['id'], True),
                     (profile['id'], MAX_REVISION + 1)):
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                self.store.select('codex', self.chats['codex'], 0, *pair)
        for identity in ('../bad', 'bad', str(uuid.uuid4()).upper()):
            with self.assertRaises(ValueError):
                self.store.read('codex', identity)
        for workspace in ('bad', 'A' * 64, None):
            with self.assertRaises(ValueError):
                NativeChatSelections(self.path, workspace_id=workspace)

    def test_concurrent_first_selection_has_one_cas_winner(self):
        barrier = threading.Barrier(2)
        def write(profile):
            barrier.wait(timeout=5)
            try:
                return ('ok', self.choose('codex', 0, profile))
            except NativeChatSelectionConflict as error:
                return ('conflict', error.current)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, self.saved['codex']))
        self.assertEqual(sorted(state for state, _ in results), ['conflict', 'ok'])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(self.store.read('codex', self.chats['codex']), results[0][1])

    def test_js_safe_revision_limit_allows_exact_noop_but_cannot_advance(self):
        self.choose('codex', 0)
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE ' + TABLE + ' SET revision=?', (MAX_REVISION,))
        self.assertEqual(self.choose('codex', MAX_REVISION)['selection']['revision'], MAX_REVISION)
        with self.assertRaises(ValueError):
            self.choose('codex', MAX_REVISION, self.saved['codex'][0])

    def test_corrupt_managed_record_fails_closed_for_read_select_and_intent_guard(self):
        self.choose('codex', 0)
        with sqlite3.connect(self.path) as db:
            db.execute('PRAGMA ignore_check_constraints=ON')
            db.execute('UPDATE ' + TABLE + ' SET ai_profile_id=?,ai_profile_revision=NULL', ('bad',))
        with self.assertRaises(ValueError):
            self.store.read('codex', self.chats['codex'])
        with self.assertRaises(ValueError):
            self.choose('codex', 1)
        with sqlite3.connect(self.path) as db, self.assertRaises(ValueError):
            assert_selection_in(db, self.workspace, 'codex', self.chats['codex'], None, None)

    def test_wrong_schema_is_not_misread_as_missing_legacy_table(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE VIEW ' + TABLE + ' AS SELECT 1 AS revision')
        with self.assertRaises(ValueError):
            self.store.read('codex', self.chats['codex'])
        with sqlite3.connect(self.path) as db, self.assertRaises(ValueError):
            assert_selection_in(db, self.workspace, 'codex', self.chats['codex'], None, None)

    def test_incomplete_table_is_corrupt_and_never_recreated_or_repaired_by_reads(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE ' + TABLE + '(revision INTEGER)')
        before = self.schema()
        with self.assertRaises(NativeChatSelectionCorrupt):
            self.store.read('codex', self.chats['codex'])
        with self.assertRaises(NativeChatSelectionCorrupt):
            self.choose('codex', 0)
        self.assertEqual(self.schema(), before)

    def test_exact_profile_decoder_failure_is_corrupt_without_echoing_raw_data(self):
        profile = self.saved['codex'][0]
        self.choose('codex', 0, profile)
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE ai_profiles SET reasoning_effort=? WHERE id=?',
                       ('private-corrupt-value-2613', profile['id']))
        with self.assertRaises(NativeChatSelectionCorrupt) as caught:
            self.store.read('codex', self.chats['codex'])
        self.assertNotIn('private-corrupt-value-2613', str(caught.exception))
        self.assertEqual(self.choose('codex', 1)['selection']['revision'], 2)

    def test_restart_reads_exact_persisted_reference_without_capability_discovery(self):
        selected = self.choose('pi', 0, self.saved['pi'][0])
        self.profiles.capability_validator = lambda _: self.fail('selection never discovers capabilities')
        fresh = NativeChatSelections(self.path, workspace_id=self.workspace)
        self.assertEqual(fresh.read('pi', self.chats['pi']), selected)
        self.assertEqual(fresh.select('pi', self.chats['pi'], 1,
            self.saved['pi'][1]['id'], 1)['selection']['revision'], 2)

    def test_failed_first_selection_rolls_back_schema_with_its_failed_transaction(self):
        before = self.schema()
        for expected, profile in ((1, None), (0, {'id': str(uuid.uuid4()), 'revision': 1})):
            with self.assertRaises(NativeChatSelectionConflict):
                self.choose('codex', expected, profile)
            self.assertEqual(self.schema(), before)

    def test_three_intents_keep_legacy_behavior_when_selection_table_is_absent(self):
        for agent in AGENTS:
            with self.subTest(agent=agent):
                intents = self.intent(agent)
                body = self.body(agent, self.saved[agent][0])
                value = intents.freeze(self.chats[agent], body, lambda _: self.resolve(agent))
                self.assertEqual(value['request_sha256'], request_sha256(body))
                self.assertEqual(value['ai_profile']['id'], self.saved[agent][0]['id'])
        self.assertNotIn(TABLE, {row[1] for row in self.schema()})

    def test_three_intents_reject_stale_browser_selection_before_resolver_and_preserve_raw_hash(self):
        for agent in AGENTS:
            with self.subTest(agent=agent):
                intents = self.intent(agent)
                profile, other = self.saved[agent]
                self.choose(agent, 0, profile)
                bad = self.body(agent, other)
                with self.assertRaises(NativeChatSelectionConflict):
                    intents.freeze(self.chats[agent], bad, lambda _: self.fail('guard must precede discovery'))
                body = self.body(agent, profile)
                first = intents.freeze(self.chats[agent], body, lambda _: self.resolve(agent))
                self.assertEqual(first['request_sha256'], request_sha256(body))
                self.assertEqual(first['ai_profile'], make_profile_snapshot(profile)['ai_profile'])

    def test_three_intents_old_frozen_request_replays_after_selection_change_and_clear(self):
        for agent in AGENTS:
            with self.subTest(agent=agent):
                intents = self.intent(agent)
                profile, other = self.saved[agent]
                self.choose(agent, 0, profile)
                body = self.body(agent, profile)
                original = intents.freeze(self.chats[agent], body, lambda _: self.resolve(agent))
                self.choose(agent, 1, other)
                self.assertEqual(intents.freeze(self.chats[agent], body, None), original)
                self.choose(agent, 2)
                self.assertEqual(intents.freeze(self.chats[agent], body, None), original)
                with self.assertRaises(NativeChatSelectionConflict):
                    intents.freeze(self.chats[agent], self.body(agent, profile), None)
                cleared = self.body(agent)
                result = intents.freeze(self.chats[agent], cleared, lambda _: self.resolve(agent))
                self.assertIsNone(result['ai_profile'])

    def test_frozen_replay_is_readable_even_when_later_selection_metadata_is_corrupt(self):
        originals = []
        for agent in AGENTS:
            intents, profile = self.intent(agent), self.saved[agent][0]
            self.choose(agent, 0, profile)
            body = self.body(agent, profile)
            original = intents.freeze(self.chats[agent], body, lambda _: self.resolve(agent))
            originals.append((agent, intents, body, original))
        with sqlite3.connect(self.path) as db:
            db.execute('PRAGMA ignore_check_constraints=ON')
            db.execute('UPDATE ' + TABLE + ' SET revision=-1')
        for agent, intents, body, original in originals:
            with self.subTest(agent=agent):
                self.assertEqual(intents.freeze(self.chats[agent], body, None), original)
                with self.assertRaises(NativeChatSelectionCorrupt):
                    intents.freeze(self.chats[agent], self.body(agent, self.saved[agent][0]), None)

    def test_three_intents_final_write_guard_rejects_selection_changed_during_resolver(self):
        for agent in AGENTS:
            with self.subTest(agent=agent):
                intents = self.intent(agent)
                profile, other = self.saved[agent]
                self.choose(agent, 0, profile)
                started, release = threading.Event(), threading.Event()
                def resolve(_profile):
                    started.set()
                    if not release.wait(timeout=5):
                        raise AssertionError('test resolver was not released')
                    return self.resolve(agent)
                body = self.body(agent, profile)
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(intents.freeze, self.chats[agent], body, resolve)
                    try:
                        self.assertTrue(started.wait(timeout=5))
                        self.choose(agent, 1, other)
                    finally:
                        release.set()
                    with self.assertRaises(NativeChatSelectionConflict):
                        future.result(timeout=5)
                self.assertIsNone(intents.lookup(self.chats[agent], body))

    def test_managed_aba_with_same_exact_reference_retains_semantic_send_identity(self):
        for agent in AGENTS:
            with self.subTest(agent=agent):
                intents = self.intent(agent)
                profile, other = self.saved[agent]
                self.choose(agent, 0, profile)
                self.choose(agent, 1, other)
                self.choose(agent, 2, profile)
                body = self.body(agent, profile)
                value = intents.freeze(self.chats[agent], body, lambda _: self.resolve(agent))
                self.assertEqual(value['request_sha256'], request_sha256(body))
                with self.assertRaises(NativeChatSelectionConflict):
                    self.choose(agent, 1)


if __name__ == '__main__':
    unittest.main()
