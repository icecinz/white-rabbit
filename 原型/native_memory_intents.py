"""Frozen readonly memory for native entries, without native submission.

The first preparing row grants only this call's recall work. Existing rows never
authorize another recall or native send. The native ledger remains the owner of
submission receipts. Only bounded memory evidence is stored here, not user inputs
or material bodies; raw and derived request identities are bound separately.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from rabbit_memory.memory_profiles import (MemoryProfiles, make_memory_snapshot, validate_memory_snapshot,
                             validate_memory_profile_id, validate_memory_profile_revision)
from memory_context import (MAX_PACKAGE_BYTES, memory_query, validate_recall_package,
                            public_memory_info, selected_memory_block)
from rabbit_runtime.native_session_store import NativeSessionStoreError, request_sha256
from native_materials import MAX_INPUT_BYTES, NativeMaterialError, validate_refs
from profiles import validate_profile_id, validate_profile_revision
from native_memory_selection import assert_memory_selection_in


MAX_PROFILE_BYTES = 24 * 1024
MAX_LIST_CLIENTS = 128
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_CLIENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z')
_FIELDS = frozenset(('client_id', 'inputs', 'ai_profile_id', 'ai_profile_revision',
                     'material_refs', 'memory_profile_id', 'memory_profile_revision'))
_HERMES_FIELDS = (_FIELDS - {'inputs'}) | {'message'}
_CLAUDE_FIELDS = _HERMES_FIELDS
_DSH_FIELDS = frozenset(('client_id', 'message', 'memory_profile_id', 'memory_profile_revision', 'material_refs'))
_STATES = frozenset(('preparing', 'ready', 'failed'))
_ERRORS = {
    'invalid_request': '本轮只读记忆请求格式无效，尚未提交原生消息。',
    'profile_not_found': '找不到所选记忆配置，尚未提交原生消息。',
    'profile_conflict': '所选记忆配置已经变化，尚未提交原生消息。',
    'profile_read_only_required': '这个入口只接受只读取记忆配置，尚未提交原生消息。',
    'intent_conflict': '这个发送编号已有不同对话、内容或记忆选择，请核对原回执。',
    'intent_not_found': '找不到本轮已冻结的记忆准备记录。',
    'preparation_not_ready': '本轮记忆准备尚未完成，没有绑定原生输入。',
    'prepare_failed': '本轮记忆准备失败，尚未提交原生消息；原请求保留。',
    'invalid_record': '本轮记忆准备记录无法核验，请保留原编号核对。',
}
NATIVE_MEMORY_INTENTS_DDL = '''CREATE TABLE IF NOT EXISTS native_memory_intents (
    workspace_id TEXT NOT NULL, client_id TEXT NOT NULL, chat_id TEXT NOT NULL,
    raw_request_sha256 TEXT NOT NULL, query_sha256 TEXT NOT NULL,
    memory_profile TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('preparing','ready','failed')),
    error TEXT CHECK(error IS NULL OR error='prepare_failed'),
    memory_inputs TEXT, memory_inputs_sha256 TEXT,
    memory_connection_fingerprint TEXT, derived_inputs_sha256 TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(workspace_id,client_id))'''


class NativeMemoryIntentError(ValueError):
    def __init__(self, code):
        self.code = code if code in _ERRORS else 'invalid_request'
        super().__init__(_ERRORS[self.code])


def _fail(code):
    raise NativeMemoryIntentError(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _hash(value):
    if type(value) is not str or not _SHA.fullmatch(value):
        _fail('invalid_record')
    return value


def _client(value):
    if type(value) is not str or not _CLIENT.fullmatch(value):
        _fail('invalid_request')
    return value


def _chat(value):
    try:
        return validate_memory_profile_id(value)
    except ValueError:
        _fail('invalid_request')


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _timestamp(value):
    if type(value) is not str or not 1 <= len(value) <= 64:
        _fail('invalid_record')
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo != timezone.utc or parsed.isoformat(timespec='microseconds') != value:
            _fail('invalid_record')
    except ValueError:
        _fail('invalid_record')
    return value


def _decode(raw, maximum):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail('invalid_record')
            result[key] = value
        return result
    if type(raw) is not str or len(raw.encode('utf-8')) > maximum:
        _fail('invalid_record')
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _value: _fail('invalid_record'))


def validate_selection(body):
    """Return a selection or None; never insert defaults into the raw request."""
    if type(body) is not dict:
        _fail('invalid_request')
    if 'memory_profile_id' not in body:
        if 'memory_profile_revision' in body:
            _fail('invalid_request')
        return None
    identifier = body['memory_profile_id']
    if identifier == 'none':
        if body.get('memory_profile_revision') is not None:
            _fail('invalid_request')
        return None
    try:
        return {'id': validate_memory_profile_id(identifier),
                'revision': validate_memory_profile_revision(body.get('memory_profile_revision'))}
    except ValueError:
        _fail('invalid_request')


def _request(body, query_filter=None, request_kind='codex'):
    selection = validate_selection(body)
    if selection is None:
        return None
    try:
        if request_kind in ('hermes', 'claude', 'pi', 'dsh'):
            fields = _DSH_FIELDS if request_kind == 'dsh' else _CLAUDE_FIELDS if request_kind == 'claude' else _HERMES_FIELDS
            if (not {'client_id', 'message'} <= body.keys() or body.keys() - fields
                    or type(body['message']) is not str or not body['message'].strip()
                    or ('ai_profile_id' in body) != ('ai_profile_revision' in body)):
                _fail('invalid_request')
            client_id, query = _chat(body['client_id']), body['message']
            if 'ai_profile_id' in body:
                validate_profile_id(body['ai_profile_id'])
                validate_profile_revision(body['ai_profile_revision'])
            if 'material_refs' in body:
                validate_refs(body['material_refs'])
        else:
            if (not {'client_id', 'inputs'} <= body.keys() or body.keys() - _FIELDS
                    or type(body['inputs']) is not list or not body['inputs']
                    or any(type(part) is not dict or part.get('type') != 'text'
                           or type(part.get('text')) is not str for part in body['inputs'])):
                _fail('invalid_request')
            client_id = _client(body['client_id'])
            query = '\n'.join(part['text'] for part in body['inputs'])
        if len(query) > 30000:
            _fail('invalid_request')
        query.encode('utf-8')
        digest = request_sha256(body)
        # Only the query is filtered. The original body and its native input
        # remain the caller's exact request, with a separate raw identity.
        if query_filter is not None:
            query = query_filter(query)
        query = memory_query(query)
        query.encode('utf-8')
        return client_id, digest, query, selection
    except (ValueError, TypeError, UnicodeError, NativeSessionStoreError, NativeMaterialError):
        _fail('invalid_request')


def _checked(record):
    """Validate decoded evidence and return an independent bounded copy."""
    try:
        _hash(record['workspace_id']); _client(record['client_id']); _chat(record['chat_id'])
        _hash(record['raw_request_sha256']); _hash(record['query_sha256'])
        created, updated = _timestamp(record['created_at']), _timestamp(record['updated_at'])
        profile = validate_memory_snapshot(record['memory_profile'])
        if (profile['id'] == 'none' or profile['mode'] != 'read_only'
                or len(_json(profile).encode('utf-8')) > MAX_PROFILE_BYTES
                or type(record['state']) is not str or record['state'] not in _STATES or updated < created):
            _fail('invalid_record')
        package = record['memory_inputs']
        if record['state'] == 'ready':
            package = validate_recall_package(package, profile)
            selected_memory_block(package, profile)
            if (record['error'] is not None or package['query_sha256'] != record['query_sha256']
                    or _sha(_json(package)) != _hash(record['memory_inputs_sha256'])):
                _fail('invalid_record')
            _hash(record['memory_connection_fingerprint'])
            if record['derived_inputs_sha256'] is not None:
                _hash(record['derived_inputs_sha256'])
        elif (record['error'] != ('prepare_failed' if record['state'] == 'failed' else None)
                or any(record[key] is not None for key in ('memory_inputs', 'memory_inputs_sha256',
                    'memory_connection_fingerprint', 'derived_inputs_sha256'))
                or record['state'] == 'preparing' and updated != created):
            _fail('invalid_record')
        return {key: value for key, value in dict(record, memory_profile=profile, memory_inputs=package).items()
                if key not in ('created', 'reused')}
    except (NativeMemoryIntentError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        _fail('invalid_record')


def public(record):
    """Safe saved evidence only; readiness is not a native submission receipt."""
    if record is None:
        return None
    record = _checked(record)
    profile = {key: value for key, value in record['memory_profile'].items() if key != 'bank_ref'}
    return {'workspace_id': record['workspace_id'], 'chat_id': record['chat_id'],
        'client_id': record['client_id'], 'request_sha256': record['raw_request_sha256'],
        'query_sha256': record['query_sha256'], 'state': record['state'], 'error': record['error'],
        'memory_profile': profile, 'derived_inputs_sha256': record['derived_inputs_sha256'],
        'recall_info': public_memory_info(record['memory_inputs'], record['memory_profile'])
            if record['state'] == 'ready' else None,
        'created_at': record['created_at']}


def _read_record_in(db, workspace_id, client_id):
    row = db.execute('SELECT * FROM native_memory_intents WHERE workspace_id=? AND client_id=?',
                     (workspace_id, client_id)).fetchone()
    if row is None:
        return None
    try:
        value = dict(row)
        value['memory_profile'] = _decode(value['memory_profile'], MAX_PROFILE_BYTES)
        if value['memory_inputs'] is not None:
            value['memory_inputs'] = _decode(value['memory_inputs'], MAX_PACKAGE_BYTES)
        record = _checked(value)
        if record['workspace_id'] != workspace_id or record['client_id'] != client_id:
            _fail('invalid_record')
        return record
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        _fail('invalid_record')


def frozen_memory_in(db, workspace_id, chat_id, client_id, raw_sha256):
    """Reuse earlier memory admission in this submission's catalogue snapshot."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_memory_intents'").fetchone() is None:
        return None
    record = _read_record_in(db, workspace_id, client_id)
    if record is not None:
        if record['chat_id'] != chat_id or record['raw_request_sha256'] != raw_sha256:
            _fail('intent_conflict')
        if record['state'] != 'ready':
            return None
    return record


class NativeMemoryIntents:
    def __init__(self, path, workspace_id, *, query_filter=None, request_kind='codex'):
        try:
            if (type(request_kind) is not str or request_kind not in ('codex', 'hermes', 'claude', 'pi', 'dsh')
                    or query_filter is not None and not callable(query_filter)):
                _fail('invalid_request')
            self.query_filter = query_filter
            self.request_kind = request_kind
            self.workspace_id = _hash(workspace_id)
            self.path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
            self._check_path()
            with self._db(write=True) as db:
                db.execute(NATIVE_MEMORY_INTENTS_DDL)
        except (OSError, TypeError, ValueError):
            _fail('invalid_record')

    def _check_path(self):
        cloud = Path.home() / 'Library' / 'Mobile Documents'
        if (not self.path.is_file() or self.path.is_symlink()
                or any(parent.is_symlink() for parent in self.path.parents)
                or self.path == cloud or cloud in self.path.parents):
            _fail('invalid_record')

    @contextmanager
    def _db(self, *, write=False):
        self._check_path()
        db = None
        try:
            db = sqlite3.connect('file:' + quote(str(self.path), safe='/') +
                ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA trusted_schema=OFF')
            if not write:
                db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            with db:
                yield db
        except (sqlite3.Error, OSError):
            _fail('invalid_record')
        finally:
            if db is not None:
                db.close()

    def _read_in(self, db, client_id):
        return _read_record_in(db, self.workspace_id, client_id)

    @staticmethod
    def _match(record, chat_id, digest=None):
        if record is not None and (record['chat_id'] != chat_id
                or digest is not None and record['raw_request_sha256'] != digest):
            _fail('intent_conflict')
        return record

    def read(self, chat_id, client_id):
        chat_id, client_id = _chat(chat_id), _client(client_id)
        with self._db() as db:
            return self._match(self._read_in(db, client_id), chat_id)

    def lookup(self, chat_id, body):
        request = _request(body, self.query_filter, self.request_kind)
        if request is None:
            return None
        client_id, digest, _query, _selection = request
        with self._db() as db:
            return self._match(self._read_in(db, client_id), _chat(chat_id), digest)

    @staticmethod
    def _profile_in(db, selection):
        try:
            profile = make_memory_snapshot(MemoryProfiles.get_in(db, selection['id']))
        except KeyError:
            _fail('profile_not_found')
        except ValueError:
            _fail('invalid_record')
        if profile['revision'] != selection['revision']:
            _fail('profile_conflict')
        if profile['mode'] != 'read_only':
            _fail('profile_read_only_required')
        return profile

    def _failed(self, chat_id, client_id, digest):
        with self._db(write=True) as db:
            record = self._match(self._read_in(db, client_id), chat_id, digest)
            if record is not None and record['state'] == 'preparing':
                db.execute("UPDATE native_memory_intents SET state='failed',error='prepare_failed',updated_at=? "
                           'WHERE workspace_id=? AND client_id=?', (_stamp(), self.workspace_id, client_id))

    def prepare(self, chat_id, body, recaller):
        request = _request(body, self.query_filter, self.request_kind)
        if request is None:
            return None
        chat_id = _chat(chat_id)
        client_id, digest, query, selection = request
        with self._db(write=True) as db:
            prior = self._match(self._read_in(db, client_id), chat_id, digest)
            if prior is not None:
                return dict(prior, created=False, reused=True)
            if self.request_kind in ('claude', 'dsh'):
                profile_intent = None
                if self.request_kind == 'claude':
                    from native_profile_intents import frozen_claude_profile_in
                    profile_intent = frozen_claude_profile_in(db, self.workspace_id, chat_id, client_id, digest)
                if profile_intent is None:
                    assert_memory_selection_in(db, self.workspace_id, self.request_kind, chat_id,
                                               selection['id'], selection['revision'])
            if not callable(recaller):
                _fail('invalid_request')
            profile = self._profile_in(db, selection)
            stamp = _stamp()
            db.execute('''INSERT INTO native_memory_intents VALUES(?,?,?,?,?,?,'preparing',NULL,
                NULL,NULL,NULL,NULL,?,?)''',
                (self.workspace_id, client_id, chat_id, digest, _sha(query), _json(profile), stamp, stamp))
        try:
            # No SQLite transaction survives the reservation. The callback owns
            # source identity checks; it cannot authorize a native submission.
            result = recaller(dict(profile), query)
            if type(result) is not tuple or len(result) != 2:
                _fail('prepare_failed')
            package = validate_recall_package(result[0], profile, query=query)
            selected_memory_block(package, profile)
            fingerprint = _hash(result[1])
            encoded = _json(package)
            with self._db(write=True) as db:
                record = self._match(self._read_in(db, client_id), chat_id, digest)
                if record is None or record['state'] != 'preparing':
                    _fail('intent_conflict')
                if self._profile_in(db, selection) != profile:
                    _fail('profile_conflict')
                db.execute("UPDATE native_memory_intents SET state='ready',memory_inputs=?,memory_inputs_sha256=?,"
                    'memory_connection_fingerprint=?,updated_at=? WHERE workspace_id=? AND client_id=?',
                    (encoded, _sha(encoded), fingerprint, _stamp(), self.workspace_id, client_id))
                return dict(self._read_in(db, client_id), created=True, reused=False)
        except Exception as error:
            self._failed(chat_id, client_id, digest)
            code = error.code if isinstance(error, NativeMemoryIntentError) and error.code in (
                'profile_conflict', 'profile_not_found', 'profile_read_only_required') else 'prepare_failed'
            raise NativeMemoryIntentError(code) from None

    def bind_inputs(self, chat_id, body, inputs):
        if self.request_kind != 'codex':
            _fail('invalid_request')
        request = _request(body, self.query_filter, self.request_kind)
        if request is None:
            return None
        try:
            if type(inputs) is not list or not inputs or any(type(part) is not dict for part in inputs):
                _fail('invalid_request')
            if len(_json(inputs).encode('utf-8')) > MAX_INPUT_BYTES:
                _fail('invalid_request')
            digest = request_sha256({'input': inputs})
        except (ValueError, TypeError, NativeSessionStoreError):
            _fail('invalid_request')
        return self._bind_digest(chat_id, request, digest)

    def bind_hermes_prompt(self, chat_id, body, prompt):
        """Bind Hermes' logical prompt (including images), never RPC parameters.

        The holder validates material ownership and native image staging. This
        codec only freezes the final bounded request hash, without its contents.
        """
        if self.request_kind != 'hermes':
            _fail('invalid_request')
        request = _request(body, self.query_filter, self.request_kind)
        if request is None:
            return None
        try:
            if (type(prompt) is not dict or not {'type', 'message'} <= prompt.keys()
                    or prompt.keys() - {'type', 'message', 'images'}
                    or prompt['type'] != 'prompt' or type(prompt['message']) is not str
                    or not prompt['message'].strip()
                    or 'images' in prompt and (type(prompt['images']) is not list
                        or any(type(image) is not dict for image in prompt['images']))
                    or len(_json(prompt).encode('utf-8')) > MAX_INPUT_BYTES):
                _fail('invalid_request')
            digest = request_sha256(prompt)
        except (ValueError, TypeError, NativeSessionStoreError, RecursionError):
            _fail('invalid_request')
        return self._bind_digest(chat_id, request, digest)

    def bind_pi_request(self, chat_id, body, prompt):
        """Freeze Pi's exact text/base64-image request using Pi's own budget."""
        if self.request_kind != 'pi':
            _fail('invalid_request')
        raw = _request(body, self.query_filter, self.request_kind)
        if raw is None:
            return None
        from rabbit_runtime.pi.material_protocol import request_sha256 as pi_sha256, PiMaterialProtocolError
        try:
            if (type(prompt) is not dict or not {'type','message'} <= prompt.keys()
                    or prompt.keys() - {'type','message','images'} or prompt['type'] != 'prompt'
                    or type(prompt['message']) is not str or not prompt['message'].strip()
                    or len(_json([{'type':'text','text':prompt['message']}]).encode()) > MAX_INPUT_BYTES
                    or 'images' in prompt and (type(prompt['images']) is not list or any(
                        type(image) is not dict or set(image) != {'type','data','mimeType'} or image['type'] != 'image'
                        or type(image['data']) is not str or not image['data']
                        or image['mimeType'] not in ('image/png','image/jpeg') for image in prompt['images']))):
                _fail('invalid_request')
            digest = pi_sha256(prompt)
        except (ValueError, TypeError, UnicodeError, RecursionError, PiMaterialProtocolError):
            _fail('invalid_request')
        return self._bind_digest(chat_id, raw, digest)

    def bind_dsh_request(self, chat_id, body, prompt):
        """Freeze DSH's exact logical text request; never retain its body."""
        if self.request_kind != 'dsh':
            _fail('invalid_request')
        raw = _request(body, self.query_filter, self.request_kind)
        if raw is None:
            return None
        try:
            if (type(prompt) is not dict or set(prompt) != {'type', 'message'}
                    or prompt['type'] != 'prompt' or type(prompt['message']) is not str
                    or not prompt['message'].strip() or len(_json(prompt).encode('utf-8')) > MAX_INPUT_BYTES):
                _fail('invalid_request')
            digest = request_sha256(prompt)
        except (ValueError, TypeError, UnicodeError, RecursionError, NativeSessionStoreError):
            _fail('invalid_request')
        return self._bind_digest(chat_id, raw, digest)

    def bind_claude_request(self, chat_id, body, request):
        """Bind the original user block plus host context and image metadata.

        Claude's existing protocol owns combined text/image bounds. Only its
        exact small request is hashed here; no text or image bodies are stored.
        """
        if self.request_kind != 'claude':
            _fail('invalid_request')
        raw = _request(body, self.query_filter, self.request_kind)
        if raw is None:
            return None
        from rabbit_runtime.claude.material_protocol import ClaudeMaterialProtocolError, request as claude_request
        try:
            if (type(request) is not dict or not {'type', 'message', 'context_text'} <= request.keys()
                    or request.keys() - {'type', 'message', 'context_text', 'images'}
                    or request['type'] != 'prompt' or request['message'] != body['message']):
                _fail('invalid_request')
            canonical = claude_request(request['message'], request['context_text'], request.get('images', []))
            if canonical != request:
                _fail('invalid_request')
            digest = request_sha256(canonical)
        except (ValueError, TypeError, NativeSessionStoreError, ClaudeMaterialProtocolError, RecursionError):
            _fail('invalid_request')
        return self._bind_digest(chat_id, raw, digest)

    def _bind_digest(self, chat_id, request, digest):
        client_id, raw_sha, _query, _selection = request
        chat_id = _chat(chat_id)
        with self._db(write=True) as db:
            record = self._match(self._read_in(db, client_id), chat_id, raw_sha)
            if record is None:
                _fail('intent_not_found')
            if record['state'] != 'ready':
                _fail('preparation_not_ready')
            if record['derived_inputs_sha256'] not in (None, digest):
                _fail('intent_conflict')
            if record['derived_inputs_sha256'] is None:
                db.execute('UPDATE native_memory_intents SET derived_inputs_sha256=?,updated_at=? '
                    'WHERE workspace_id=? AND client_id=?', (digest, _stamp(), self.workspace_id, client_id))
            return self._read_in(db, client_id)

    def list_for_chat(self, chat_id, client_ids):
        chat_id = _chat(chat_id)
        if (type(client_ids) not in (list, tuple) or len(client_ids) > MAX_LIST_CLIENTS
                or len(set(_client(value) for value in client_ids)) != len(client_ids)):
            _fail('invalid_request')
        with self._db() as db:
            records = [self._read_in(db, client_id) for client_id in client_ids]
        return [record for record in records if record is not None and record['chat_id'] == chat_id]

    def recent(self, chat_id, *, limit=64):
        """Bounded saved metadata for one trusted owner; never recalls or writes."""
        chat_id = _chat(chat_id)
        if type(limit) is not int or not 1 <= limit <= MAX_LIST_CLIENTS:
            _fail('invalid_request')
        with self._db() as db:
            rows = db.execute('SELECT client_id FROM native_memory_intents WHERE workspace_id=? AND chat_id=? '
                'ORDER BY created_at DESC,client_id DESC LIMIT ?', (self.workspace_id, chat_id, limit + 1)).fetchall()
            records = [self._match(self._read_in(db, row['client_id']), chat_id) for row in rows[:limit]]
        return {'records': records, 'has_more': len(rows) > limit}

    public = staticmethod(public)


