"""Frozen AI choices for native Codex/Claude turns, without an Agent loop.

This table belongs to the existing private chat database, not the strict native
session ledger. It stores hashes and small configuration snapshots, never user
inputs. A resolver runs outside every SQLite transaction; its result is then
committed only if the exact saved profile still exists unchanged. Replays use
the first frozen result even if the profile or model directory later changes.

Empty model / None effort mean no native override. Native Codex may retain a
previous turn's settings; this module never calls that restoring initial defaults.
Freezing an intent is not proof that a native turn was sent or accepted.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote
import uuid

from agent_registry import valid_model_id
from rabbit_runtime.native_session_store import request_sha256, NativeSessionStoreError
from profiles import (AIProfiles, make_profile_snapshot, validate_profile_id,
                      validate_profile_revision, REASONING_EFFORTS)
from native_chat_selection import assert_selection_in
from native_memory_selection import assert_memory_selection_in
from native_memory_intents import validate_selection
from native_materials import validate_refs


MAX_LIST_CLIENTS = 128
_WORKSPACE = re.compile(r'[0-9a-f]{64}\Z')
_CLIENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_FIELDS = frozenset(('client_id', 'inputs', 'ai_profile_id', 'ai_profile_revision', 'material_refs',
                     'memory_profile_id', 'memory_profile_revision'))
_CLAUDE_FIELDS = (_FIELDS - {'inputs'}) | {'message'}
_MESSAGES = {
    'invalid_request': '原生 AI 配置请求格式无效，尚未发送。',
    'profile_not_found': '找不到所选 AI 配置，尚未发送。',
    'profile_conflict': '所选 AI 配置已经变化，请重新选择后发送。',
    'profile_scope': '这段原生对话只接受相同本机 Agent 的 AI 配置，尚未发送。',
    'resolution_failed': '所选 AI 配置暂不能用于这段原生对话，尚未发送。',
    'intent_conflict': '这个发送编号已有不同对话或内容，请先核对原回执。',
    'invalid_record': '原生 AI 配置意图记录无法核验，请先核对原回执。',
}


class NativeProfileIntentError(ValueError):
    def __init__(self, code, message=None):
        self.code = code if code in _MESSAGES else 'invalid_request'
        self.message = message if message is not None else _MESSAGES[self.code]
        super().__init__(self.message)


def _fail(code):
    raise NativeProfileIntentError(code)


def _chat(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        _fail('invalid_request')
    return value


def _client(value):
    if type(value) is not str or not _CLIENT.fullmatch(value):
        _fail('invalid_request')
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _profile(value, request_kind='codex'):
    """Reuse the existing bounded profile codec, without a memory selection."""
    try:
        result = make_profile_snapshot(value)['ai_profile']
        _json(result).encode('utf-8')
    except (ValueError, KeyError, TypeError, UnicodeError):
        _fail('invalid_record')
    if result['agent_instance_id'] != 'local/' + request_kind or result['device_id'] != 'local':
        _fail('profile_scope')
    return result


def _options(value, profile, request_kind='codex'):
    """Exactly the requested native overrides; no invented default or policy."""
    if type(value) is not dict or value.keys() - ({'model'} if request_kind == 'claude' else {'model', 'effort'}):
        _fail('resolution_failed')
    prefix = 'local/' + request_kind + '/'
    model = profile['model_id'].removeprefix(prefix) if profile else ''
    effort = profile['reasoning_effort'] if profile else None
    expected = {}
    if model:
        if not valid_model_id(prefix + model, request_kind) or request_kind == 'claude' and any(
                ord(char) <= 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in model):
            _fail('resolution_failed')
        expected['model'] = model
    if effort is not None:
        if request_kind == 'claude':
            _fail('resolution_failed')
        if type(effort) is not str or effort not in REASONING_EFFORTS:
            _fail('resolution_failed')
        expected['effort'] = effort
    if value != expected or any(type(item) is not str for item in value.values()):
        _fail('resolution_failed')
    return dict(expected)


def initialize_schema(db):
    """Only this module's table; participate in the caller's transaction."""
    db.execute('''CREATE TABLE IF NOT EXISTS native_profile_intents (
        workspace_id TEXT NOT NULL CHECK(length(workspace_id)=64),
        client_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
        ai_profile TEXT, native_options TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(workspace_id,client_id))''')


def _read_scope_in(db, workspace_id, client_id, request_kind, *, chat_id=None):
    query = 'SELECT * FROM native_profile_intents WHERE workspace_id=? AND client_id=?'
    parameters = (workspace_id, client_id)
    if chat_id is not None:
        query += ' AND chat_id=?'
        parameters += (chat_id,)
    row = db.execute(query, parameters).fetchone()
    if row is None:
        return None
    try:
        if (row['workspace_id'] != workspace_id or _client(row['client_id']) != client_id
                or type(row['request_sha256']) is not str or not _SHA.fullmatch(row['request_sha256'])
                or type(row['created_at']) is not str or not 1 <= len(row['created_at']) <= 64
                or any(ord(char) < 32 for char in row['created_at'])):
            _fail('invalid_record')
        _chat(row['chat_id'])
        if (row['ai_profile'] is not None and
                (type(row['ai_profile']) is not str or len(row['ai_profile']) > 4096)
                or type(row['native_options']) is not str or len(row['native_options']) > 1024):
            _fail('invalid_record')
        saved = json.loads(row['ai_profile']) if row['ai_profile'] is not None else None
        profile = _profile(saved, request_kind) if saved is not None else None
        if saved is not None and profile != saved:
            _fail('invalid_record')
        options = _options(json.loads(row['native_options']), profile, request_kind)
    except (NativeProfileIntentError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        _fail('invalid_record')
    return {'workspace_id': workspace_id, 'chat_id': row['chat_id'],
            'client_id': client_id, 'request_sha256': row['request_sha256'],
            'ai_profile': profile, 'native_options': options, 'created_at': row['created_at']}

def frozen_claude_profile_in(db, workspace_id, chat_id, client_id, digest):
    """Read an earlier Claude profile freeze in this same catalogue transaction."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_profile_intents'").fetchone() is None:
        return None
    record = _read_scope_in(db, workspace_id, client_id, 'claude')
    if record is not None and (record['chat_id'] != chat_id or record['request_sha256'] != digest):
        _fail('intent_conflict')
    return record


class NativeProfileIntents:
    def __init__(self, path, *, workspace_id, request_kind='codex'):
        if type(workspace_id) is not str or not _WORKSPACE.fullmatch(workspace_id):
            _fail('invalid_request')
        if type(request_kind) is not str or request_kind not in ('codex', 'claude'):
            _fail('invalid_request')
        self.request_kind = request_kind
        self.workspace_id = workspace_id
        self.path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
        cloud = Path.home() / 'Library' / 'Mobile Documents'
        if (not self.path.is_file() or self.path.is_symlink()
                or any(parent.is_symlink() for parent in self.path.parents)
                or self.path == cloud or cloud in self.path.parents):
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

    def _read_in(self, db, client_id, *, chat_id=None):
        return _read_scope_in(db, self.workspace_id, client_id, self.request_kind, chat_id=chat_id)

    @staticmethod
    def _replay(record, chat_id, digest):
        if record is not None and (record['chat_id'] != chat_id or record['request_sha256'] != digest):
            _fail('intent_conflict')
        return record

    def _saved_in(self, db, profile_id):
        try:
            return _profile(AIProfiles.get_in(db, profile_id), self.request_kind)
        except KeyError:
            _fail('profile_not_found')

    @staticmethod
    def _request_identity(body, request_kind='codex'):
        # Material references are validated by the owning HTTP/material module.
        # This module freezes their exact JSON identity without reading bodies.
        required = {'client_id', 'message' if request_kind == 'claude' else 'inputs'}
        fields = _CLAUDE_FIELDS if request_kind == 'claude' else _FIELDS
        if (type(body) is not dict or not required <= body.keys()
                or body.keys() - fields
                or ('ai_profile_id' in body) != ('ai_profile_revision' in body)
                or request_kind == 'codex' and (type(body.get('inputs')) is not list or not body['inputs']
                    or any(type(item) is not dict for item in body['inputs']))
                or request_kind == 'claude' and (type(body.get('message')) is not str or not body['message'].strip())
                or 'material_refs' in body and type(body['material_refs']) is not list):
            _fail('invalid_request')
        client_id = _chat(body['client_id']) if request_kind == 'claude' else _client(body['client_id'])
        explicit = 'ai_profile_id' in body
        try:
            if request_kind == 'claude':
                if len(body['message'].encode('utf-8')) > 128 * 1024:
                    _fail('invalid_request')
                if 'material_refs' in body:
                    validate_refs(body['material_refs'])
            validate_selection(body)
            if explicit:
                validate_profile_id(body['ai_profile_id'])
                validate_profile_revision(body['ai_profile_revision'])
            digest = request_sha256(body)
        except (ValueError, TypeError, NativeSessionStoreError):
            _fail('invalid_request')
        return client_id, explicit, digest

    def lookup(self, chat_id, body):
        """Verify an old raw request without discovery, writes or material IO."""
        chat_id = _chat(chat_id)
        client_id, _, digest = self._request_identity(body, self.request_kind)
        with self._db() as db:
            return self._replay(self._read_in(db, client_id), chat_id, digest)

    def freeze(self, chat_id, body, resolver):
        """Freeze before native send. This operation itself never calls an Agent.

        The resolver accepts a detached seven-field AI profile snapshot and
        returns only model/effort overrides. It may inspect capabilities, but
        must not send a model turn. It runs with no SQLite transaction held.
        """
        chat_id = _chat(chat_id)
        client_id, explicit, digest = self._request_identity(body, self.request_kind)
        # Do not retain the caller's mutable body across an external resolver.
        profile_id = body.get('ai_profile_id')
        revision = body.get('ai_profile_revision')
        memory = validate_selection(body)
        memory_id, memory_revision = (memory['id'], memory['revision']) if memory else (None, None)
        with self._db() as db:
            previous = self._replay(self._read_in(db, client_id), chat_id, digest)
            if previous is not None:
                return previous
            assert_selection_in(db, self.workspace_id, self.request_kind, chat_id, profile_id, revision)
            assert_memory_selection_in(db, self.workspace_id, self.request_kind, chat_id, memory_id, memory_revision)
            profile = self._saved_in(db, profile_id) if explicit else None
            if profile is not None and profile['revision'] != revision:
                _fail('profile_conflict')
        # Discovery/native read-only work must not hold a database transaction.
        if explicit:
            if not callable(resolver):
                _fail('resolution_failed')
            # The caller owns capability diagnostics and HTTP mapping. A
            # resolver failure propagates unchanged and freezes no intent.
            resolved = resolver(dict(profile))
            options = _options(resolved, profile, self.request_kind)
        else:
            options = {}
        stamp = datetime.now(timezone.utc).isoformat()
        with self._db(write=True) as db:
            # A concurrent first call may already have frozen this request.
            previous = self._replay(self._read_in(db, client_id), chat_id, digest)
            if previous is not None:
                return previous
            assert_selection_in(db, self.workspace_id, self.request_kind, chat_id, profile_id, revision)
            assert_memory_selection_in(db, self.workspace_id, self.request_kind, chat_id, memory_id, memory_revision)
            if explicit and self._saved_in(db, profile_id) != profile:
                _fail('profile_conflict')
            db.execute('INSERT INTO native_profile_intents VALUES(?,?,?,?,?,?,?)',
                       (self.workspace_id, client_id, chat_id, digest,
                        _json(profile) if profile is not None else None, _json(options), stamp))
            return self._read_in(db, client_id)

    def list_for_chat(self, chat_id, client_ids):
        """Read only requested receipts in this workspace and exact chat scope."""
        chat_id = _chat(chat_id)
        if (type(client_ids) not in (list, tuple) or len(client_ids) > MAX_LIST_CLIENTS
                or len(set(_client(value) for value in client_ids)) != len(client_ids)):
            _fail('invalid_request')
        with self._db() as db:
            records = [self._read_in(db, client_id, chat_id=chat_id) for client_id in client_ids]
        return [record for record in records if record is not None]
