"""Pi configuration intent and verified startup metadata in the app database.

No prompt body, native transcript, credential, Agent or transport lives here.
Resolvers run outside SQLite transactions. Prompt submission receipts remain in
PiNativeStore: an intent alone never means a prompt was sent. Verified startup
settings may be recorded before submission, including the original baseline.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote
import unicodedata
import uuid

from rabbit_runtime.native_session_store import request_sha256, NativeSessionStoreError
from native_materials import validate_refs, NativeMaterialError
from native_memory_intents import validate_selection, NativeMemoryIntentError
from profiles import (AIProfiles, make_profile_snapshot, validate_profile_id,
                      validate_profile_revision, MAX_REVISION)
from native_chat_selection import assert_selection_in
from native_memory_selection import assert_memory_selection_in


MAX_LIST_CLIENTS = 128
THINKING = frozenset(('off', 'minimal', 'low', 'medium', 'high', 'xhigh'))
_WORKSPACE = re.compile(r'[0-9a-f]{64}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_SESSION = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9._-]{0,254}[A-Za-z0-9])?\Z')
_ERRORS = {
    'invalid_request': 'Pi AI 配置请求格式无效。',
    'profile_not_found': '找不到所选 Pi AI 配置。',
    'profile_conflict': '所选 Pi AI 配置已变化，请重新选择。',
    'profile_scope': 'Pi 原生入口只接受本机 Pi 的 AI 配置。',
    'intent_conflict': '这个发送编号已有不同对话、内容或配置，请核对原回执。',
    'resolution_failed': 'Pi 原生模型或思考设置没有精确匹配所选配置。',
    'startup_conflict': 'Pi 已确认启动设置已在另一操作中变化，请重新核对。',
    'startup_identity': 'Pi 原生会话身份与已确认启动记录不一致。',
    'startup_unverified': 'Pi 原生状态尚未证实这份启动设置。',
    'invalid_record': 'Pi 配置意图或启动记录无法核验。',
}


class PiProfileIntentError(ValueError):
    def __init__(self, code):
        self.code = code if code in _ERRORS else 'invalid_request'
        self.message = _ERRORS[self.code]
        super().__init__(self.message)


def _fail(code):
    raise PiProfileIntentError(code)


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        _fail('invalid_request')
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _text(value, limit):
    if (type(value) is not str or not 1 <= len(value) <= limit
            or value != value.strip() or value.startswith('-')
            or any(unicodedata.category(character) in ('Cc', 'Cf', 'Cs') for character in value)):
        _fail('resolution_failed')
    try:
        value.encode('utf-8')
    except UnicodeError:
        _fail('resolution_failed')
    return value


def _profile(value):
    try:
        result = make_profile_snapshot(value)['ai_profile']
        _json(result).encode('utf-8')
    except (ValueError, TypeError, KeyError, UnicodeError):
        _fail('invalid_record')
    if result['agent_instance_id'] != 'local/pi' or result['device_id'] != 'local':
        _fail('profile_scope')
    return result


def _overrides(value, profile=None):
    if type(value) is not dict or set(value) != {'provider', 'model', 'thinking'}:
        _fail('resolution_failed')
    result = {'provider': _text(value['provider'], 256), 'model': _text(value['model'], 1024)}
    if type(value['thinking']) is not str or value['thinking'] not in THINKING:
        _fail('resolution_failed')
    result['thinking'] = value['thinking']
    if profile is not None:
        from rabbit_runtime.pi.profile_catalog import native_model_id
        if profile['model_id'] and profile['model_id'] != native_model_id(result['provider'], result['model']):
            _fail('resolution_failed')
        requested = profile['reasoning_effort']
        if requested is not None and result['thinking'] != ('off' if requested == 'none' else requested):
            _fail('resolution_failed')
    return result


def _identity(value):
    if type(value) is not dict or set(value) != {'session_id', 'session_file', 'cwd'}:
        _fail('startup_identity')
    session_id = value['session_id']
    if type(session_id) is not str or not _SESSION.fullmatch(session_id):
        _fail('startup_identity')
    for key in ('session_file', 'cwd'):
        path = value[key]
        if (type(path) is not str or not 1 <= len(path) <= 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in path)
                or not os.path.isabs(path) or os.path.abspath(path) != path or '..' in Path(path).parts):
            _fail('startup_identity')
        try:
            path.encode('utf-8')
        except UnicodeError:
            _fail('startup_identity')
    return dict(value)


def initialize_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS pi_profile_intents (
        workspace_id TEXT NOT NULL, client_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        request_sha256 TEXT NOT NULL, ai_profile TEXT, startup_overrides TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(workspace_id,client_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS pi_startup_settings (
        workspace_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>0), identity TEXT NOT NULL,
        startup_overrides TEXT NOT NULL, created_at TEXT NOT NULL, observed_at TEXT NOT NULL,
        PRIMARY KEY(workspace_id,chat_id))''')


class PiProfileIntents:
    def __init__(self, path, *, workspace_id):
        if type(workspace_id) is not str or not _WORKSPACE.fullmatch(workspace_id):
            _fail('invalid_request')
        self.workspace_id = workspace_id
        self.path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
        cloud = Path.home() / 'Library/Mobile Documents'
        if (not self.path.is_file() or self.path.is_symlink() or cloud in self.path.parents
                or any(parent.is_symlink() for parent in self.path.parents)):
            _fail('invalid_request')
        with self._db(write=True) as db:
            initialize_schema(db)

    @contextmanager
    def _db(self, *, write=False):
        uri = 'file:' + quote(str(self.path), safe='/') + ('?mode=rw' if write else '?mode=ro')
        db = sqlite3.connect(uri, uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            if not write:
                db.execute('PRAGMA query_only=ON')
            with db:
                yield db
        finally:
            db.close()

    def _intent_in(self, db, client_id, *, chat_id=None):
        query = 'SELECT * FROM pi_profile_intents WHERE workspace_id=? AND client_id=?'
        values = (self.workspace_id, client_id)
        if chat_id is not None:
            query += ' AND chat_id=?'; values += (chat_id,)
        row = db.execute(query, values).fetchone()
        if row is None:
            return None
        try:
            _uuid(row['chat_id']); _uuid(row['client_id'])
            if type(row['request_sha256']) is not str or not _SHA.fullmatch(row['request_sha256']):
                _fail('invalid_record')
            raw = row['ai_profile']
            if (raw is not None and (type(raw) is not str or len(raw) > 4096)
                    or type(row['startup_overrides']) is not str or len(row['startup_overrides']) > 4096):
                _fail('invalid_record')
            profile = None if raw is None else _profile(json.loads(raw))
            if raw is not None and profile != json.loads(raw):
                _fail('invalid_record')
            overrides = _overrides(json.loads(row['startup_overrides']), profile)
            if type(row['created_at']) is not str or not 1 <= len(row['created_at']) <= 64:
                _fail('invalid_record')
        except (PiProfileIntentError, ValueError, TypeError, KeyError, RecursionError):
            _fail('invalid_record')
        return {'workspace_id': self.workspace_id, 'chat_id': row['chat_id'], 'client_id': client_id,
                'request_sha256': row['request_sha256'], 'ai_profile': profile,
                'startup_overrides': overrides, 'created_at': row['created_at']}

    @staticmethod
    def _replay(record, chat_id, digest):
        if record is not None and (record['chat_id'] != chat_id or record['request_sha256'] != digest):
            _fail('intent_conflict')
        return record

    @staticmethod
    def _saved_in(db, profile_id):
        try:
            return _profile(AIProfiles.get_in(db, profile_id))
        except KeyError:
            _fail('profile_not_found')

    @staticmethod
    def _request_identity(body):
        # References are frozen exactly; only the material owner may resolve
        # their sources. This codec performs no material or native IO.
        if (type(body) is not dict or not {'client_id', 'message'} <= body.keys()
                or body.keys() - {'client_id', 'message', 'ai_profile_id', 'ai_profile_revision', 'material_refs', 'memory_profile_id', 'memory_profile_revision'}
                or ('ai_profile_id' in body) != ('ai_profile_revision' in body)
                or type(body['message']) is not str or not body['message'].strip()
                or 'material_refs' in body and type(body['material_refs']) is not list):
            _fail('invalid_request')
        client_id = _uuid(body['client_id'])
        explicit = 'ai_profile_id' in body
        try:
            validate_selection(body)
            if len(body['message'].encode('utf-8')) > 128 * 1024:
                _fail('invalid_request')
            if 'material_refs' in body:
                validate_refs(body['material_refs'])
            if explicit:
                validate_profile_id(body['ai_profile_id']); validate_profile_revision(body['ai_profile_revision'])
            digest = request_sha256(body)
        except (ValueError, TypeError, UnicodeError, NativeSessionStoreError, NativeMaterialError, NativeMemoryIntentError):
            _fail('invalid_request')
        return client_id, explicit, digest

    def lookup(self, chat_id, body):
        """Read an exact old HTTP intent; never resolve sources or a model."""
        chat_id = _uuid(chat_id)
        client_id, _, digest = self._request_identity(body)
        with self._db() as db:
            return self._replay(self._intent_in(db, client_id), chat_id, digest)

    def freeze(self, chat_id, body, resolver):
        chat_id = _uuid(chat_id)
        client_id, explicit, digest = self._request_identity(body)
        profile_id, revision = body.get('ai_profile_id'), body.get('ai_profile_revision')
        memory = validate_selection(body)
        memory_id, memory_revision = (memory['id'], memory['revision']) if memory else (None, None)
        with self._db() as db:
            old = self._replay(self._intent_in(db, client_id), chat_id, digest)
            if old is not None:
                return old
            assert_selection_in(db, self.workspace_id, 'pi', chat_id, profile_id, revision)
            assert_memory_selection_in(db, self.workspace_id, 'pi', chat_id, memory_id, memory_revision)
            profile = self._saved_in(db, profile_id) if explicit else None
            if profile is not None and profile['revision'] != revision:
                _fail('profile_conflict')
        if not callable(resolver):
            _fail('resolution_failed')
        # Even legacy/no-profile requests capture the current complete native
        # triple. Its frozen inherited values cannot drift during a retry.
        overrides = _overrides(resolver(dict(profile) if profile is not None else None), profile)
        with self._db(write=True) as db:
            old = self._replay(self._intent_in(db, client_id), chat_id, digest)
            if old is not None:
                return old
            assert_selection_in(db, self.workspace_id, 'pi', chat_id, profile_id, revision)
            assert_memory_selection_in(db, self.workspace_id, 'pi', chat_id, memory_id, memory_revision)
            if explicit and self._saved_in(db, profile_id) != profile:
                _fail('profile_conflict')
            db.execute('INSERT INTO pi_profile_intents VALUES(?,?,?,?,?,?,?)',
                (self.workspace_id, client_id, chat_id, digest, _json(profile) if profile is not None else None,
                 _json(overrides), datetime.now(timezone.utc).isoformat()))
            return self._intent_in(db, client_id)

    def list_for_chat(self, chat_id, client_ids):
        chat_id = _uuid(chat_id)
        if (type(client_ids) not in (list, tuple) or len(client_ids) > MAX_LIST_CLIENTS
                or len(set(_uuid(value) for value in client_ids)) != len(client_ids)):
            _fail('invalid_request')
        with self._db() as db:
            result = [self._intent_in(db, value, chat_id=chat_id) for value in client_ids]
        return [value for value in result if value is not None]

    def _startup_in(self, db, chat_id):
        row = db.execute('SELECT * FROM pi_startup_settings WHERE workspace_id=? AND chat_id=?',
                         (self.workspace_id, chat_id)).fetchone()
        if row is None:
            return None
        try:
            if type(row['revision']) is not int or not 1 <= row['revision'] <= MAX_REVISION:
                _fail('invalid_record')
            if (type(row['identity']) is not str or len(row['identity']) > 16384
                    or type(row['startup_overrides']) is not str or len(row['startup_overrides']) > 4096
                    or any(type(row[key]) is not str or not 1 <= len(row[key]) <= 64
                           for key in ('created_at', 'observed_at'))):
                _fail('invalid_record')
            identity = _identity(json.loads(row['identity']))
            overrides = _overrides(json.loads(row['startup_overrides']))
        except (PiProfileIntentError, ValueError, TypeError, KeyError, RecursionError):
            _fail('invalid_record')
        return {'workspace_id': self.workspace_id, 'chat_id': chat_id, 'revision': row['revision'],
                'identity': identity, 'startup_overrides': overrides,
                'created_at': row['created_at'], 'observed_at': row['observed_at']}

    def startup(self, chat_id):
        chat_id = _uuid(chat_id)
        with self._db() as db:
            return self._startup_in(db, chat_id)

    def confirm_startup(self, chat_id, *, expected_revision, identity, startup_overrides, observed_state):
        chat_id = _uuid(chat_id)
        if type(expected_revision) is not int or not 0 <= expected_revision < MAX_REVISION:
            _fail('invalid_request')
        identity, overrides = _identity(identity), _overrides(startup_overrides)
        if type(observed_state) is not dict or type(observed_state.get('model')) is not dict:
            _fail('startup_unverified')
        actual = observed_state['model']
        if (actual.get('provider') != overrides['provider'] or actual.get('id') != overrides['model']
                or observed_state.get('thinkingLevel') != overrides['thinking']
                or observed_state.get('sessionId') != identity['session_id']
                or observed_state.get('sessionFile') != identity['session_file']):
            _fail('startup_unverified')
        # Pi get_state need not report cwd. The holder must provide that field
        # only after validating the exact native session header against binding.
        if observed_state.get('cwd') != identity['cwd']:
            _fail('startup_unverified')
        with self._db(write=True) as db:
            old = self._startup_in(db, chat_id)
            if (old['revision'] if old else 0) != expected_revision:
                _fail('startup_conflict')
            if old is not None and old['identity'] != identity:
                _fail('startup_identity')
            stamp = datetime.now(timezone.utc).isoformat()
            db.execute('INSERT INTO pi_startup_settings VALUES(?,?,?,?,?,?,?) '
                'ON CONFLICT(workspace_id,chat_id) DO UPDATE SET revision=excluded.revision,'
                'startup_overrides=excluded.startup_overrides,observed_at=excluded.observed_at',
                (self.workspace_id, chat_id, expected_revision + 1, _json(identity), _json(overrides),
                 old['created_at'] if old else stamp, stamp))
            return self._startup_in(db, chat_id)
