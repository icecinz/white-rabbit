"""Owned Claude identities, conservative receipts and bounded final Results.

``created=True`` is a transient permission to forward a newly committed intent
once. Reloading submitted_unknown never grants another create/query/interrupt.
An SDK acknowledgement is not acceptance; only the exact native user UUID echo
can mark acceptance. A same-process, single-active-query Result may finish that
query, but this ledger makes no claim of durable CLI deduplication or replay.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
from urllib.parse import quote
import uuid

from ..native_session_store import (NativeSessionStoreError, open_directory as _directory, file_identity as _identity,
                                  validate_path as _path, validate_token as _token, request_sha256)


APPLICATION_ID = 0x52434E53  # RCNS: distinct from shared, Codex, Pi and Hermes.
SCHEMA_VERSION = 2
MAX_RESULT_BYTES = 128 * 1024
_MAX_SAVED_BYTES = MAX_RESULT_BYTES * 6 + 4096
PENDING = frozenset(('submitted_unknown', 'accepted'))
TERMINAL = frozenset(('completed', 'failed', 'interrupted'))
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_MESSAGES = {
    'invalid_arguments': 'Claude 会话记录参数无效。',
    'conflict': 'Claude 原生身份或请求不一致，未覆盖原记录。',
    'busy': 'Claude 记录库已有持有者，或原请求仍待确认。',
    'closed': 'Claude 会话记录库已关闭。',
    'readonly': 'Claude 会话记录库为只读。',
    'not_found': '找不到 Claude 会话绑定或请求回执。',
    'scope_changed': 'Claude 记录库、原生文件或工作目录身份变化。',
    'invalid_schema': '这不是受支持的独立 Claude 记录库。',
    'io_failed': 'Claude 会话记录暂时无法读取或保存。',
    'wrong_process': '不能借用其他进程的 Claude 记录库持有权。',
}


class ClaudeStoreError(ValueError):
    def __init__(self, code):
        self.code = code if code in _MESSAGES else 'io_failed'
        super().__init__(_MESSAGES[self.code])


def validate_uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise ClaudeStoreError('invalid_arguments') from None
    return value


def _epoch(value):
    try:
        return _token(value)
    except NativeSessionStoreError:
        raise ClaudeStoreError('invalid_arguments') from None


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


_DDL_V1 = (
    '''CREATE TABLE claude_bindings (
        chat_id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE,
        cwd TEXT NOT NULL, cwd_device INTEGER NOT NULL, cwd_inode INTEGER NOT NULL,
        native_path TEXT, native_device INTEGER, native_inode INTEGER,
        created_at TEXT NOT NULL,
        CHECK((native_path IS NULL)=(native_device IS NULL)),
        CHECK((native_path IS NULL)=(native_inode IS NULL)))''',
    '''CREATE TABLE claude_creations (
        chat_id TEXT PRIMARY KEY REFERENCES claude_bindings(chat_id),
        client_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL, epoch TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('submitted_unknown','confirmed')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
    '''CREATE TABLE claude_prompts (
        client_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES claude_bindings(chat_id),
        session_id TEXT NOT NULL, epoch TEXT NOT NULL,
        request_sha256 TEXT NOT NULL, message_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN
            ('submitted_unknown','accepted','rejected','completed','failed','interrupted')),
        native_user_uuid TEXT, native_result_uuid TEXT UNIQUE,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK(status<>'accepted' OR native_user_uuid IS NOT NULL),
        CHECK((status IN ('completed','failed','interrupted'))=(native_result_uuid IS NOT NULL)))''',
    '''CREATE UNIQUE INDEX claude_one_pending ON claude_prompts(chat_id)
        WHERE status IN ('submitted_unknown','accepted')''',
    '''CREATE TABLE claude_controls (
        client_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES claude_bindings(chat_id),
        session_id TEXT NOT NULL, epoch TEXT NOT NULL,
        active_client_id TEXT NOT NULL REFERENCES claude_prompts(client_id),
        request_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('submitted_unknown','acknowledged')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
    '''CREATE UNIQUE INDEX claude_one_interrupt ON claude_controls(active_client_id)
        WHERE status='submitted_unknown' ''',
)
_RESULT_DDL = (
    'ALTER TABLE claude_prompts ADD COLUMN result_json TEXT',
    'ALTER TABLE claude_prompts ADD COLUMN result_sha256 TEXT',
)
_DDL = _DDL_V1 + _RESULT_DDL
_PROMPT_COLUMNS = ('client_id,chat_id,session_id,epoch,request_sha256,message_sha256,status,'
                   'native_user_uuid,native_result_uuid,created_at,updated_at')
_RESULT_IDENTITY = ('chat_id', 'client_id', 'session_id', 'epoch', 'request_sha256',
                    'message_sha256', 'native_user_uuid', 'native_result_uuid')


def _schema(db):
    return tuple(tuple(row) for row in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))


def _expected_schema(ddl):
    db = sqlite3.connect(':memory:')
    try:
        for sql in ddl:
            db.execute(sql)
        return _schema(db)
    finally:
        db.close()


_EXPECTED_SCHEMA_V1 = _expected_schema(_DDL_V1)
_EXPECTED_SCHEMA = _expected_schema(_DDL)


def _result_record(receipt, result_uuid, record):
    """Accept only the original successful top-level SDK Result, without extras."""
    if (receipt['native_user_uuid'] != receipt['client_id'] or type(record) is not dict
            or record.get('sdk_type') != 'ResultMessage'
            or record.get('display_truncated') is not None and record.get('display_truncated') is not False
            or type(record.get('data')) is not dict):
        return None
    data = record['data']
    origin, text = data.get('origin'), data.get('result')
    if (data.get('uuid') != result_uuid or data.get('session_id') != receipt['session_id']
            or data.get('parent_tool_use_id') is not None
            or data.get('display_truncated') is not None and data.get('display_truncated') is not False
            or origin is not None and (type(origin) is not dict or origin.get('kind') != 'human')
            or data.get('subtype') != 'success' or data.get('is_error') is not False
            or data.get('terminal_reason') not in (None, 'completed') or type(text) is not str):
        return None
    try:
        if not 0 < len(text.encode('utf-8')) <= MAX_RESULT_BYTES:
            return None
    except UnicodeError:
        return None
    return {'sdk_type': 'ResultMessage', 'data': {
        'uuid': result_uuid, 'session_id': receipt['session_id'], 'parent_tool_use_id': None,
        'origin': None if origin is None else {'kind': 'human'}, 'subtype': 'success',
        'is_error': False, 'terminal_reason': data.get('terminal_reason'), 'result': text}}


def _saved_json(receipt, record):
    value = {'identity': {key: receipt[key] for key in _RESULT_IDENTITY}, 'record': record}
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class ClaudeNativeStore:
    """Single local owner; native SDK/file verification remains the caller's job."""
    def __init__(self, path, *, readonly=False):
        if type(readonly) is not bool:
            raise ClaudeStoreError('invalid_arguments')
        try:
            self.path = _path(path)
        except NativeSessionStoreError:
            raise ClaudeStoreError('invalid_arguments') from None
        if not self.path.name:
            raise ClaudeStoreError('invalid_arguments')
        self.readonly, self.lock = readonly, threading.RLock()
        self._closed, self._owner_pid = False, os.getpid()
        self._parent_fd = self._file_fd = self._lock_fd = None
        self._lock_name = '.' + self.path.name + '.lock'
        created = False
        try:
            self._parent_fd = _directory(self.path.parent)
            self._parent_identity = _identity(os.fstat(self._parent_fd))
            flags = (os.O_RDONLY if readonly else os.O_RDWR | os.O_CREAT) | os.O_NOFOLLOW | os.O_NONBLOCK
            self._lock_fd = os.open(self._lock_name, flags, 0o600, dir_fd=self._parent_fd)
            self._regular(os.fstat(self._lock_fd))
            self._lock_identity = _identity(os.fstat(self._lock_fd))
            if not readonly:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                        raise ClaudeStoreError('busy') from None
                    raise
                try:
                    self._file_fd = os.open(self.path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                            0o600, dir_fd=self._parent_fd)
                    created = True
                except FileExistsError:
                    pass
            if self._file_fd is None:
                self._file_fd = os.open(self.path.name, (os.O_RDONLY if readonly else os.O_RDWR) |
                                        os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._parent_fd)
            self._regular(os.fstat(self._file_fd))
            self._file_identity = _identity(os.fstat(self._file_fd))
            with self._db(write=not readonly, initialize=created, migrate=not readonly):
                pass
        except BaseException as error:
            self.close()
            if isinstance(error, ClaudeStoreError):
                raise
            if isinstance(error, sqlite3.Error):
                raise ClaudeStoreError('invalid_schema') from None
            if isinstance(error, (OSError, NativeSessionStoreError)):
                raise ClaudeStoreError('scope_changed') from None
            raise

    @staticmethod
    def _regular(info):
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ClaudeStoreError('scope_changed')

    def maintenance_state(self):
        """Read the existing three bounded counts without opening native history."""
        from ..maintenance import count_rows
        specs = {'pending_creations':('claude_creations', "status='submitted_unknown'", ()),
            'pending_prompts':('claude_prompts', "status IN ('submitted_unknown','accepted')", ()),
            'unknown_controls':('claude_controls', "status='submitted_unknown'", ())}
        return count_rows(self.path, specs, lock=self.lock, expected_identity=self._file_identity,
            check_path=self._check_path)

    def _check_path(self):
        if self._closed:
            raise ClaudeStoreError('closed')
        if self._owner_pid != os.getpid():
            raise ClaudeStoreError('wrong_process')
        parent = _directory(self.path.parent)
        try:
            if _identity(os.fstat(parent)) != self._parent_identity:
                raise ClaudeStoreError('scope_changed')
        finally:
            os.close(parent)
        for name in (self.path.name, self._lock_name, self.path.name+'-journal',
                     self.path.name+'-wal', self.path.name+'-shm'):
            try:
                info = os.stat(name, dir_fd=self._parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if name in (self.path.name, self._lock_name):
                    raise ClaudeStoreError('scope_changed') from None
                continue
            self._regular(info)
            expected = self._file_identity if name == self.path.name else self._lock_identity if name == self._lock_name else None
            if expected is not None and _identity(info) != expected:
                raise ClaudeStoreError('scope_changed')

    @contextmanager
    def _db(self, *, write=False, initialize=False, migrate=False):
        with self.lock:
            if write and self.readonly:
                raise ClaudeStoreError('readonly')
            db = None
            try:
                self._check_path()
                db = sqlite3.connect('file:' + quote(str(self.path), safe='/') + ('?mode=rw' if write else '?mode=ro'),
                                     uri=True, timeout=10, isolation_level=None)
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('PRAGMA trusted_schema=OFF')
                if not write:
                    db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                if initialize:
                    if _schema(db):
                        raise ClaudeStoreError('invalid_schema')
                    for sql in _DDL:
                        db.execute(sql)
                    db.execute('PRAGMA application_id=' + str(APPLICATION_ID))
                    db.execute('PRAGMA user_version=' + str(SCHEMA_VERSION))
                else:
                    version = db.execute('PRAGMA user_version').fetchone()[0]
                    expected = {1: _EXPECTED_SCHEMA_V1, 2: _EXPECTED_SCHEMA}.get(version)
                    if (db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                            or expected is None or _schema(db) != expected):
                        raise ClaudeStoreError('invalid_schema')
                    if version == 1 and write:
                        if not migrate:
                            raise ClaudeStoreError('invalid_schema')
                        for sql in _RESULT_DDL:
                            db.execute(sql)
                        db.execute('PRAGMA user_version=' + str(SCHEMA_VERSION))
                        if _schema(db) != _EXPECTED_SCHEMA:
                            raise ClaudeStoreError('invalid_schema')
                yield db
                self._check_path()
                if write:
                    db.commit()
            except ClaudeStoreError:
                raise
            except sqlite3.IntegrityError:
                raise ClaudeStoreError('conflict') from None
            except sqlite3.Error:
                raise ClaudeStoreError('io_failed') from None
            except (OSError, NativeSessionStoreError):
                raise ClaudeStoreError('scope_changed') from None
            finally:
                if db is not None:
                    if db.in_transaction:
                        db.rollback()
                    db.close()

    @classmethod
    def _file(cls, path, identity):
        parent = _directory(path.parent)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                info = os.fstat(fd)
                cls._regular(info)
                if _identity(info) != identity:
                    raise ClaudeStoreError('scope_changed')
            finally:
                os.close(fd)
        finally:
            os.close(parent)

    @classmethod
    def _binding(cls, db, chat_id):
        row = db.execute('SELECT * FROM claude_bindings WHERE chat_id=?', (chat_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        validate_uuid(value['chat_id']); validate_uuid(value['session_id'])
        fd = _directory(_path(value['cwd']))
        try:
            if _identity(os.fstat(fd)) != (value['cwd_device'], value['cwd_inode']):
                raise ClaudeStoreError('scope_changed')
        finally:
            os.close(fd)
        path, device, inode = (value.pop(key) for key in ('native_path', 'native_device', 'native_inode'))
        value['native_file'] = None
        if path is not None:
            path = _path(path)
            if path.name != value['session_id'] + '.jsonl':
                raise ClaudeStoreError('invalid_schema')
            cls._file(path, (device, inode))
            value['native_file'] = {key: value[key] for key in
                                    ('session_id', 'cwd', 'cwd_device', 'cwd_inode')}
            value['native_file'].update(path=str(path), device=device, inode=inode)
        return value

    @staticmethod
    def _receipt(db, table, key, *, field='client_id'):
        # Table/column names are only private fixed literals from methods below.
        columns = _PROMPT_COLUMNS if table == 'claude_prompts' else '*'
        row = db.execute('SELECT ' + columns + ' FROM ' + table + ' WHERE ' + field + '=?', (key,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for name in ('client_id', 'chat_id', 'session_id'):
            validate_uuid(result[name])
        _epoch(result['epoch'])
        for name in ('request_sha256', 'message_sha256'):
            if name in result and not _SHA.fullmatch(result[name]):
                raise ClaudeStoreError('invalid_schema')
        if table == 'claude_prompts':
            result['kind'] = 'prompt'
        elif table == 'claude_controls':
            result['kind'] = 'interrupt'
        return result

    def bind(self, chat_id, cwd, session_id=None):
        """Reserve an immutable native UUID before any original create process."""
        chat_id = validate_uuid(chat_id)
        if session_id is not None:
            session_id = validate_uuid(session_id)
        try:
            cwd = _path(cwd)
            fd = _directory(cwd)
            try:
                identity = _identity(os.fstat(fd))
            finally:
                os.close(fd)
        except NativeSessionStoreError:
            raise ClaudeStoreError('invalid_arguments') from None
        except OSError:
            raise ClaudeStoreError('scope_changed') from None
        with self._db(write=True) as db:
            old = self._binding(db, chat_id)
            if old:
                if ((old['cwd'], old['cwd_device'], old['cwd_inode']) != (str(cwd), *identity)
                        or session_id is not None and old['session_id'] != session_id):
                    raise ClaudeStoreError('conflict')
                return old
            session_id = session_id or str(uuid.uuid4())
            db.execute('INSERT INTO claude_bindings VALUES(?,?,?,?,?,NULL,NULL,NULL,?)',
                       (chat_id, session_id, str(cwd), *identity, _stamp()))
            return self._binding(db, chat_id)

    def binding(self, chat_id):
        with self._db() as db:
            return self._binding(db, validate_uuid(chat_id))

    def bindings(self):
        with self._db() as db:
            return [self._binding(db, row[0]) for row in
                    db.execute('SELECT chat_id FROM claude_bindings ORDER BY created_at,chat_id').fetchall()]

    def sources(self):
        """Reserved owned IDs, including creating; not file-existence evidence."""
        with self._db() as db:
            return [('claude', validate_uuid(row[0])) for row in
                    db.execute('SELECT session_id FROM claude_bindings ORDER BY session_id')]

    def source_bindings(self, *, include_activity=False):
        """All persisted native owners, including unpublished; never open files."""
        if type(include_activity) is not bool:
            raise ClaudeStoreError('invalid_arguments')
        with self._db() as db:
            activity = ',a.activity_at' if include_activity else ''
            join = (" LEFT JOIN (SELECT chat_id,MAX(updated_at) activity_at FROM claude_prompts "
                    "WHERE status IN ('accepted','completed','failed','interrupted') GROUP BY chat_id) "
                    "a ON a.chat_id=b.chat_id") if include_activity else ''
            return [{'chat_id': validate_uuid(row['chat_id']), 'session_id': validate_uuid(row['session_id']),
                     **({'activity_at': row['activity_at']} if include_activity else {})}
                    for row in db.execute('SELECT b.chat_id,b.session_id' + activity + ' FROM claude_bindings b' + join +
                        ' ORDER BY b.session_id')]

    def adopt(self, chat_id, session_id, cwd, descriptor):
        """Anchor a proven existing file atomically, without a creation receipt."""
        chat_id, session_id = validate_uuid(chat_id), validate_uuid(session_id)
        required = {'session_id','cwd','path','device','inode','cwd_device','cwd_inode'}
        if (type(descriptor) is not dict or not required <= descriptor.keys()
                or descriptor.keys() - required - {'size','mtime_ns'}
                or any(type(descriptor[key]) is not int or descriptor[key] < 0
                    for key in ('device','inode','cwd_device','cwd_inode'))):
            raise ClaudeStoreError('invalid_arguments')
        try:
            cwd, path = _path(cwd), _path(descriptor['path'])
            if (descriptor['session_id'] != session_id or descriptor['cwd'] != str(cwd)
                    or path.name != session_id+'.jsonl'):
                raise ClaudeStoreError('conflict')
            fd = _directory(cwd)
            try: cwd_identity = _identity(os.fstat(fd))
            finally: os.close(fd)
            if cwd_identity != (descriptor['cwd_device'],descriptor['cwd_inode']):
                raise ClaudeStoreError('scope_changed')
            self._file(path,(descriptor['device'],descriptor['inode']))
            expected = {key:descriptor[key] for key in required}
            with self._db(write=True) as db:
                old = self._binding(db,chat_id)
                if any(db.execute('SELECT 1 FROM '+table+' WHERE chat_id=?',(chat_id,)).fetchone()
                        for table in ('claude_creations','claude_prompts','claude_controls')):
                    raise ClaudeStoreError('conflict')
                if old is not None:
                    if old['session_id'] != session_id or old['native_file'] != expected:
                        raise ClaudeStoreError('conflict')
                else:
                    db.execute('INSERT INTO claude_bindings VALUES(?,?,?,?,?,?,?,?,?)',
                        (chat_id,session_id,str(cwd),*cwd_identity,str(path),descriptor['device'],descriptor['inode'],_stamp()))
                return self._binding(db,chat_id)
        except ClaudeStoreError:
            raise
        except NativeSessionStoreError:
            raise ClaudeStoreError('invalid_arguments') from None
        except OSError:
            raise ClaudeStoreError('scope_changed') from None

    def attach_file(self, chat_id, descriptor):
        """Caller obtained this finite identity from the exact native file helper."""
        chat_id = validate_uuid(chat_id)
        required = {'session_id', 'cwd', 'path', 'device', 'inode', 'cwd_device', 'cwd_inode'}
        if (type(descriptor) is not dict or not required <= descriptor.keys()
                or descriptor.keys() - required - {'size', 'mtime_ns'}
                or any(type(descriptor[key]) is not int or descriptor[key] < 0
                       for key in ('device', 'inode', 'cwd_device', 'cwd_inode'))):
            raise ClaudeStoreError('invalid_arguments')
        validate_uuid(descriptor['session_id'])
        try:
            path = _path(descriptor['path'])
        except NativeSessionStoreError:
            raise ClaudeStoreError('invalid_arguments') from None
        with self._db(write=True) as db:
            binding = self._binding(db, chat_id)
            if binding is None:
                raise ClaudeStoreError('not_found')
            if (any(binding[key] != descriptor[key] for key in
                    ('session_id', 'cwd', 'cwd_device', 'cwd_inode'))
                    or path.name != binding['session_id'] + '.jsonl'):
                raise ClaudeStoreError('conflict')
            self._file(path, (descriptor['device'], descriptor['inode']))
            expected = {key: descriptor[key] for key in required}
            if binding['native_file'] is not None and binding['native_file'] != expected:
                raise ClaudeStoreError('conflict')
            db.execute('UPDATE claude_bindings SET native_path=?,native_device=?,native_inode=? WHERE chat_id=?',
                       (str(path), descriptor['device'], descriptor['inode'], chat_id))
            return self._binding(db, chat_id)

    def begin_create(self, chat_id, client_id, epoch):
        chat_id, client_id, epoch = validate_uuid(chat_id), validate_uuid(client_id), _epoch(epoch)
        with self._db(write=True) as db:
            binding = self._binding(db, chat_id)
            if binding is None:
                raise ClaudeStoreError('not_found')
            old = self._receipt(db, 'claude_creations', chat_id, field='chat_id')
            if old:
                if old['client_id'] != client_id:
                    raise ClaudeStoreError('conflict')
                return dict(old, created=False)
            if binding['native_file'] is not None:
                # Existing imported files resume; they never become a new
                # native creation merely to satisfy an application receipt.
                raise ClaudeStoreError('conflict')
            now = _stamp()
            db.execute("INSERT INTO claude_creations VALUES(?,?,?,?,'submitted_unknown',?,?)",
                       (chat_id, client_id, binding['session_id'], epoch, now, now))
            return dict(self._receipt(db, 'claude_creations', chat_id, field='chat_id'), created=True)

    def creation(self, chat_id):
        with self._db() as db:
            self._binding(db, validate_uuid(chat_id))
            return self._receipt(db, 'claude_creations', chat_id, field='chat_id')

    def confirm_create(self, chat_id, client_id, session_id, epoch):
        """Original init and saved file proof are both required by the caller."""
        chat_id, client_id, session_id, epoch = validate_uuid(chat_id), validate_uuid(client_id), validate_uuid(session_id), _epoch(epoch)
        with self._db(write=True) as db:
            binding = self._binding(db, chat_id)
            old = self._receipt(db, 'claude_creations', chat_id, field='chat_id')
            if binding is None or old is None:
                raise ClaudeStoreError('not_found')
            if (binding['native_file'] is None or (old['client_id'], old['session_id'], old['epoch'])
                    != (client_id, session_id, epoch)):
                raise ClaudeStoreError('conflict')
            db.execute("UPDATE claude_creations SET status='confirmed',updated_at=? WHERE chat_id=?",
                       (_stamp(), chat_id))
            return self._receipt(db, 'claude_creations', chat_id, field='chat_id')

    def begin(self, chat_id, client_id, message, epoch, *, request=None):
        """Commit before SDK.query; the original UTF-8 text never enters SQLite."""
        chat_id, client_id, epoch = validate_uuid(chat_id), validate_uuid(client_id), _epoch(epoch)
        if type(message) is not str or not message or len(message) > 1024 * 1024:
            raise ClaudeStoreError('invalid_arguments')
        try:
            if request is None:
                request = {'type': 'prompt', 'message': message}
            else:
                from .material_protocol import request as material_request
                if (type(request) is not dict
                        or material_request(message, request.get('context_text'), request.get('images', [])) != request):
                    raise ClaudeStoreError('invalid_arguments')
            digest = request_sha256(request)
            message_digest = hashlib.sha256(message.encode('utf-8')).hexdigest()
        except (NativeSessionStoreError, UnicodeError, ValueError, TypeError):
            raise ClaudeStoreError('invalid_arguments') from None
        with self._db(write=True) as db:
            binding = self._binding(db, chat_id)
            if binding is None:
                raise ClaudeStoreError('not_found')
            old = self._receipt(db, 'claude_prompts', client_id)
            if old:
                if (old['chat_id'], old['request_sha256'], old['message_sha256']) != (chat_id, digest, message_digest):
                    raise ClaudeStoreError('conflict')
                return dict(old, created=False)
            creation = self._receipt(db, 'claude_creations', chat_id, field='chat_id')
            if creation is None:
                if binding['native_file'] is None:
                    raise ClaudeStoreError('conflict')
            elif creation['status'] != 'confirmed' and creation['epoch'] != epoch:
                raise ClaudeStoreError('conflict')
            if self._active(db, chat_id) is not None:
                raise ClaudeStoreError('busy')
            now = _stamp()
            db.execute("INSERT INTO claude_prompts (" + _PROMPT_COLUMNS + ") VALUES(?,?,?,?,?,?,'submitted_unknown',NULL,NULL,?,?)",
                       (client_id, chat_id, binding['session_id'], epoch, digest, message_digest, now, now))
            return dict(self._receipt(db, 'claude_prompts', client_id), created=True)

    @classmethod
    def _active(cls, db, chat_id):
        row = db.execute("SELECT client_id FROM claude_prompts WHERE chat_id=? AND status IN ('submitted_unknown','accepted')",
                         (chat_id,)).fetchone()
        return cls._receipt(db, 'claude_prompts', row[0]) if row else None

    def active_for_chat(self, chat_id):
        with self._db() as db:
            self._binding(db, validate_uuid(chat_id))
            return self._active(db, chat_id)

    def lookup(self, client_id):
        with self._db() as db:
            receipt = self._receipt(db, 'claude_prompts', validate_uuid(client_id))
            if receipt:
                self._binding(db, receipt['chat_id'])
            return receipt

    operation = lookup

    def operations(self, chat_id, limit=20):
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ClaudeStoreError('invalid_arguments')
        with self._db() as db:
            chat_id = validate_uuid(chat_id)
            if self._binding(db, chat_id) is None:
                raise ClaudeStoreError('not_found')
            ids = db.execute('SELECT client_id FROM claude_prompts WHERE chat_id=? '
                             'ORDER BY created_at DESC,client_id LIMIT ?', (chat_id, limit)).fetchall()
            return [self._receipt(db, 'claude_prompts', row[0]) for row in ids]

    def _target(self, db, client_id, session_id, epoch, *, table='claude_prompts'):
        old = self._receipt(db, table, validate_uuid(client_id))
        if old is None:
            raise ClaudeStoreError('not_found')
        binding = self._binding(db, old['chat_id'])
        if (old['session_id'], old['epoch']) != (validate_uuid(session_id), _epoch(epoch)) or binding['session_id'] != session_id:
            raise ClaudeStoreError('conflict')
        return old

    def accepted(self, client_id, session_id, epoch, native_user_uuid):
        """Exact native UUID replay plus original session observation, not ACK."""
        if validate_uuid(native_user_uuid) != validate_uuid(client_id):
            raise ClaudeStoreError('conflict')
        with self._db(write=True) as db:
            old = self._target(db, client_id, session_id, epoch)
            if old['native_user_uuid'] == native_user_uuid and old['status'] != 'rejected':
                return old
            if old['status'] != 'submitted_unknown':
                raise ClaudeStoreError('conflict')
            db.execute("UPDATE claude_prompts SET status='accepted',native_user_uuid=?,updated_at=? WHERE client_id=?",
                       (native_user_uuid, _stamp(), client_id))
            return self._receipt(db, 'claude_prompts', client_id)

    @staticmethod
    def _saved_result(db, receipt):
        if db.execute('PRAGMA user_version').fetchone()[0] == 1:
            return None
        row = db.execute('''SELECT result_sha256,length(CAST(result_json AS BLOB)) AS size,
            CASE WHEN length(CAST(result_json AS BLOB))<=? THEN result_json END AS result_json
            FROM claude_prompts WHERE client_id=?''', (_MAX_SAVED_BYTES, receipt['client_id'])).fetchone()
        raw, digest, size = row['result_json'], row['result_sha256'], row['size']
        if raw is None and digest is None and size is None:
            return None
        try:
            if (receipt['status'] != 'completed' or type(raw) is not str or type(digest) is not str
                    or not _SHA.fullmatch(digest) or not 0 < size <= _MAX_SAVED_BYTES
                    or hashlib.sha256(raw.encode('utf-8')).hexdigest() != digest):
                raise ValueError
            validate_uuid(receipt['native_result_uuid'])
            value = json.loads(raw)
            if (type(value) is not dict or set(value) != {'identity', 'record'}
                    or value['identity'] != {key: receipt[key] for key in _RESULT_IDENTITY}):
                raise ValueError
            record = _result_record(receipt, receipt['native_result_uuid'], value['record'])
            if record is None or record != value['record'] or _saved_json(receipt, record) != raw:
                raise ValueError
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise ClaudeStoreError('invalid_schema') from None
        return raw, record

    def completed_result(self, chat_id, client_id):
        """Read a stored SDK Result from its original epoch, never a live claim."""
        chat_id, client_id = validate_uuid(chat_id), validate_uuid(client_id)
        with self._db() as db:
            receipt = self._receipt(db, 'claude_prompts', client_id)
            if receipt is None:
                return None
            if receipt['chat_id'] != chat_id:
                raise ClaudeStoreError('conflict')
            binding = self._binding(db, chat_id)
            if binding is None or binding['session_id'] != receipt['session_id']:
                raise ClaudeStoreError('conflict')
            saved = self._saved_result(db, receipt)
            return None if saved is None else {'receipt': receipt, 'record': saved[1]}

    def finish(self, client_id, session_id, epoch, result_uuid, status='completed', result=None):
        """Only an exact original Result in this epoch's unique active query."""
        result_uuid = validate_uuid(result_uuid)
        if type(status) is not str or status not in TERMINAL:
            raise ClaudeStoreError('invalid_arguments')
        with self._db(write=True) as db:
            old = self._target(db, client_id, session_id, epoch)
            record = _result_record(old, result_uuid, result) if status == 'completed' else None
            if (old['status'], old['native_result_uuid']) == (status, result_uuid):
                saved = self._saved_result(db, old)
                if saved is not None and result is not None and (record is None or _saved_json(old, record) != saved[0]):
                    raise ClaudeStoreError('conflict')
                return old
            if old['status'] not in PENDING or self._active(db, old['chat_id'])['client_id'] != client_id:
                raise ClaudeStoreError('conflict')
            raw = _saved_json(dict(old, native_result_uuid=result_uuid), record) if record is not None else None
            digest = hashlib.sha256(raw.encode('utf-8')).hexdigest() if raw is not None else None
            db.execute('''UPDATE claude_prompts SET status=?,native_result_uuid=?,updated_at=?,
                result_json=?,result_sha256=? WHERE client_id=?''',
                (status, result_uuid, _stamp(), raw, digest, client_id))
            return self._receipt(db, 'claude_prompts', client_id)

    def rejected(self, client_id, session_id, epoch):
        """Only a proven pre-acceptance rejection; transport loss stays unknown."""
        with self._db(write=True) as db:
            old = self._target(db, client_id, session_id, epoch)
            if old['status'] == 'rejected':
                return old
            if old['status'] != 'submitted_unknown':
                raise ClaudeStoreError('conflict')
            db.execute("UPDATE claude_prompts SET status='rejected',updated_at=? WHERE client_id=?", (_stamp(), client_id))
            return self._receipt(db, 'claude_prompts', client_id)

    def interrupt(self, chat_id, client_id, epoch, active_client_id):
        """Independent control fence; it cannot finish or replace a prompt."""
        chat_id, client_id, epoch = validate_uuid(chat_id), validate_uuid(client_id), _epoch(epoch)
        active_client_id = validate_uuid(active_client_id)
        with self._db(write=True) as db:
            binding = self._binding(db, chat_id)
            if binding is None:
                raise ClaudeStoreError('not_found')
            old = self._receipt(db, 'claude_controls', client_id)
            if old:
                if (old['chat_id'], old['active_client_id']) != (chat_id, active_client_id):
                    raise ClaudeStoreError('conflict')
                return dict(old, created=False)
            active = self._active(db, chat_id)
            if active is None or (active['client_id'], active['epoch']) != (active_client_id, epoch):
                raise ClaudeStoreError('conflict')
            digest = request_sha256({'type': 'interrupt', 'session_id': binding['session_id'],
                                     'active_client_id': active_client_id})
            now = _stamp()
            db.execute("INSERT INTO claude_controls VALUES(?,?,?,?,?,?,'submitted_unknown',?,?)",
                       (client_id, chat_id, binding['session_id'], epoch, active_client_id, digest, now, now))
            return dict(self._receipt(db, 'claude_controls', client_id), created=True)

    def finish_interrupt(self, client_id, session_id, epoch):
        with self._db(write=True) as db:
            self._target(db, client_id, session_id, epoch, table='claude_controls')
            db.execute("UPDATE claude_controls SET status='acknowledged',updated_at=? WHERE client_id=?",
                       (_stamp(), client_id))
            return self._receipt(db, 'claude_controls', client_id)

    def interrupt_receipt(self, client_id):
        with self._db() as db:
            value = self._receipt(db, 'claude_controls', validate_uuid(client_id))
            if value:
                self._binding(db, value['chat_id'])
            return value

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            # Do not LOCK_UN: a forked child's close must not unlock its parent.
            for name in ('_file_fd', '_lock_fd', '_parent_fd'):
                fd = getattr(self, name)
                if fd is not None:
                    os.close(fd)
                    setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
