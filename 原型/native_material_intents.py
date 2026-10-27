"""Small native material identities in the existing application catalogue.

Only references and two hashes are retained. This module never resolves a
material, stores a prompt/image body, imports a native runtime, or sends an RPC.
An intent is not a submission receipt or permission to repeat a native query.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
from urllib.parse import quote
import uuid
import weakref

from native_materials import NativeMaterialError, validate_refs
from rabbit_runtime.native_session_store import NativeSessionStoreError, request_sha256
from native_memory_intents import validate_selection, NativeMemoryIntentError, frozen_memory_in
from native_memory_selection import assert_memory_selection_in
from profiles import validate_profile_id, validate_profile_revision


SCHEMA_VERSION = 1
MAX_MESSAGE_BYTES = 128 * 1024
MAX_REFS_BYTES = 4096
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_TABLES = {'claude': 'claude_material_intents', 'dsh': 'dsh_material_intents'}
_LABELS = {'claude': 'Claude', 'dsh': 'DSH'}
_FIELDS = frozenset(('client_id', 'message', 'material_refs'))
_MEMORY_FIELDS = frozenset(('memory_profile_id', 'memory_profile_revision'))
_AI_FIELDS = frozenset(('ai_profile_id', 'ai_profile_revision'))
_LOCKS_GUARD = threading.Lock()
_LOCKS = weakref.WeakValueDictionary()
_ERRORS = {
    'invalid_request': '{label} 材料意图请求格式无效。',
    'intent_conflict': '这个发送编号已有不同对话、内容或材料，请核对原回执。',
    'intent_not_found': '找不到这次 {label} 材料意图。',
    'invalid_record': '{label} 材料意图记录或目录身份无法核验。',
}
CLAUDE_MATERIAL_INTENTS_DDL = '''CREATE TABLE claude_material_intents (
    schema_version INTEGER NOT NULL CHECK(schema_version=1),
    workspace_id TEXT NOT NULL CHECK(length(workspace_id)=64),
    chat_id TEXT NOT NULL, client_id TEXT NOT NULL,
    raw_request_sha256 TEXT NOT NULL CHECK(length(raw_request_sha256)=64),
    material_refs TEXT NOT NULL,
    request_sha256 TEXT CHECK(request_sha256 IS NULL OR length(request_sha256)=64),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(workspace_id,client_id))'''


class NativeMaterialIntentError(ValueError):
    def __init__(self, code, *, agent='claude'):
        self.code = code if code in _ERRORS else 'invalid_request'
        self.message = _ERRORS[self.code].format(label=_LABELS.get(agent, 'Claude'))
        super().__init__(self.message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _identity(info):
    return info.st_dev, info.st_ino


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _schema(db, table):
    # Names come only from the fixed internal descriptors. Other catalogue
    # tables belong to their owners; extra indexes/triggers on ours fail closed.
    return tuple(tuple(row) for row in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_master '
        'WHERE name=? OR tbl_name=? ORDER BY type,name', (table, table)))


DSH_MATERIAL_INTENTS_DDL = CLAUDE_MATERIAL_INTENTS_DDL.replace(
    'CREATE TABLE claude_material_intents (', 'CREATE TABLE dsh_material_intents (', 1)


def _expected_schema(table, ddl):
    db = sqlite3.connect(':memory:')
    try:
        db.execute(ddl)
        return _schema(db, table)
    finally:
        db.close()


_DESCRIPTORS = {
    'claude': (CLAUDE_MATERIAL_INTENTS_DDL,
               _expected_schema(_TABLES['claude'], CLAUDE_MATERIAL_INTENTS_DDL)),
    'dsh': (DSH_MATERIAL_INTENTS_DDL,
            _expected_schema(_TABLES['dsh'], DSH_MATERIAL_INTENTS_DDL)),
}


class NativeMaterialIntents:
    """Freeze bounded HTTP identity before source access, without sending.

    ``freeze`` alone adds the transient ``created`` field. Reads and same-value
    confirmations use actual mode=ro connections and never refresh timestamps.
    """
    def _fail(self, code):
        raise NativeMaterialIntentError(code, agent=self.agent)

    def _uuid(self, value):
        try:
            if type(value) is not str or str(uuid.UUID(value)) != value:
                raise ValueError
        except (ValueError, AttributeError):
            self._fail('invalid_request')
        return value

    def _sha(self, value):
        if type(value) is not str or not _SHA.fullmatch(value):
            self._fail('invalid_request')
        return value

    def _regular(self, info):
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            self._fail('invalid_record')

    def _timestamp(self, value):
        if type(value) is not str or not 1 <= len(value) <= 64:
            self._fail('invalid_record')
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo != timezone.utc or parsed.isoformat(timespec='microseconds') != value:
                self._fail('invalid_record')
        except ValueError:
            self._fail('invalid_record')
        return value

    def _unique_object(self, pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                self._fail('invalid_record')
            result[key] = value
        return result

    def _refs(self, value):
        refs = validate_refs(value)
        if self.agent == 'dsh' and any(ref['kind'] != 'text' for ref in refs):
            self._fail('invalid_request')
        return refs

    def __init__(self, path, *, workspace_id, agent='claude'):
        if type(agent) is not str or agent not in _DESCRIPTORS:
            raise NativeMaterialIntentError('invalid_request')
        self.agent = agent
        self._table = _TABLES[agent]
        self._ddl, self._expected_schema = _DESCRIPTORS[agent]
        self.workspace_id = self._sha(workspace_id)
        try:
            raw = os.fspath(path)
            if (type(raw) is not str or not raw or not os.path.isabs(raw)
                    or raw != os.path.abspath(raw) or '\x00' in raw):
                self._fail('invalid_request')
            raw.encode('utf-8')
        except (TypeError, ValueError, UnicodeError):
            self._fail('invalid_request')
        self.path = Path(raw)
        if not self.path.name:
            self._fail('invalid_request')
        self.lock = threading.RLock()
        self._descriptors, self._anchors = [], []
        self._closed = False
        try:
            parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self._descriptors.append(parent)
            for part in self.path.parent.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                self._descriptors.append(child)
                self._anchors.append((parent, part, child))
                parent = child
            self._parent_fd = parent
            descriptor = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            self._descriptors.append(descriptor)
            self._file_identity = _identity(os.fstat(descriptor))
            # POSIX closes can release the process's SQLite file locks. Share
            # a lock across this module's instances, including descriptor close.
            with _LOCKS_GUARD:
                shared_lock = _LOCKS.get(self._file_identity)
                if shared_lock is None:
                    shared_lock = threading.RLock()
                    _LOCKS[self._file_identity] = shared_lock
                self.lock = shared_lock
            self._regular(os.fstat(descriptor))
            # A valid existing table is opened entirely read-only. An absent
            # file is never created, nor is another owner's schema rewritten.
            with self._db(allow_missing=True) as db:
                missing = not _schema(db, self._table)
            if missing:
                with self._db(write=True, allow_missing=True) as db:
                    if not _schema(db, self._table):
                        db.execute(self._ddl)
                    if _schema(db, self._table) != self._expected_schema:
                        self._fail('invalid_record')
        except BaseException as error:
            self.close()
            if isinstance(error, NativeMaterialIntentError):
                raise
            if isinstance(error, (OSError, sqlite3.Error)):
                self._fail('invalid_record')
            raise

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            for descriptor in reversed(self._descriptors):
                os.close(descriptor)
            self._descriptors.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _check_path(self):
        if self._closed:
            self._fail('invalid_record')
        for parent, name, descriptor in self._anchors:
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(named.st_mode) or _identity(named) != _identity(os.fstat(descriptor)):
                self._fail('invalid_record')
        for suffix in ('', '-journal', '-wal', '-shm'):
            try:
                info = os.stat(self.path.name + suffix, dir_fd=self._parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if not suffix:
                    self._fail('invalid_record')
                continue
            self._regular(info)
            if not suffix and _identity(info) != self._file_identity:
                self._fail('invalid_record')

    @contextmanager
    def _db(self, *, write=False, allow_missing=False):
        with self.lock:
            db = None
            try:
                self._check_path()
                db = sqlite3.connect('file:' + quote(str(self.path), safe='/') +
                    ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=15, isolation_level=None)
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA trusted_schema=OFF')
                if not write:
                    db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                schema = _schema(db, self._table)
                if schema != self._expected_schema and not (allow_missing and not schema):
                    self._fail('invalid_record')
                yield db
                self._check_path()
                if _schema(db, self._table) != self._expected_schema and not (allow_missing and not _schema(db, self._table)):
                    self._fail('invalid_record')
                if write:
                    db.commit()
            except NativeMaterialIntentError:
                raise
            except (OSError, sqlite3.Error):
                self._fail('invalid_record')
            finally:
                if db is not None:
                    if db.in_transaction:
                        db.rollback()
                    db.close()

    def _request_identity(self, body):
        allowed = _FIELDS | _MEMORY_FIELDS | (_AI_FIELDS if self.agent == 'claude' else set())
        if (type(body) is not dict or not _FIELDS <= body.keys() or body.keys() - allowed
                or ('ai_profile_id' in body) != ('ai_profile_revision' in body)
                or type(body['message']) is not str or not body['message'].strip()):
            self._fail('invalid_request')
        client_id = self._uuid(body['client_id'])
        try:
            if len(body['message'].encode('utf-8')) > MAX_MESSAGE_BYTES:
                self._fail('invalid_request')
            refs = self._refs(body['material_refs'])
            validate_selection(body)
            if 'ai_profile_id' in body:
                validate_profile_id(body['ai_profile_id'])
                validate_profile_revision(body['ai_profile_revision'])
            if not refs:
                self._fail('invalid_request')
            encoded_refs = _json(refs)
            if len(encoded_refs.encode('utf-8')) > MAX_REFS_BYTES:
                self._fail('invalid_request')
            digest = request_sha256(body)
        except (NativeMaterialError, NativeMemoryIntentError, NativeSessionStoreError, ValueError, TypeError, UnicodeError, RecursionError):
            self._fail('invalid_request')
        return client_id, digest, refs, encoded_refs

    def _read_in(self, db, client_id):
        row = db.execute(f'SELECT * FROM {self._table} WHERE workspace_id=? AND client_id=?',
                         (self.workspace_id, client_id)).fetchone()
        if row is None:
            return None
        try:
            if (type(row['schema_version']) is not int or row['schema_version'] != SCHEMA_VERSION
                    or row['workspace_id'] != self.workspace_id or row['client_id'] != client_id):
                self._fail('invalid_record')
            self._uuid(row['chat_id']); self._uuid(row['client_id'])
            self._sha(row['raw_request_sha256'])
            if row['request_sha256'] is not None:
                self._sha(row['request_sha256'])
            raw = row['material_refs']
            if type(raw) is not str or len(raw.encode('utf-8')) > MAX_REFS_BYTES:
                self._fail('invalid_record')
            refs = self._refs(json.loads(raw, object_pairs_hook=self._unique_object))
            if not refs:
                self._fail('invalid_record')
            created, updated = self._timestamp(row['created_at']), self._timestamp(row['updated_at'])
            if row['request_sha256'] is None and updated != created:
                self._fail('invalid_record')
        except (NativeMaterialIntentError, NativeMaterialError, ValueError, TypeError,
                KeyError, UnicodeError, RecursionError):
            self._fail('invalid_record')
        return {'schema_version': SCHEMA_VERSION, 'workspace_id': self.workspace_id,
                'chat_id': row['chat_id'], 'client_id': client_id,
                'raw_request_sha256': row['raw_request_sha256'], 'material_refs': refs,
                'request_sha256': row['request_sha256'], 'created_at': created, 'updated_at': updated}

    def _match(self, row, chat_id, raw_sha256=None, refs=None):
        if row is not None and (row['chat_id'] != chat_id
                or raw_sha256 is not None and row['raw_request_sha256'] != raw_sha256
                or refs is not None and row['material_refs'] != refs):
            self._fail('intent_conflict')
        return row

    def lookup(self, chat_id, client_id):
        """Read one identity without source access or a write connection."""
        chat_id, client_id = self._uuid(chat_id), self._uuid(client_id)
        with self._db() as db:
            return self._match(self._read_in(db, client_id), chat_id)

    def lookup_body(self, chat_id, body):
        """Verify the original small HTTP request even if its sources vanished."""
        chat_id = self._uuid(chat_id)
        client_id, digest, refs, _encoded = self._request_identity(body)
        with self._db() as db:
            return self._match(self._read_in(db, client_id), chat_id, digest, refs)

    def freeze(self, chat_id, body):
        """Freeze original request identity; an exact replay is read-only."""
        chat_id = self._uuid(chat_id)
        client_id, digest, refs, encoded_refs = self._request_identity(body)
        with self._db() as db:
            old = self._match(self._read_in(db, client_id), chat_id, digest, refs)
            if old is not None:
                return dict(old, created=False)
        with self._db(write=True) as db:
            old = self._match(self._read_in(db, client_id), chat_id, digest, refs)
            if old is not None:
                return dict(old, created=False)
            selection = validate_selection(body)
            profile_intent = None
            if self.agent == 'claude':
                from native_profile_intents import frozen_claude_profile_in
                profile_intent = frozen_claude_profile_in(db, self.workspace_id, chat_id, client_id, digest)
            memory = frozen_memory_in(db, self.workspace_id, chat_id, client_id, digest)
            if memory is None and profile_intent is None:
                assert_memory_selection_in(db, self.workspace_id, self.agent, chat_id,
                    selection['id'] if selection else None, selection['revision'] if selection else None)
            stamp = _stamp()
            db.execute(f'INSERT INTO {self._table} VALUES(?,?,?,?,?,?,?,?,?)',
                (SCHEMA_VERSION, self.workspace_id, chat_id, client_id, digest, encoded_refs, None, stamp, stamp))
            return dict(self._read_in(db, client_id), created=True)

    def bind_request(self, chat_id, client_id, raw_sha256, derived_sha256):
        """Confirm one derived request hash once; a same-value replay is read-only."""
        chat_id, client_id = self._uuid(chat_id), self._uuid(client_id)
        raw_sha256, derived_sha256 = self._sha(raw_sha256), self._sha(derived_sha256)

        def checked(db):
            row = self._match(self._read_in(db, client_id), chat_id, raw_sha256)
            if row is None:
                self._fail('intent_not_found')
            if row['request_sha256'] not in (None, derived_sha256):
                self._fail('intent_conflict')
            return row

        with self._db() as db:
            old = checked(db)
            if old['request_sha256'] is not None:
                return old
        with self._db(write=True) as db:
            old = checked(db)
            if old['request_sha256'] is not None:
                return old
            db.execute(f'UPDATE {self._table} SET request_sha256=?,updated_at=? '
                       'WHERE workspace_id=? AND client_id=? AND request_sha256 IS NULL',
                       (derived_sha256, _stamp(), self.workspace_id, client_id))
            return self._read_in(db, client_id)
