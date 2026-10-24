"""Temporary private DBs only; no real models, credentials or source records."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from profiles import (AIProfiles, ProfileConflict, DefaultProfileConflict, MAX_REVISION, validate_profile_fields,
                      validate_profile_id, validate_profile_revision, make_profile_snapshot,
                      validate_request_snapshot, initialize_schema)


class AIProfilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rabbit-profiles-synthetic-', dir='/private/tmp')
        self.path = Path(self.temp.name) / 'chat.sqlite'
        with sqlite3.connect(self.path) as db:
            db.executescript('CREATE TABLE chats(id TEXT,content TEXT);'
                             'CREATE TABLE runs(id TEXT,status TEXT);'
                             'CREATE TABLE store_meta(key TEXT,value INTEGER);')
            db.execute('INSERT INTO chats VALUES(?,?)', ('synthetic-chat', '原有共同聊天不改'))
            db.execute('INSERT INTO runs VALUES(?,?)', ('synthetic-run', 'queued'))
            db.execute('INSERT INTO store_meta VALUES(?,?)', ('revision', 7))
        self.seen = []
        def validator(fields):
            self.seen.append(dict(fields))
            allowed = {'local/codex': {'', 'local/codex/synthetic-sol'},
                       'local/hermes': {'', 'local/hermes/synthetic-flash'}}
            if fields['model_id'] not in allowed[fields['agent_instance_id']]:
                raise ValueError('synthetic exact model unavailable')
            efforts = {'local/codex': {None, 'low', 'ultra'}, 'local/hermes': {None}}
            if fields['reasoning_effort'] not in efforts[fields['agent_instance_id']]:
                raise ValueError('synthetic exact effort unsupported')
        self.validator = validator
        self.profiles = AIProfiles(self.path, capability_validator=validator)

    def tearDown(self):
        self.temp.cleanup()

    def other_tables(self):
        with sqlite3.connect(self.path) as db:
            return {table: db.execute('SELECT * FROM ' + table).fetchall()
                    for table in ('chats', 'runs', 'store_meta')}

    def create(self, **changes):
        fields = dict(name='开发', agent_instance_id='local/codex', model_id='local/codex/synthetic-sol',
                      reasoning_effort='ultra')
        fields.update(changes)
        return self.profiles.create(**fields)

    def test_get_in_uses_callers_uncommitted_row_transaction_without_connection_or_commit(self):
        profile = self.create()
        class CallerConnection(sqlite3.Connection):
            def commit(self):
                raise AssertionError('Reader committed the caller transaction')
            def rollback(self):
                raise AssertionError('Reader rolled back the caller transaction')
        db = sqlite3.connect(self.path, factory=CallerConnection)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE ai_profiles SET name=? WHERE id=?', ('未提交的调用者名称', profile['id']))
            with patch('sqlite3.connect', side_effect=AssertionError('Reader opened another connection')):
                selected = AIProfiles.get_in(db, profile['id'])
            self.assertEqual(selected, dict(profile, name='未提交的调用者名称'))
            self.assertTrue(db.in_transaction)
            self.assertFalse(hasattr(AIProfiles, '_get_in'))
        finally:
            sqlite3.Connection.rollback(db)
            db.close()
        self.assertEqual(self.profiles.get(profile['id']), profile)

    def test_initialization_only_adds_profile_table_and_never_migrates_other_tables(self):
        before = self.other_tables()
        AIProfiles(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                             {'chats', 'runs', 'store_meta', 'ai_profiles', 'ai_profile_migrations', 'profile_settings', 'ai_profile_creation_receipts'})
            self.assertEqual([row[1] for row in db.execute('PRAGMA table_info(chats)')], ['id', 'content'])
            self.assertEqual([row[1] for row in db.execute('PRAGMA table_info(runs)')], ['id', 'status'])
            self.assertEqual([row[1] for row in db.execute('PRAGMA table_info(store_meta)')], ['key', 'value'])
        self.assertEqual(self.other_tables(), before)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_create_stable_uuid_duplicate_names_are_independent_and_read_is_not_write(self):
        before = self.other_tables()
        first, second = self.create(), self.create()
        self.assertEqual(validate_profile_id(first['id']), first['id'])
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(first['revision'], 1)
        self.assertEqual(first['created_at'], first['updated_at'])
        self.assertTrue(first['created_at'].endswith('Z'))
        self.assertEqual(len(self.seen), 2)
        original_bytes = self.path.read_bytes()
        original_stat = self.path.stat()
        with patch.object(self.profiles, '_capabilities', side_effect=AssertionError('reads cannot validate/write')):
            self.assertEqual(self.profiles.get(first['id']), first)
            self.assertEqual(len(self.profiles.list()['profiles']), 2)
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertEqual(self.path.stat().st_mtime_ns, original_stat.st_mtime_ns)
        self.assertEqual(self.other_tables(), before)

    def test_rename_and_parameter_edit_keep_id_and_original_creation_time(self):
        first = self.create()
        second = self.profiles.update(first['id'], 1, name='  写作  ')
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(second['revision'], 2)
        self.assertEqual(second['created_at'], first['created_at'])
        self.assertEqual(second['name'], '写作')
        self.assertEqual(second['model_id'], first['model_id'])
        third = self.profiles.update(first['id'], 2, agent_instance_id='local/hermes',
                                    model_id='local/hermes/synthetic-flash', reasoning_effort=None)
        self.assertEqual(third['revision'], 3)
        self.assertEqual(third['reasoning_effort'], None)
        self.assertEqual(third['id'], first['id'])
        before = self.path.read_bytes()
        self.assertEqual(self.profiles.update(third['id'], 3), third)
        self.assertEqual(self.path.read_bytes(), before)

    def test_stale_update_and_copy_conflict_leave_original_and_have_fresh_public_profile(self):
        first = self.create()
        current = self.profiles.update(first['id'], 1, name='新名称')
        before = self.path.read_bytes()
        checks = len(self.seen)
        for action in (lambda: self.profiles.update(first['id'], 1, name='错误覆盖'),
                       lambda: self.profiles.copy(first['id'], 1)):
            with self.assertRaises(ProfileConflict) as caught:
                action()
            self.assertEqual(caught.exception.profile, current)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.seen), checks)
        self.assertEqual(len(self.profiles.list()['profiles']), 1)

    def test_copy_parameters_gets_new_id_revision_and_can_override_without_editing_original(self):
        original = self.create()
        cloned = self.profiles.copy(original['id'], 1)
        self.assertNotEqual(original['id'], cloned['id'])
        self.assertEqual(cloned['revision'], 1)
        self.assertEqual(cloned['name'], '开发 副本')
        for key in ('agent_instance_id', 'device_id', 'model_id', 'reasoning_effort'):
            self.assertEqual(cloned[key], original[key])
        different = self.profiles.copy(original['id'], 1, name='爱马仕', agent_instance_id='local/hermes',
                                       model_id='local/hermes/synthetic-flash', reasoning_effort=None)
        self.assertEqual(different['agent_instance_id'], 'local/hermes')
        self.assertEqual(self.profiles.get(original['id']), original)
        self.assertEqual(len(self.profiles.list()['profiles']), 3)

    def test_exact_capability_rejection_rolls_back_create_update_copy_and_ignores_enum_membership(self):
        original = self.create()
        before = self.path.read_bytes()
        actions = (lambda: self.create(model_id='local/codex/missing-model'),
                   lambda: self.create(reasoning_effort='max'),
                   lambda: self.profiles.update(original['id'], 1, reasoning_effort='max'),
                   lambda: self.profiles.copy(original['id'], 1, reasoning_effort='max'))
        for action in actions:
            with self.assertRaises(ValueError):
                action()
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.profiles.get(original['id']), original)
        # Dynamic rejection is supplied by the caller, even for a former valid choice.
        def rejected(_fields):
            return False
        with self.assertRaises(ValueError):
            self.profiles.update(original['id'], 1, name='不能偷偷退回默认', validator=rejected)
        self.assertEqual(self.path.read_bytes(), before)

    def test_default_semantics_and_missing_or_malformed_validator_never_claim_explicit_support(self):
        independent = AIProfiles(self.path)
        default = independent.create('旧默认', 'local/hermes')
        self.assertEqual(default['model_id'], '')
        self.assertIsNone(default['reasoning_effort'])
        before = self.path.read_bytes()
        for fields in ({'model_id': 'local/hermes/synthetic-flash'}, {'reasoning_effort': 'low'}):
            with self.assertRaises(ValueError):
                independent.create('待核对', 'local/hermes', **fields)
            self.assertEqual(self.path.read_bytes(), before)
        for outcome in (False, {}, 'supported', 1):
            with self.assertRaises(ValueError):
                independent.create('错误校验', 'local/codex', validator=lambda _f: outcome)
            self.assertEqual(self.path.read_bytes(), before)
        accepted = independent.create('已核对', 'local/codex', model_id='local/codex/synthetic-sol',
                                      reasoning_effort='ultra', validator=lambda _f: True)
        self.assertEqual(accepted['reasoning_effort'], 'ultra')

    def test_callback_cannot_rewrite_saved_fields_or_store_route_credentials(self):
        def mutating_validator(fields):
            fields['name'] = 'callback changed'
            fields['api_key'] = 'synthetic-private-never-store'
            return None
        profile = self.profiles.create('原名', 'local/codex', validator=mutating_validator)
        self.assertEqual(profile['name'], '原名')
        self.assertNotIn('api_key', profile)
        self.assertNotIn(b'synthetic-private-never-store', self.path.read_bytes())
        # Future unrelated DB columns must not be accidentally exposed by SELECT *.
        with sqlite3.connect(self.path) as db:
            db.execute('ALTER TABLE ai_profiles ADD COLUMN private_fixture TEXT')
            db.execute('UPDATE ai_profiles SET private_fixture=?', ('private value',))
        self.assertNotIn('private_fixture', self.profiles.get(profile['id']))
        self.assertNotIn('private_fixture', self.profiles.list()['profiles'][0])

    def test_two_instances_one_cas_winner_with_complete_conflict(self):
        original = self.create()
        other = AIProfiles(self.path, capability_validator=self.validator)
        barrier = threading.Barrier(2)
        def update(service, name):
            barrier.wait()
            try:
                return 'saved', service.update(original['id'], 1, name=name)
            except ProfileConflict as error:
                return 'conflict', error.profile
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(update, service, name) for service, name in
                       ((self.profiles, '入口一'), (other, '入口二'))]
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(sorted(kind for kind, _ in results), ['conflict', 'saved'])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(self.profiles.get(original['id'])['revision'], 2)

    def test_global_field_bounds_cross_agent_model_and_no_arbitrary_devices(self):
        valid = dict(name='基本参数', agent_instance_id='local/codex')
        invalids = ({'name': ''}, {'name': ' '}, {'name': '字' * 41}, {'name': 'bad\nname'},
                    {'name': '\x00secret'}, {'agent_instance_id': 'cloud/codex'},
                    {'agent_instance_id': 'local/unregistered'}, {'device_id': 'remote/windows'},
                    {'model_id': 'gpt-raw-model'}, {'model_id': 'local/hermes/synthetic-flash'},
                    {'model_id': None}, {'reasoning_effort': 'auto'}, {'reasoning_effort': True})
        before = self.path.read_bytes()
        for mutation in invalids:
            with self.subTest(fields=mutation), self.assertRaises(ValueError):
                self.profiles.create(**dict(valid, **mutation))
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(validate_profile_fields('字' * 40, 'local/codex')['name'], '字' * 40)

    def test_revision_uuid_not_found_and_version_max_fail_without_changes(self):
        original = self.create()
        for value in (False, 0, -1, '1', 1.5, MAX_REVISION + 1):
            with self.assertRaises(ValueError):
                validate_profile_revision(value)
        for value in ('not-id', original['id'].upper(), None):
            with self.assertRaises(ValueError):
                validate_profile_id(value)
        with self.assertRaises(KeyError):
            self.profiles.get(str(uuid.uuid4()))
        with self.assertRaises(KeyError):
            self.profiles.update(str(uuid.uuid4()), 1, name='不存在')
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE ai_profiles SET revision=? WHERE id=?', (MAX_REVISION, original['id']))
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.profiles.update(original['id'], MAX_REVISION, name='不能溢出')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.profiles.copy(original['id'], MAX_REVISION)['revision'], 1)

    def test_symlink_db_and_parent_and_nonordinary_file_are_refused(self):
        alias = Path(self.temp.name) / 'alias.sqlite'
        alias.symlink_to(self.path)
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            AIProfiles(alias)
        folder = Path(self.temp.name) / 'linked-parent'
        folder.symlink_to(self.path.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            AIProfiles(folder / 'chat.sqlite')
        with self.assertRaises(ValueError):
            AIProfiles(self.path.parent)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_snapshot_is_strict_intent_only_without_secrets_devices_memory_or_actual_claims(self):
        original = self.create()
        snapshot = make_profile_snapshot(dict(original, private_secret='never copy'))
        self.assertEqual(set(snapshot), {'schema_version', 'ai_profile', 'memory_profile', 'used_memory_references'})
        self.assertEqual(snapshot['ai_profile']['id'], original['id'])
        self.assertEqual(snapshot['memory_profile'], {'id': 'none'})
        self.assertEqual(snapshot['used_memory_references'], [])
        self.assertNotIn('private_secret', json.dumps(snapshot))
        self.assertNotIn('actual', json.dumps(snapshot))
        self.assertIsNone(validate_request_snapshot(None))
        changes = [lambda s: s.update(api_key='secret'), lambda s: s.update(schema_version=True),
                   lambda s: s['ai_profile'].update(device_id='remote/windows'),
                   lambda s: s['ai_profile'].update(path='/private/synthetic'),
                   lambda s: s.update(memory_profile={'id': 'other-bank'}),
                   lambda s: s.update(used_memory_references=['old secret'])]
        for mutate in changes:
            changed = json.loads(json.dumps(snapshot))
            mutate(changed)
            with self.assertRaises(ValueError):
                validate_request_snapshot(changed)
        default = self.profiles.create('默认', 'local/codex')
        self.assertEqual(make_profile_snapshot(default)['ai_profile']['model_id'], '')

    def test_legacy_mapping_is_stable_preserves_offline_parameters_and_renames_and_capacity(self):
        with patch.object(self.profiles, '_capabilities', side_effect=AssertionError('legacy is not capability proof')):
            preserved = self.profiles.import_legacy('offline:codex', '旧模型', 'local/codex',
                                                    model_id='local/codex/offline-old-model')
            default = self.profiles.import_legacy('default:codex', '旧默认', 'local/codex')
        self.assertEqual(preserved['model_id'], 'local/codex/offline-old-model')
        self.assertNotIn('supported', preserved)
        renamed = self.profiles.update(default['id'], 1, name='我重新命名')
        reopened = AIProfiles(self.path, capability_validator=self.validator)
        self.assertEqual(reopened.import_legacy('default:codex', '旧默认', 'local/codex'), renamed)
        before = self.path.read_bytes()
        with patch('profiles.MAX_PROFILES', 2):
            self.assertEqual(reopened.import_legacy('offline:codex', '忽略旧名', 'local/codex'), preserved)
            for action in (lambda: self.create(), lambda: self.profiles.copy(default['id'], 2),
                           lambda: self.profiles.import_legacy('new-key', '新旧参数', 'local/hermes')):
                with self.assertRaises(ValueError):
                    action()
                self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_mapping_two_instances_creates_one_profile_and_does_not_change_chat_revision(self):
        before = self.other_tables()
        other = AIProfiles(self.path)
        barrier = threading.Barrier(2)
        def migrate(service):
            barrier.wait()
            return service.import_legacy('default:hermes', '旧爱马仕', 'local/hermes')
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(migrate, service) for service in (self.profiles, other)]
            records = [future.result(timeout=5) for future in futures]
        self.assertEqual(records[0], records[1])
        self.assertEqual(len(self.profiles.list()['profiles']), 1)
        self.assertEqual(self.other_tables(), before)

    def test_default_selection_is_readonly_or_independent_and_schema_helper_does_not_commit(self):
        self.assertIsNone(self.profiles.get_default())
        before = self.other_tables()
        profile = self.create()
        with patch.object(self.profiles, '_capabilities', side_effect=AssertionError('default cannot invoke model')):
            self.assertEqual(self.profiles.set_default(profile['id']), profile['id'])
        original_bytes = self.path.read_bytes()
        self.assertEqual(self.profiles.get_default(), profile['id'])
        self.assertEqual(self.profiles.set_default(profile['id']), profile['id'])
        self.assertEqual(self.path.read_bytes(), original_bytes)
        with self.assertRaises(KeyError):
            self.profiles.set_default(str(uuid.uuid4()))
        self.assertEqual(self.other_tables(), before)
        separate = Path(self.temp.name) / 'transaction-only.sqlite'
        with sqlite3.connect(separate) as db:
            db.execute('BEGIN')
            initialize_schema(db)
            self.assertTrue(db.in_transaction)
            db.rollback()
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='ai_profiles'").fetchone())

    def test_saved_state_preserves_healthy_shape_and_projects_only_an_unavailable_default(self):
        profile = self.create()
        self.assertEqual(self.profiles.saved_state(), {
            'profiles': [profile], 'default_state': {'ai_profile_id': None, 'revision': 0}})
        self.profiles.set_default(profile['id'])
        self.assertEqual(self.profiles.saved_state(), {
            'profiles': [profile], 'default_state': {'ai_profile_id': profile['id'], 'revision': 1}})
        untouched = self.other_tables()
        fingerprints = set()
        for value in ('damaged-default', str(uuid.uuid4()), b'\xffdefault-id'):
            with self.subTest(value=value):
                with sqlite3.connect(self.path) as db:
                    db.execute("UPDATE profile_settings SET value=? WHERE key='default_ai_profile_id'", (value,))
                before, stamp = self.path.read_bytes(), self.path.stat().st_mtime_ns
                with patch.object(self.profiles, '_capabilities', side_effect=AssertionError('read cannot validate')):
                    saved = self.profiles.saved_state()
                    self.assertEqual(self.profiles.saved_state(), saved)
                    for strict_read in (self.profiles.get_default, self.profiles.get_default_state):
                        with self.assertRaises((ValueError, KeyError)):
                            strict_read()
                self.assertEqual(set(saved), {'profiles', 'default_state', 'default_unavailable', 'default_fingerprint'})
                self.assertEqual(saved['profiles'], [profile])
                self.assertEqual(saved['default_state'], {'ai_profile_id': None, 'revision': 1})
                self.assertIs(saved['default_unavailable'], True)
                self.assertRegex(saved['default_fingerprint'], r'^[0-9a-f]{64}$')
                fingerprints.add(saved['default_fingerprint'])
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.path.stat().st_mtime_ns, stamp)
                self.assertEqual(self.other_tables(), untouched)
                with sqlite3.connect(self.path) as db:
                    self.assertEqual(dict(db.execute('SELECT key,value FROM profile_settings')),
                        {'default_ai_profile_id': value, 'default_ai_profile_revision': '1'})
        self.assertEqual(len(fingerprints), 3)

    def test_explicit_cas_selection_repairs_bad_default_without_revising_profiles_or_chats(self):
        first, chosen = self.create(name='原默认'), self.create(name='可选择的配置')
        self.profiles.set_default(first['id'])
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE profile_settings SET value=? WHERE key='default_ai_profile_id'", (b'\xffbad-id',))
        before = self.path.read_bytes()
        with self.assertRaises(DefaultProfileConflict):
            self.profiles.set_default_state(chosen['id'], expected_revision=0)
        self.assertEqual(self.path.read_bytes(), before)
        untouched = self.other_tables()
        profiles = self.profiles.list()['profiles']
        with patch.object(self.profiles, '_capabilities', side_effect=AssertionError('selection cannot invoke model')):
            accepted = self.profiles.set_default_state(chosen['id'], expected_revision=1)
        self.assertEqual(accepted, {'ai_profile_id': chosen['id'], 'revision': 2})
        self.assertEqual(self.profiles.get_default_state(), accepted)
        self.assertEqual(self.profiles.saved_state(), {'profiles': profiles, 'default_state': accepted})
        self.assertEqual(self.profiles.list()['profiles'], profiles)
        self.assertEqual(self.other_tables(), untouched)

    def test_unreadable_default_revision_is_not_fabricated_or_repaired_by_inventory(self):
        profile = self.create()
        self.profiles.set_default(profile['id'])
        untouched = self.other_tables()
        for value in ('not-a-revision', '01', '-1', str(MAX_REVISION + 1), b'1'):
            with self.subTest(value=value):
                with sqlite3.connect(self.path) as db:
                    db.execute("UPDATE profile_settings SET value=? WHERE key='default_ai_profile_revision'", (value,))
                    db.execute("UPDATE profile_settings SET value='also-bad-id' WHERE key='default_ai_profile_id'")
                before = self.path.read_bytes()
                for action in (self.profiles.saved_state, self.profiles.get_default_state,
                               lambda: self.profiles.set_default_state(profile['id'], expected_revision=0)):
                    with self.assertRaises(ValueError):
                        action()
                    self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.other_tables(), untouched)


if __name__ == '__main__':
    unittest.main()
