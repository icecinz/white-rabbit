"""Independent native-session bindings and conservative submission receipts.

This module never launches an Agent, reads credentials, or copies transcripts.
``begin`` commits submitted_unknown before the caller may send a native RPC.
Only its transient ``created=True`` result grants that first submission; a
recovered or repeated receipt must be inspected, never automatically resent.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
from urllib.parse import quote


APPLICATION_ID = 0x52424E53  # RBNS, separate from the shared-chat database.
SCHEMA_VERSION = 1
KINDS = frozenset(('thread_start', 'thread_resume', 'turn_start', 'turn_interrupt'))
TERMINAL = frozenset(('completed', 'failed', 'interrupted'))
STATUSES = frozenset(('submitted_unknown', 'accepted', 'rejected')) | TERMINAL
MAX_REQUEST_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 10000
_TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_MESSAGES = {
    'invalid_arguments': '原生会话参数无效。',
    'scope_changed': '原生会话记录或工作目录不可用，未跟随替换路径。',
    'invalid_schema': '这不是受支持的独立原生会话记录库，原库未被改写。',
    'invalid_state': '原生会话记录状态无效，未修改记录。',
    'not_found': '找不到原生会话绑定或操作回执。',
    'conflict': '原生会话绑定或操作回执不匹配，未覆盖原记录。',
    'busy': '已有原生操作尚未确认或结束，请先核对原回执。',
    'readonly': '原生会话记录以只读方式打开。',
    'closed': '原生会话记录库已关闭。',
    'io_failed': '原生会话记录暂时无法读取或保存，请核对原回执。',
}


class NativeSessionStoreError(ValueError):
    def __init__(self, code):
        self.code = code if code in _MESSAGES else 'io_failed'
        super().__init__(_MESSAGES[self.code])


def validate_token(value):
    """Validate one stable local ledger token without normalization."""
    if type(value) is not str or not _TOKEN.fullmatch(value):
        raise NativeSessionStoreError('invalid_arguments')
    return value


def validate_native_id(value, *, optional=False):
    """Validate one exact native ID while preserving an explicitly optional None."""
    if value is None and optional:
        return None
    if (type(value) is not str or not 1 <= len(value) <= 256
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)):
        raise NativeSessionStoreError('invalid_arguments')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise NativeSessionStoreError('invalid_arguments') from None
    return value


def request_sha256(request):
    """Strict Python JSON values only; hash the body without storing its text."""
    return _request_snapshot(request)[0]


def _request_snapshot(request):
    if type(request) is not dict:
        raise NativeSessionStoreError('invalid_arguments')
    active, count = set(), 0

    def check(value, depth=0):
        nonlocal count
        count += 1
        if count > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise NativeSessionStoreError('invalid_arguments')
        kind = type(value)
        if value is None or kind is bool:
            return value
        if kind is int:
            if not -(2**53 - 1) <= value <= 2**53 - 1:
                raise NativeSessionStoreError('invalid_arguments')
            return value
        if kind is float:
            if not math.isfinite(value):
                raise NativeSessionStoreError('invalid_arguments')
            return value
        if kind is str:
            try:
                value.encode('utf-8')
            except UnicodeError:
                raise NativeSessionStoreError('invalid_arguments') from None
            return value
        if kind not in (dict, list) or id(value) in active:
            raise NativeSessionStoreError('invalid_arguments')
        active.add(id(value))
        try:
            if kind is dict:
                copied = {}
                for key, item in value.items():
                    if type(key) is not str:
                        raise NativeSessionStoreError('invalid_arguments')
                    check(key, depth + 1)
                    copied[key] = check(item, depth + 1)
                return copied
            else:
                return [check(item, depth + 1) for item in value]
        finally:
            active.remove(id(value))

    try:
        snapshot = check(request)
        raw = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    except (RuntimeError, TypeError, ValueError, UnicodeError):
        raise NativeSessionStoreError('invalid_arguments') from None
    if len(raw) > MAX_REQUEST_BYTES:
        raise NativeSessionStoreError('invalid_arguments')
    return hashlib.sha256(raw).hexdigest(), snapshot


def validate_path(value):
    """Validate an explicit absolute lexical path without resolving it."""
    try:
        raw = os.fspath(value)
    except TypeError:
        raise NativeSessionStoreError('invalid_arguments') from None
    if type(raw) is not str or not raw or '\x00' in raw or not os.path.isabs(raw) or raw != os.path.abspath(raw):
        raise NativeSessionStoreError('invalid_arguments')
    try:
        raw.encode('utf-8')
    except UnicodeError:
        raise NativeSessionStoreError('invalid_arguments') from None
    return Path(raw)


def open_directory(path):
    """Open an existing directory through anchored, non-symlink components."""
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def file_identity(info):
    """Return the filesystem device and inode used for identity checks."""
    return info.st_dev, info.st_ino


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


_DDL = (
    '''CREATE TABLE native_sessions (
        session_key TEXT PRIMARY KEY, cwd TEXT NOT NULL,
        cwd_device INTEGER NOT NULL, cwd_inode INTEGER NOT NULL,
        thread_id TEXT UNIQUE, session_id TEXT UNIQUE,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK(thread_id IS NOT NULL OR session_id IS NULL))''',
    '''CREATE TABLE native_operations (
        client_id TEXT PRIMARY KEY, session_key TEXT NOT NULL REFERENCES native_sessions(session_key),
        kind TEXT NOT NULL CHECK(kind IN ('thread_start','thread_resume','turn_start','turn_interrupt')),
        request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
        status TEXT NOT NULL CHECK(status IN ('submitted_unknown','accepted','rejected','completed','failed','interrupted')),
        thread_id TEXT, session_id TEXT, turn_id TEXT, target_turn_id TEXT, rpc_error_code INTEGER,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK(status NOT IN ('accepted','completed','failed','interrupted') OR thread_id IS NOT NULL),
        CHECK(status NOT IN ('completed','failed','interrupted') OR (kind='turn_start' AND turn_id IS NOT NULL)),
        CHECK((status='rejected' AND rpc_error_code IS NOT NULL) OR (status<>'rejected' AND rpc_error_code IS NULL)),
        CHECK(status NOT IN ('submitted_unknown','rejected') OR (thread_id IS NULL AND session_id IS NULL AND turn_id IS NULL)),
        CHECK((kind='turn_interrupt' AND target_turn_id IS NOT NULL) OR (kind<>'turn_interrupt' AND target_turn_id IS NULL)))''',
    '''CREATE UNIQUE INDEX native_one_thread_start ON native_operations(session_key)
        WHERE kind='thread_start' AND status IN ('submitted_unknown','accepted')''',
    '''CREATE UNIQUE INDEX native_one_turn_start ON native_operations(session_key)
        WHERE kind='turn_start' AND status IN ('submitted_unknown','accepted')''',
    '''CREATE UNIQUE INDEX native_turn_owner ON native_operations(thread_id,turn_id)
        WHERE kind='turn_start' AND turn_id IS NOT NULL''',
)


def _schema(db):
    return tuple(tuple(row) for row in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))


def _expected_schema():
    db = sqlite3.connect(':memory:')
    try:
        for statement in _DDL:
            db.execute(statement)
        return _schema(db)
    finally:
        db.close()


_EXPECTED_SCHEMA = _expected_schema()


class NativeSessionStore:
    """Own one explicitly supplied database; reading never recovers or resends."""
    def __init__(self, path, *, readonly=False):
        if type(readonly) is not bool:
            raise NativeSessionStoreError('invalid_arguments')
        self.path = validate_path(path)
        if not self.path.name:
            raise NativeSessionStoreError('invalid_arguments')
        self.readonly = readonly
        self.lock = threading.RLock()
        self._parent_fd = self._file_fd = self._lock_fd = None
        self._closed = False
        created = False
        try:
            self._parent_fd = open_directory(self.path.parent)
            self._parent_identity = file_identity(os.fstat(self._parent_fd))
            self._lock_name = '.' + self.path.name + '.lock'
            if not readonly:
                try:
                    self._lock_fd = os.open(self._lock_name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                            0o600, dir_fd=self._parent_fd)
                except FileExistsError:
                    pass
            if self._lock_fd is None:
                self._lock_fd = os.open(self._lock_name, (os.O_RDONLY if readonly else os.O_RDWR) |
                                        os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._parent_fd)
            info = os.fstat(self._lock_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise NativeSessionStoreError('scope_changed')
            self._lock_identity = file_identity(info)
            # Keep initialization under a separate lock. On macOS flock on the
            # SQLite file itself conflicts with SQLite's own file locks.
            fcntl.flock(self._lock_fd, fcntl.LOCK_SH if readonly else fcntl.LOCK_EX)
            if not readonly:
                try:
                    self._file_fd = os.open(self.path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                            0o600, dir_fd=self._parent_fd)
                    created = True
                except FileExistsError:
                    pass
            if self._file_fd is None:
                self._file_fd = os.open(self.path.name, (os.O_RDONLY if readonly else os.O_RDWR) |
                                        os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._parent_fd)
            self._file_identity = file_identity(os.fstat(self._file_fd))
            # An existing database is first inspected in actual mode=ro. An
            # unrelated/chat database must never be initialized or recovered.
            with self._db(write=created, initialize=created):
                pass
        except BaseException as error:
            self.close()
            if isinstance(error, NativeSessionStoreError):
                raise
            if isinstance(error, sqlite3.Error):
                raise NativeSessionStoreError('invalid_schema') from None
            if isinstance(error, OSError):
                raise NativeSessionStoreError('scope_changed') from None
            raise

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            for field in ('_file_fd', '_lock_fd', '_parent_fd'):
                descriptor = getattr(self, field)
                if descriptor is not None:
                    os.close(descriptor)
                    setattr(self, field, None)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _check_path(self):
        if self._closed:
            raise NativeSessionStoreError('closed')
        parent = open_directory(self.path.parent)
        try:
            if file_identity(os.fstat(parent)) != self._parent_identity:
                raise NativeSessionStoreError('scope_changed')
        finally:
            os.close(parent)
        for name in (self._lock_name, self.path.name, self.path.name + '-journal', self.path.name + '-wal', self.path.name + '-shm'):
            try:
                info = os.stat(name, dir_fd=self._parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if name in (self.path.name, self._lock_name):
                    raise NativeSessionStoreError('scope_changed') from None
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (name == self.path.name and file_identity(info) != self._file_identity):
                raise NativeSessionStoreError('scope_changed')
            if name == self._lock_name and file_identity(info) != self._lock_identity:
                raise NativeSessionStoreError('scope_changed')

    @contextmanager
    def _db(self, *, write=False, initialize=False):
        with self.lock:
            if self._closed:
                raise NativeSessionStoreError('closed')
            if write and self.readonly:
                raise NativeSessionStoreError('readonly')
            db = None
            locked = False
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
                locked = True
                self._check_path()
                db = sqlite3.connect('file:' + quote(str(self.path), safe='/') + ('?mode=rw' if write else '?mode=ro'),
                                     uri=True, timeout=10, isolation_level=None)
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('PRAGMA trusted_schema=OFF')
                if initialize:
                    if _schema(db):
                        raise NativeSessionStoreError('invalid_schema')
                    db.execute('BEGIN IMMEDIATE')
                    for statement in _DDL:
                        db.execute(statement)
                    db.execute('PRAGMA application_id=' + str(APPLICATION_ID))
                    db.execute('PRAGMA user_version=' + str(SCHEMA_VERSION))
                else:
                    if (db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                            or db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION
                            or _schema(db) != _EXPECTED_SCHEMA):
                        raise NativeSessionStoreError('invalid_schema')
                    if write:
                        db.execute('BEGIN IMMEDIATE')
                    else:
                        db.execute('PRAGMA query_only=ON')
                        db.execute('BEGIN')
                yield db
                self._check_path()
                if write:
                    db.commit()
            except NativeSessionStoreError:
                raise
            except sqlite3.IntegrityError:
                raise NativeSessionStoreError('conflict') from None
            except sqlite3.Error:
                raise NativeSessionStoreError('io_failed') from None
            except OSError:
                raise NativeSessionStoreError('scope_changed') from None
            finally:
                if db is not None:
                    if db.in_transaction:
                        db.rollback()
                    db.close()
                if locked:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    @staticmethod
    def _binding(db, session_key):
        row = db.execute('SELECT * FROM native_sessions WHERE session_key=?', (session_key,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        try:
            validate_token(value['session_key'])
            validate_path(value['cwd'])
            validate_native_id(value['thread_id'], optional=True)
            validate_native_id(value['session_id'], optional=True)
            if (type(value['cwd_device']) is not int or value['cwd_device'] < 0
                    or type(value['cwd_inode']) is not int or value['cwd_inode'] <= 0
                    or value['thread_id'] is None and value['session_id'] is not None
                    or any(type(value[key]) is not str or not value[key] for key in ('created_at', 'updated_at'))):
                raise NativeSessionStoreError('invalid_state')
        except NativeSessionStoreError:
            raise NativeSessionStoreError('invalid_state') from None
        return value

    @staticmethod
    def _operation(db, client_id):
        row = db.execute('SELECT * FROM native_operations WHERE client_id=?', (client_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        try:
            validate_token(value['client_id'])
            validate_token(value['session_key'])
            for key in ('thread_id', 'session_id', 'turn_id', 'target_turn_id'):
                validate_native_id(value[key], optional=True)
            if (value['kind'] not in KINDS or value['status'] not in STATUSES
                    or type(value['request_sha256']) is not str or not _SHA.fullmatch(value['request_sha256'])
                    or any(type(value[key]) is not str or not value[key] for key in ('created_at', 'updated_at'))):
                raise NativeSessionStoreError('invalid_state')
            rejection = value['status'] == 'rejected'
            if (rejection and (type(value['rpc_error_code']) is not int or not -(2**31) <= value['rpc_error_code'] < 2**31)
                    or not rejection and value['rpc_error_code'] is not None
                    or (value['kind'] == 'turn_interrupt') != (value['target_turn_id'] is not None)):
                raise NativeSessionStoreError('invalid_state')
            binding = NativeSessionStore._binding(db, value['session_key'])
            if binding is None:
                raise NativeSessionStoreError('invalid_state')
            if value['status'] in ('submitted_unknown', 'rejected'):
                if any(value[key] is not None for key in ('thread_id', 'session_id', 'turn_id')):
                    raise NativeSessionStoreError('invalid_state')
            elif ((value['thread_id'], value['session_id']) != (binding['thread_id'], binding['session_id'])
                    or value['thread_id'] is None
                    or value['kind'] in ('thread_start', 'thread_resume') and value['turn_id'] is not None
                    or value['kind'] == 'turn_start' and value['turn_id'] is None
                    or value['kind'] == 'turn_interrupt' and value['turn_id'] != value['target_turn_id']
                    or value['status'] in TERMINAL and value['kind'] != 'turn_start'):
                raise NativeSessionStoreError('invalid_state')
        except NativeSessionStoreError:
            raise NativeSessionStoreError('invalid_state') from None
        return value

    @staticmethod
    def _check_cwd(binding):
        descriptor = open_directory(validate_path(binding['cwd']))
        try:
            if file_identity(os.fstat(descriptor)) != (binding['cwd_device'], binding['cwd_inode']):
                raise NativeSessionStoreError('scope_changed')
        finally:
            os.close(descriptor)

    def bind(self, session_key, cwd):
        """Reserve a cwd only; native thread identity is accepted atomically later."""
        session_key, cwd = validate_token(session_key), validate_path(cwd)
        try:
            descriptor = open_directory(cwd)
            try:
                device, inode = file_identity(os.fstat(descriptor))
            finally:
                os.close(descriptor)
        except OSError:
            raise NativeSessionStoreError('scope_changed') from None
        with self._db(write=True) as db:
            old = self._binding(db, session_key)
            if old:
                if old['cwd'] != str(cwd) or (old['cwd_device'], old['cwd_inode']) != (device, inode):
                    raise NativeSessionStoreError('conflict')
                return old
            stamp = _stamp()
            db.execute('INSERT INTO native_sessions VALUES(?,?,?,?,NULL,NULL,?,?)',
                       (session_key, str(cwd), device, inode, stamp, stamp))
            return self._binding(db, session_key)

    def adopt_thread(self, session_key, cwd, thread_id, session_id=None):
        """Register a trusted existing identity, without fabricating an ACK.

        The caller verifies the native source before entering this metadata
        primitive. This does not resume a thread or coordinate external hosts.
        A prior operation, even rejected, prevents adoption or adoption replay.
        """
        session_key, cwd = validate_token(session_key), validate_path(cwd)
        thread_id = validate_native_id(thread_id)
        session_id = validate_native_id(session_id, optional=True)
        try:
            descriptor = open_directory(cwd)
            try:
                identity = file_identity(os.fstat(descriptor))
            finally:
                os.close(descriptor)
        except OSError:
            raise NativeSessionStoreError('scope_changed') from None
        with self._db(write=True) as db:
            old = self._binding(db, session_key)
            if db.execute('SELECT 1 FROM native_operations WHERE session_key=? LIMIT 1',
                          (session_key,)).fetchone() is not None:
                raise NativeSessionStoreError('conflict')
            if old is not None:
                self._check_cwd(old)
                if (old['cwd'], old['cwd_device'], old['cwd_inode'], old['thread_id'], old['session_id']) != (
                        str(cwd), *identity, thread_id, session_id):
                    raise NativeSessionStoreError('conflict')
                return old
            stamp = _stamp()
            db.execute('INSERT INTO native_sessions VALUES(?,?,?,?,?,?,?,?)',
                       (session_key, str(cwd), *identity, thread_id, session_id, stamp, stamp))
            binding = self._binding(db, session_key)
            self._check_cwd(binding)
            return binding

    def read_binding(self, session_key):
        with self._db() as db:
            return self._binding(db, validate_token(session_key))

    def sources(self):
        """Ordinary app-owned Codex threads only; no cwd or transcript read."""
        with self._db() as db:
            rows = db.execute('SELECT session_key,thread_id FROM native_sessions '
                              'WHERE thread_id IS NOT NULL ORDER BY session_key')
            return [('codex', validate_native_id(row['thread_id'])) for row in rows
                    if re.fullmatch(r'chat/codex/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}',
                                    row['session_key'])]

    def source_bindings(self, *, include_activity=False):
        """Ordinary catalogue links only; no source/cwd validation or RPC."""
        if type(include_activity) is not bool:
            raise NativeSessionStoreError('invalid_arguments')
        with self._db() as db:
            activity = ',a.activity_at' if include_activity else ''
            join = (" LEFT JOIN (SELECT session_key,MAX(updated_at) activity_at FROM native_operations "
                    "WHERE kind='turn_start' AND status IN ('accepted','completed','failed','interrupted') "
                    "GROUP BY session_key) a ON a.session_key=b.session_key") if include_activity else ''
            rows = db.execute('SELECT b.session_key,b.thread_id' + activity + ' FROM native_sessions b' + join +
                              ' WHERE b.thread_id IS NOT NULL ORDER BY b.session_key')
            return [{'chat_id': row['session_key'].removeprefix('chat/codex/'),
                     'session_id': validate_native_id(row['thread_id']),
                     **({'activity_at': row['activity_at']} if include_activity else {})} for row in rows
                    if re.fullmatch(r'chat/codex/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}',
                                    row['session_key'])]

    def verify_scope(self, session_key):
        """Read and verify the original cwd identity before a native action."""
        with self._db() as db:
            binding = self._binding(db, validate_token(session_key))
            if binding is None:
                raise NativeSessionStoreError('not_found')
            self._check_cwd(binding)
            return binding

    def read_operation(self, client_id, *, session_key=None):
        client_id = validate_token(client_id)
        if session_key is not None:
            session_key = validate_token(session_key)
        with self._db() as db:
            row = self._operation(db, client_id)
            if row and session_key is not None and row['session_key'] != session_key:
                raise NativeSessionStoreError('conflict')
            return row

    def find_turn(self, session_key, turn_id):
        session_key, turn_id = validate_token(session_key), validate_native_id(turn_id)
        with self._db() as db:
            row = db.execute("SELECT client_id FROM native_operations WHERE session_key=? AND kind='turn_start' AND turn_id=?",
                             (session_key, turn_id)).fetchone()
            return self._operation(db, row['client_id']) if row else None

    def pending_operations(self, session_key):
        """Unknown thread starts and unknown/accepted turns, never auto-resend."""
        session_key = validate_token(session_key)
        with self._db() as db:
            rows = db.execute("""SELECT client_id FROM native_operations WHERE session_key=? AND
                ((kind='thread_start' AND status='submitted_unknown') OR
                 (kind='turn_start' AND status IN ('submitted_unknown','accepted')))
                ORDER BY created_at,client_id""", (session_key,)).fetchall()
            return [self._operation(db, row['client_id']) for row in rows]

    def has_unfinished_operations(self, session_key):
        """A scoped disconnect guard, including unknown resumes/interrupts.

        Accepted creation/control ACKs are complete. An accepted turn still
        needs its native terminal receipt. This reads no prompt or transcript.
        """
        session_key = validate_token(session_key)
        with self._db() as db:
            return db.execute("""SELECT 1 FROM native_operations WHERE session_key=? AND
                (status='submitted_unknown' OR (kind='turn_start' AND status='accepted')) LIMIT 1""",
                (session_key,)).fetchone() is not None

    def maintenance_state(self):
        """Read only the three bounded ledger counts and their completeness."""
        from .maintenance import count_rows
        specs = {'pending_creations':('native_operations', "kind='thread_start' AND status='submitted_unknown'", ()),
            'pending_prompts':('native_operations', "kind='turn_start' AND status IN ('submitted_unknown','accepted')", ()),
            'unknown_controls':('native_operations', "kind IN ('thread_resume','turn_interrupt') AND status='submitted_unknown'", ())}
        return count_rows(self.path, specs, lock=self.lock, expected_identity=self._file_identity,
            check_path=self._check_path, flock_fd=self._lock_fd)

    def begin(self, session_key, client_id, kind, request):
        """Commit uncertainty first. created=False never authorizes another send."""
        session_key, client_id = validate_token(session_key), validate_token(client_id)
        if type(kind) is not str or kind not in KINDS:
            raise NativeSessionStoreError('invalid_arguments')
        digest, request = _request_snapshot(request)
        with self._db(write=True) as db:
            old = self._operation(db, client_id)
            if old:
                if (old['session_key'], old['kind'], old['request_sha256']) != (session_key, kind, digest):
                    raise NativeSessionStoreError('conflict')
                return dict(old, created=False)
            binding = self._binding(db, session_key)
            if binding is None:
                raise NativeSessionStoreError('not_found')
            self._check_cwd(binding)
            if 'cwd' in request and request['cwd'] != binding['cwd']:
                raise NativeSessionStoreError('conflict')
            target_turn = None
            if kind == 'thread_start':
                if request.get('cwd') != binding['cwd']:
                    raise NativeSessionStoreError('conflict')
                if binding['thread_id'] is not None:
                    raise NativeSessionStoreError('conflict')
            else:
                if binding['thread_id'] is None:
                    raise NativeSessionStoreError('not_found')
                if request.get('threadId') != binding['thread_id']:
                    raise NativeSessionStoreError('conflict')
                if kind == 'turn_interrupt':
                    target_turn = validate_native_id(request.get('turnId'))
                    active = db.execute("SELECT 1 FROM native_operations WHERE session_key=? AND kind='turn_start' AND status='accepted' AND turn_id=?",
                                        (session_key, target_turn)).fetchone()
                    if not active:
                        raise NativeSessionStoreError('conflict')
            if kind in ('thread_start', 'turn_start') and db.execute(
                    "SELECT 1 FROM native_operations WHERE session_key=? AND kind=? AND status IN ('submitted_unknown','accepted')",
                    (session_key, kind)).fetchone():
                raise NativeSessionStoreError('busy')
            stamp = _stamp()
            db.execute('''INSERT INTO native_operations VALUES(?,?,?,?, 'submitted_unknown',NULL,NULL,NULL,?,NULL,?,?)''',
                       (client_id, session_key, kind, digest, target_turn, stamp, stamp))
            return dict(self._operation(db, client_id), created=True)

    def accept_operation(self, client_id, *, thread_id, session_id=None, turn_id=None):
        client_id, thread_id = validate_token(client_id), validate_native_id(thread_id)
        session_id, turn_id = validate_native_id(session_id, optional=True), validate_native_id(turn_id, optional=True)
        with self._db(write=True) as db:
            row = self._operation(db, client_id)
            if row is None:
                raise NativeSessionStoreError('not_found')
            binding = self._binding(db, row['session_key'])
            self._check_cwd(binding)
            if row['kind'] == 'thread_start':
                if turn_id is not None:
                    raise NativeSessionStoreError('conflict')
                accepted_session = session_id
            else:
                if binding['thread_id'] != thread_id or session_id is not None and session_id != binding['session_id']:
                    raise NativeSessionStoreError('conflict')
                accepted_session = binding['session_id']
                if row['kind'] == 'turn_start' and turn_id is None or row['kind'] == 'thread_resume' and turn_id is not None:
                    raise NativeSessionStoreError('conflict')
                if row['kind'] == 'turn_interrupt' and turn_id != row['target_turn_id']:
                    raise NativeSessionStoreError('conflict')
            if row['status'] != 'submitted_unknown':
                if (row['status'] in ('accepted', *TERMINAL) and
                        (row['thread_id'], row['session_id'], row['turn_id']) == (thread_id, accepted_session, turn_id)):
                    return row
                raise NativeSessionStoreError('conflict')
            stamp = _stamp()
            if row['kind'] == 'thread_start':
                if binding['thread_id'] is not None:
                    raise NativeSessionStoreError('conflict')
                db.execute('UPDATE native_sessions SET thread_id=?,session_id=?,updated_at=? WHERE session_key=? AND thread_id IS NULL',
                           (thread_id, accepted_session, stamp, row['session_key']))
            db.execute("UPDATE native_operations SET status='accepted',thread_id=?,session_id=?,turn_id=?,updated_at=? WHERE client_id=?",
                       (thread_id, accepted_session, turn_id, stamp, client_id))
            return self._operation(db, client_id)

    def reject_operation(self, client_id, *, rpc_error_code):
        client_id = validate_token(client_id)
        if type(rpc_error_code) is not int or not -(2**31) <= rpc_error_code < 2**31:
            raise NativeSessionStoreError('invalid_arguments')
        with self._db(write=True) as db:
            row = self._operation(db, client_id)
            if row is None:
                raise NativeSessionStoreError('not_found')
            if row['status'] == 'rejected' and row['rpc_error_code'] == rpc_error_code:
                return row
            if row['status'] != 'submitted_unknown':
                raise NativeSessionStoreError('conflict')
            db.execute("UPDATE native_operations SET status='rejected',rpc_error_code=?,updated_at=? WHERE client_id=?",
                       (rpc_error_code, _stamp(), client_id))
            return self._operation(db, client_id)

    def finish_turn(self, client_id, *, thread_id, turn_id, status):
        client_id, thread_id, turn_id = validate_token(client_id), validate_native_id(thread_id), validate_native_id(turn_id)
        if type(status) is not str or status not in TERMINAL:
            raise NativeSessionStoreError('invalid_arguments')
        with self._db(write=True) as db:
            row = self._operation(db, client_id)
            if row is None:
                raise NativeSessionStoreError('not_found')
            binding = self._binding(db, row['session_key'])
            if (row['kind'] != 'turn_start' or binding['thread_id'] != thread_id
                    or (row['thread_id'], row['turn_id']) != (thread_id, turn_id)):
                raise NativeSessionStoreError('conflict')
            if row['status'] in TERMINAL:
                if row['status'] == status:
                    return row
                raise NativeSessionStoreError('conflict')
            if row['status'] != 'accepted':
                raise NativeSessionStoreError('conflict')
            db.execute('UPDATE native_operations SET status=?,updated_at=? WHERE client_id=?',
                       (status, _stamp(), client_id))
            return self._operation(db, client_id)
