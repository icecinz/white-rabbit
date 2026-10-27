"""App-owned Hermes configuration intents and application phases.

Only bounded identities, configuration snapshots, hashes and the one native
draft materialization fence live in chat.sqlite. The latter records an attempted
official title operation; it never creates or edits a native database row.
A configuration application is separate from the native prompt receipt ledger:
prepared, verified, or unknown here never proves a prompt was sent. Resolvers and
native RPCs belong to the holder and never run inside this module's transactions.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import unicodedata
from urllib.parse import quote
import uuid

from rabbit_runtime.hermes.profile_catalog import native_model_id
from rabbit_runtime.native_session_store import request_sha256, NativeSessionStoreError
from native_materials import NativeMaterialError, validate_refs
from native_memory_intents import NativeMemoryIntentError, validate_selection
from profiles import (AIProfiles, make_profile_snapshot, validate_profile_id,
                      validate_profile_revision, MAX_REVISION, REASONING_EFFORTS)
from native_chat_selection import assert_selection_in
from native_memory_selection import assert_memory_selection_in


MAX_LIST_CLIENTS = 128
_WORKSPACE = re.compile(r'[0-9a-f]{64}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_SESSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_PROBLEM = re.compile(r'[a-z][a-z0-9_]{0,95}\Z')
TERMINAL_PHASES = frozenset(('verified', 'failed'))
TRANSITIONS = {
    'prepared': frozenset(('model_started', 'reasoning_started', 'verified', 'failed', 'unknown')),
    'model_started': frozenset(('model_applied', 'failed', 'unknown')),
    'model_applied': frozenset(('reasoning_started', 'verified', 'failed', 'unknown')),
    'reasoning_started': frozenset(('verified', 'failed', 'unknown')),
    'unknown': frozenset(('verified', 'failed')),
    'verified': frozenset(), 'failed': frozenset(),
}
_ERRORS = {
    'invalid_request': 'Hermes AI 配置请求格式无效。',
    'profile_not_found': '找不到所选 Hermes AI 配置。',
    'profile_conflict': '所选 Hermes AI 配置已变化，请重新选择。',
    'profile_scope': 'Hermes 原生入口只接受本机 Hermes 的 AI 配置。',
    'intent_conflict': '这个发送编号已有不同对话、内容或配置，请核对原回执。',
    'intent_not_found': '尚无这次 Hermes 配置意图，不能开始应用。',
    'resolution_failed': 'Hermes 原生模型或请求强度没有精确匹配所选配置。',
    'application_busy': '这段 Hermes 对话还有未核对完成的配置应用。',
    'application_conflict': 'Hermes 配置应用状态已在另一操作中变化。',
    'application_identity': 'Hermes 配置应用不能替换已绑定的原生会话身份。',
    'application_not_found': '找不到这次 Hermes 配置应用。',
    'application_transition': 'Hermes 配置应用不能按这个顺序改变状态。',
    'application_terminal': '这次 Hermes 配置应用已经结束，不能重开或覆写。',
    'invalid_record': 'Hermes 配置意图或应用记录无法核验。',
}


class HermesProfileIntentError(ValueError):
    def __init__(self, code):
        self.code = code if code in _ERRORS else 'invalid_request'
        self.message = _ERRORS[self.code]
        super().__init__(self.message)


def _fail(code):
    raise HermesProfileIntentError(code)


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
    if (type(value) is not str or not 1 <= len(value) <= limit or value != value.strip()
            or any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs') for char in value)):
        _fail('resolution_failed')
    return value


def _profile(value):
    try:
        result = make_profile_snapshot(value)['ai_profile']
        _json(result).encode('utf-8')
    except (ValueError, TypeError, KeyError, UnicodeError):
        _fail('invalid_record')
    if result['agent_instance_id'] != 'local/hermes' or result['device_id'] != 'local':
        _fail('profile_scope')
    return result


def _options(value, profile=None, *, allow_legacy=False):
    fields = {'provider', 'selection_provider', 'model', 'selection_model', 'reasoning_effort', 'reasoning_config'}
    # Existing intents keep their old target. Project the former literal model
    # locally; never rewrite/re-resolve an already frozen application or replay.
    if allow_legacy and type(value) is dict and set(value) == fields - {'selection_model'}:
        value = {**value, 'selection_model': value['model']}
    if type(value) is not dict or set(value) != fields:
        _fail('resolution_failed')
    provider, model = _text(value['provider'], 256), _text(value['model'], 1024)
    selection_provider = _text(value['selection_provider'], 256)
    selection_model = _text(value['selection_model'], 1024)
    effort, config = value['reasoning_effort'], value['reasoning_config']
    if effort is not None and (type(effort) is not str or effort not in REASONING_EFFORTS):
        _fail('resolution_failed')
    if config is not None:
        if (type(config) is not dict or config.keys() - {'enabled', 'effort'}
                or 'enabled' in config and type(config['enabled']) is not bool):
            _fail('resolution_failed')
        config = {**({'enabled': config['enabled']} if 'enabled' in config else {}),
                  **({'effort': _text(config['effort'], 128)} if 'effort' in config else {})}
    if effort is not None and config != ({'enabled': False} if effort == 'none' else {'enabled': True, 'effort': effort}):
        _fail('resolution_failed')
    if effort != (profile['reasoning_effort'] if profile is not None else None):
        _fail('resolution_failed')
    if profile is not None and profile['model_id']:
        if profile['model_id'] != native_model_id(selection_provider, selection_model):
            _fail('resolution_failed')
    return {'provider': provider, 'selection_provider': selection_provider, 'model': model, 'selection_model': selection_model,
            'reasoning_effort': effort, 'reasoning_config': config}


def _identity(value):
    if type(value) is not dict or set(value) != {'session_id', 'session_key', 'cwd', 'profile_home'}:
        _fail('application_identity')
    if any(type(value[key]) is not str or not _SESSION.fullmatch(value[key]) for key in ('session_id', 'session_key')):
        _fail('application_identity')
    for key in ('cwd', 'profile_home'):
        path = value[key]
        if (type(path) is not str or not 1 <= len(path) <= 4096
                or any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs') for char in path)
                or not os.path.isabs(path) or os.path.abspath(path) != path or '..' in Path(path).parts):
            _fail('application_identity')
    return dict(value)


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def initialize_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS hermes_profile_intents (
        workspace_id TEXT NOT NULL, client_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        request_sha256 TEXT NOT NULL, ai_profile TEXT, native_options TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(workspace_id,client_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS hermes_profile_applications (
        workspace_id TEXT NOT NULL, client_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>0), identity TEXT NOT NULL,
        phase TEXT NOT NULL, problem TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(workspace_id,client_id))''')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS hermes_profile_one_active "
        "ON hermes_profile_applications(workspace_id,chat_id) WHERE phase NOT IN ('verified','failed')")
    db.execute('''CREATE TABLE IF NOT EXISTS hermes_native_drafts (
        workspace_id TEXT NOT NULL, chat_id TEXT NOT NULL, identity TEXT NOT NULL,
        title TEXT NOT NULL, phase TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(workspace_id,chat_id))''')


class HermesProfileIntents:
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
        row = db.execute('SELECT * FROM hermes_profile_intents WHERE workspace_id=? AND client_id=?',
                         (self.workspace_id, client_id)).fetchone()
        if row is None or chat_id is not None and row['chat_id'] != chat_id:
            return None
        try:
            _uuid(row['chat_id']); _uuid(row['client_id'])
            if type(row['request_sha256']) is not str or not _SHA.fullmatch(row['request_sha256']):
                _fail('invalid_record')
            raw = row['ai_profile']
            if (raw is not None and (type(raw) is not str or len(raw) > 4096)
                    or type(row['native_options']) is not str or len(row['native_options']) > 4096):
                _fail('invalid_record')
            profile = None if raw is None else _profile(json.loads(raw))
            if raw is not None and profile != json.loads(raw):
                _fail('invalid_record')
            options = _options(json.loads(row['native_options']), profile, allow_legacy=True)
            if type(row['created_at']) is not str or not 1 <= len(row['created_at']) <= 64:
                _fail('invalid_record')
        except (HermesProfileIntentError, ValueError, TypeError, KeyError, RecursionError):
            _fail('invalid_record')
        return {'workspace_id': self.workspace_id, 'chat_id': row['chat_id'], 'client_id': client_id,
                'request_sha256': row['request_sha256'], 'ai_profile': profile,
                'native_options': options, 'created_at': row['created_at']}

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
        # Freeze references, never their sources. A replay remains checkable
        # after files move or native configuration becomes unavailable.
        if (type(body) is not dict or not {'client_id', 'message'} <= body.keys()
                or body.keys() - {'client_id', 'message', 'ai_profile_id', 'ai_profile_revision', 'material_refs',
                                 'memory_profile_id', 'memory_profile_revision'}
                or ('ai_profile_id' in body) != ('ai_profile_revision' in body)
                or type(body['message']) is not str or not body['message'].strip()):
            _fail('invalid_request')
        client_id = _uuid(body['client_id'])
        explicit = 'ai_profile_id' in body
        try:
            if len(body['message'].encode('utf-8')) > 128 * 1024:
                _fail('invalid_request')
            if 'material_refs' in body:
                validate_refs(body['material_refs'])
            validate_selection(body)
            if explicit:
                validate_profile_id(body['ai_profile_id']); validate_profile_revision(body['ai_profile_revision'])
            digest = request_sha256(body)
        except (ValueError, TypeError, UnicodeError, NativeSessionStoreError, NativeMaterialError, NativeMemoryIntentError):
            _fail('invalid_request')
        return client_id, explicit, digest

    def lookup(self, chat_id, body):
        """Read one exact old HTTP intent with no model or material IO."""
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
            assert_selection_in(db, self.workspace_id, 'hermes', chat_id, profile_id, revision)
            assert_memory_selection_in(db, self.workspace_id, 'hermes', chat_id, memory_id, memory_revision)
            profile = self._saved_in(db, profile_id) if explicit else None
            if profile is not None and profile['revision'] != revision:
                _fail('profile_conflict')
        if not callable(resolver):
            _fail('resolution_failed')
        options = _options(resolver(dict(profile) if profile is not None else None), profile)
        with self._db(write=True) as db:
            old = self._replay(self._intent_in(db, client_id), chat_id, digest)
            if old is not None:
                return old
            assert_selection_in(db, self.workspace_id, 'hermes', chat_id, profile_id, revision)
            assert_memory_selection_in(db, self.workspace_id, 'hermes', chat_id, memory_id, memory_revision)
            if explicit:
                try:
                    unchanged = self._saved_in(db, profile_id) == profile
                except HermesProfileIntentError:
                    unchanged = False
                if not unchanged:
                    _fail('profile_conflict')
            db.execute('INSERT INTO hermes_profile_intents VALUES(?,?,?,?,?,?,?)',
                (self.workspace_id, client_id, chat_id, digest, _json(profile) if profile is not None else None,
                 _json(options), _stamp()))
            return self._intent_in(db, client_id)

    def list_for_chat(self, chat_id, client_ids):
        chat_id = _uuid(chat_id)
        if (type(client_ids) not in (list, tuple) or len(client_ids) > MAX_LIST_CLIENTS
                or len(set(_uuid(value) for value in client_ids)) != len(client_ids)):
            _fail('invalid_request')
        with self._db() as db:
            result = [self._intent_in(db, value, chat_id=chat_id) for value in client_ids]
        return [value for value in result if value is not None]

    def _application_in(self, db, client_id, *, chat_id=None):
        row = db.execute('SELECT * FROM hermes_profile_applications WHERE workspace_id=? AND client_id=?',
                         (self.workspace_id, client_id)).fetchone()
        if row is None or chat_id is not None and row['chat_id'] != chat_id:
            return None
        try:
            _uuid(row['chat_id']); _uuid(row['client_id'])
            if (type(row['revision']) is not int or not 1 <= row['revision'] <= MAX_REVISION
                    or type(row['identity']) is not str or len(row['identity']) > 16384
                    or row['phase'] not in TRANSITIONS
                    or row['problem'] is not None and (type(row['problem']) is not str or not _PROBLEM.fullmatch(row['problem']))
                    or any(type(row[key]) is not str or not 1 <= len(row[key]) <= 64 for key in ('created_at', 'updated_at'))):
                _fail('invalid_record')
            identity = _identity(json.loads(row['identity']))
            if self._intent_in(db, client_id, chat_id=row['chat_id']) is None:
                _fail('invalid_record')
        except (HermesProfileIntentError, ValueError, TypeError, KeyError, RecursionError):
            _fail('invalid_record')
        return {'workspace_id': self.workspace_id, 'chat_id': row['chat_id'], 'client_id': client_id,
                'revision': row['revision'], 'identity': identity, 'phase': row['phase'], 'problem': row['problem'],
                'created_at': row['created_at'], 'updated_at': row['updated_at']}

    def application(self, client_id, chat_id=None):
        client_id = _uuid(client_id)
        if chat_id is not None:
            chat_id = _uuid(chat_id)
        with self._db() as db:
            return self._application_in(db, client_id, chat_id=chat_id)

    def active_for_chat(self, chat_id):
        """Read the durable configuration barrier even before a prompt exists."""
        chat_id = _uuid(chat_id)
        with self._db() as db:
            rows = db.execute("SELECT client_id FROM hermes_profile_applications WHERE workspace_id=? "
                              "AND chat_id=? AND phase NOT IN ('verified','failed') LIMIT 2",
                              (self.workspace_id, chat_id)).fetchall()
            if len(rows) > 1:
                _fail('invalid_record')
            return self._application_in(db, rows[0]['client_id'], chat_id=chat_id) if rows else None

    def _draft_in(self, db, chat_id):
        row = db.execute('SELECT * FROM hermes_native_drafts WHERE workspace_id=? AND chat_id=?',
                         (self.workspace_id, chat_id)).fetchone()
        if row is None:
            return None
        try:
            _uuid(row['chat_id'])
            if (type(row['identity']) is not str or len(row['identity']) > 16384
                    or row['phase'] not in ('started', 'verified')
                    or any(type(row[key]) is not str or not 1 <= len(row[key]) <= 64 for key in ('created_at', 'updated_at'))):
                _fail('invalid_record')
            identity, title = _identity(json.loads(row['identity'])), _text(row['title'], 80)
        except (HermesProfileIntentError, ValueError, TypeError, KeyError, RecursionError):
            _fail('invalid_record')
        return {'workspace_id': self.workspace_id, 'chat_id': chat_id, 'identity': identity,
                'title': title, 'phase': row['phase'], 'created_at': row['created_at'], 'updated_at': row['updated_at']}

    def draft(self, chat_id):
        chat_id = _uuid(chat_id)
        with self._db() as db:
            return self._draft_in(db, chat_id)

    def begin_draft(self, chat_id, identity, title):
        """Fence the one native title attempt before any external call."""
        chat_id, identity, title = _uuid(chat_id), _identity(identity), _text(title, 80)
        with self._db(write=True) as db:
            old = self._draft_in(db, chat_id)
            if old is not None:
                if old['identity'] != identity or old['title'] != title:
                    _fail('application_identity')
                return dict(old, created=False)
            stamp = _stamp()
            db.execute('INSERT INTO hermes_native_drafts VALUES(?,?,?,?,?,?,?)',
                       (self.workspace_id, chat_id, _json(identity), title, 'started', stamp, stamp))
            return dict(self._draft_in(db, chat_id), created=True)

    def confirm_draft(self, chat_id, identity):
        chat_id, identity = _uuid(chat_id), _identity(identity)
        with self._db(write=True) as db:
            old = self._draft_in(db, chat_id)
            if old is None or old['identity'] != identity:
                _fail('application_identity')
            if old['phase'] == 'started':
                db.execute("UPDATE hermes_native_drafts SET phase='verified',updated_at=? WHERE workspace_id=? AND chat_id=?",
                           (_stamp(), self.workspace_id, chat_id))
            return self._draft_in(db, chat_id)

    def begin_application(self, chat_id, client_id, identity):
        chat_id, client_id, identity = _uuid(chat_id), _uuid(client_id), _identity(identity)
        with self._db(write=True) as db:
            old = self._application_in(db, client_id)
            if old is not None:
                if old['chat_id'] != chat_id:
                    _fail('application_conflict')
                if old['identity'] != identity:
                    _fail('application_identity')
                return old  # Neither unknown nor terminal applications are restarted.
            if self._intent_in(db, client_id, chat_id=chat_id) is None:
                _fail('intent_not_found')
            if db.execute("SELECT 1 FROM hermes_profile_applications WHERE workspace_id=? AND chat_id=? "
                          "AND phase NOT IN ('verified','failed')", (self.workspace_id, chat_id)).fetchone():
                _fail('application_busy')
            stamp = _stamp()
            db.execute('INSERT INTO hermes_profile_applications VALUES(?,?,?,?,?,?,?,?,?)',
                (self.workspace_id, client_id, chat_id, 1, _json(identity), 'prepared', None, stamp, stamp))
            return self._application_in(db, client_id)

    def advance_application(self, client_id, expected_revision, phase, problem=None):
        client_id = _uuid(client_id)
        if (type(expected_revision) is not int or not 1 <= expected_revision < MAX_REVISION
                or type(phase) is not str or phase not in TRANSITIONS
                or problem is not None and (type(problem) is not str or not _PROBLEM.fullmatch(problem)
                                           or phase not in ('unknown', 'failed'))):
            _fail('invalid_request')
        with self._db(write=True) as db:
            old = self._application_in(db, client_id)
            if old is None:
                _fail('application_not_found')
            if old['revision'] != expected_revision:
                _fail('application_conflict')
            if old['phase'] in TERMINAL_PHASES:
                _fail('application_terminal')
            if phase not in TRANSITIONS[old['phase']]:
                _fail('application_transition')
            db.execute('UPDATE hermes_profile_applications SET revision=?,phase=?,problem=?,updated_at=? '
                       'WHERE workspace_id=? AND client_id=? AND revision=?',
                       (expected_revision + 1, phase, problem, _stamp(), self.workspace_id, client_id, expected_revision))
            return self._application_in(db, client_id)
