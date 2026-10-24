"""Private Pi bindings and conservative receipts; never stores prompt bodies.

Pi owns its JSONL tree. This store records an exact session path/id, not a Codex
thread or invented turn. An unknown submission remains pending across restart.
The writer owns a lifetime local flock; readers may inspect committed data.
The manager must stop all of its native holders before closing this store.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
from urllib.parse import quote
import uuid

from ..native_session_store import (NativeSessionStoreError, open_directory as _directory, file_identity as _identity,
    validate_path as _path, validate_token as _token)
from .material_protocol import request_sha256, PiMaterialProtocolError


APPLICATION_ID = 0x52504E53  # RPNS; separate from shared chat and Codex ledgers.
SCHEMA_VERSION = 1
PENDING = frozenset(('submitted_unknown', 'accepted'))
TERMINAL = frozenset(('completed', 'failed', 'interrupted'))
STATUSES = PENDING | TERMINAL | {'rejected'}
_SESSION_ID = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9._-]{0,254}[A-Za-z0-9])?\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_MESSAGES = {
    'invalid_arguments': 'Pi 会话记录参数无效。',
    'conflict': 'Pi 会话身份或请求指纹不一致，未覆盖原记录。',
    'busy': 'Pi 记录库已有持有者，或这段会话仍有待确认请求。',
    'closed': 'Pi 会话记录库已关闭。',
    'readonly': 'Pi 会话记录库为只读。',
    'not_found': '找不到这段 Pi 会话或请求回执。',
    'scope_changed': 'Pi 记录库或绑定目录身份变化，未继续操作。',
    'invalid_schema': '这不是受支持的独立 Pi 记录库。',
    'io_failed': 'Pi 会话记录暂时无法读取或保存。',
    'wrong_process': '当前进程不能借用其他进程的 Pi 记录库持有权。',
}


class PiStoreError(ValueError):
    def __init__(self, code):
        self.code = code if code in _MESSAGES else 'io_failed'
        super().__init__(_MESSAGES[self.code])


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise PiStoreError('invalid_arguments') from None
    return value


def validate_session_id(value):
    # Pi permits explicit alphanumeric/dot/dash/underscore IDs, not only UUIDs.
    if type(value) is not str or not _SESSION_ID.fullmatch(value):
        raise PiStoreError('invalid_arguments')
    return value


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


_DDL = (
    '''CREATE TABLE pi_bindings (
        chat_id TEXT PRIMARY KEY, cwd TEXT NOT NULL, cwd_device INTEGER NOT NULL, cwd_inode INTEGER NOT NULL,
        session_dir TEXT NOT NULL, session_dir_device INTEGER NOT NULL, session_dir_inode INTEGER NOT NULL,
        session_file TEXT UNIQUE, session_id TEXT UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK((session_file IS NULL) = (session_id IS NULL)))''',
    '''CREATE TABLE pi_operations (
        client_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES pi_bindings(chat_id),
        request_sha256 TEXT NOT NULL, status TEXT NOT NULL
        CHECK(status IN ('submitted_unknown','accepted','rejected','completed','failed','interrupted')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
    '''CREATE UNIQUE INDEX pi_one_pending ON pi_operations(chat_id)
        WHERE status IN ('submitted_unknown','accepted')''',
)


def _schema(db):
    return tuple(tuple(row) for row in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))


def _expected_schema():
    db = sqlite3.connect(':memory:')
    try:
        for sql in _DDL:
            db.execute(sql)
        return _schema(db)
    finally:
        db.close()


_EXPECTED_SCHEMA = _expected_schema()


class PiNativeStore:
    def __init__(self, path, *, readonly=False):
        if type(readonly) is not bool:
            raise PiStoreError('invalid_arguments')
        try:
            self.path = _path(path)
        except NativeSessionStoreError:
            raise PiStoreError('invalid_arguments') from None
        if not self.path.name:
            raise PiStoreError('invalid_arguments')
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
            os.set_inheritable(self._lock_fd, False)
            info = os.fstat(self._lock_fd)
            self._regular(info)
            self._lock_identity = _identity(info)
            if not readonly:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                        raise PiStoreError('busy') from None
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
            os.set_inheritable(self._file_fd, False)
            self._regular(os.fstat(self._file_fd))
            self._file_identity = _identity(os.fstat(self._file_fd))
            with self._db(write=created, initialize=created):
                pass
        except BaseException as error:
            self.close()
            if isinstance(error, PiStoreError):
                raise
            if isinstance(error, sqlite3.Error):
                raise PiStoreError('invalid_schema') from None
            if isinstance(error, (OSError, NativeSessionStoreError)):
                raise PiStoreError('scope_changed') from None
            raise

    @staticmethod
    def _regular(info):
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PiStoreError('scope_changed')

    def _check_path(self):
        if self._closed:
            raise PiStoreError('closed')
        if self._owner_pid != os.getpid():
            raise PiStoreError('wrong_process')
        parent = _directory(self.path.parent)
        try:
            if _identity(os.fstat(parent)) != self._parent_identity:
                raise PiStoreError('scope_changed')
        finally:
            os.close(parent)
        for name in (self.path.name, self._lock_name, self.path.name+'-journal', self.path.name+'-wal', self.path.name+'-shm'):
            try:
                info = os.stat(name, dir_fd=self._parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if name in (self.path.name, self._lock_name):
                    raise PiStoreError('scope_changed') from None
                continue
            self._regular(info)
            expected = self._file_identity if name == self.path.name else self._lock_identity if name == self._lock_name else None
            if expected is not None and _identity(info) != expected:
                raise PiStoreError('scope_changed')

    @contextmanager
    def _db(self, *, write=False, initialize=False):
        with self.lock:
            if self._closed:
                raise PiStoreError('closed')
            if write and self.readonly:
                raise PiStoreError('readonly')
            db = None
            try:
                self._check_path()
                db = sqlite3.connect('file:' + quote(str(self.path), safe='/') + ('?mode=rw' if write else '?mode=ro'),
                    uri=True, timeout=10, isolation_level=None)
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('PRAGMA trusted_schema=OFF')
                if initialize:
                    if _schema(db):
                        raise PiStoreError('invalid_schema')
                    db.execute('BEGIN IMMEDIATE')
                    for sql in _DDL:
                        db.execute(sql)
                    db.execute('PRAGMA application_id=' + str(APPLICATION_ID))
                    db.execute('PRAGMA user_version=' + str(SCHEMA_VERSION))
                else:
                    if (db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                            or db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION
                            or _schema(db) != _EXPECTED_SCHEMA):
                        raise PiStoreError('invalid_schema')
                    if not write:
                        db.execute('PRAGMA query_only=ON')
                    db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                yield db
                self._check_path()
                if write:
                    db.commit()
            except PiStoreError:
                raise
            except sqlite3.IntegrityError:
                raise PiStoreError('conflict') from None
            except sqlite3.Error:
                raise PiStoreError('io_failed') from None
            except (OSError, NativeSessionStoreError):
                raise PiStoreError('scope_changed') from None
            finally:
                if db is not None:
                    if db.in_transaction:
                        db.rollback()
                    db.close()

    @staticmethod
    def _binding(db, chat_id):
        row = db.execute('SELECT * FROM pi_bindings WHERE chat_id=?', (chat_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        _uuid(result['chat_id'])
        for prefix in ('cwd', 'session_dir'):
            descriptor = _directory(_path(result[prefix]))
            try:
                if _identity(os.fstat(descriptor)) != (result[prefix+'_device'], result[prefix+'_inode']):
                    raise PiStoreError('scope_changed')
            finally:
                os.close(descriptor)
        if (result['session_file'] is None) != (result['session_id'] is None):
            raise PiStoreError('invalid_schema')
        if result['session_file'] is not None:
            path = _path(result['session_file'])
            if path.suffix != '.jsonl' or Path(result['session_dir']) not in path.parents:
                raise PiStoreError('invalid_schema')
            validate_session_id(result['session_id'])
        return result

    @staticmethod
    def _operation(db, client_id):
        row = db.execute('SELECT * FROM pi_operations WHERE client_id=?', (client_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        _token(result['client_id']); _uuid(result['chat_id'])
        if result['status'] not in STATUSES or type(result['request_sha256']) is not str or not _SHA.fullmatch(result['request_sha256']):
            raise PiStoreError('invalid_schema')
        return result

    def bind(self, chat_id, cwd, session_dir):
        chat_id = _uuid(chat_id)
        try:
            cwd, session_dir = _path(cwd), _path(session_dir)
            identities = []
            for path in (cwd, session_dir):
                fd = _directory(path)
                try:
                    identities.extend(_identity(os.fstat(fd)))
                finally:
                    os.close(fd)
        except NativeSessionStoreError:
            raise PiStoreError('invalid_arguments') from None
        except OSError:
            raise PiStoreError('scope_changed') from None
        with self._db(write=True) as db:
            old = self._binding(db, chat_id)
            expected = (str(cwd), *identities[:2], str(session_dir), *identities[2:])
            if old:
                if tuple(old[k] for k in ('cwd','cwd_device','cwd_inode','session_dir','session_dir_device','session_dir_inode')) != expected:
                    raise PiStoreError('conflict')
                return old
            now = _stamp()
            db.execute('INSERT INTO pi_bindings VALUES(?,?,?,?,?,?,?,NULL,NULL,?,?)', (chat_id, *expected, now, now))
            return self._binding(db, chat_id)

    def binding(self, chat_id):
        with self._db() as db:
            return self._binding(db, _uuid(chat_id))

    def sources(self):
        """Persisted native IDs only; do not inspect session files or cwd."""
        with self._db() as db:
            return [('pi', validate_session_id(row[0])) for row in db.execute(
                'SELECT session_id FROM pi_bindings WHERE session_id IS NOT NULL ORDER BY session_id')]

    def source_bindings(self, *, include_activity=False):
        """All persisted native owners, including unpublished rows; no file read."""
        if type(include_activity) is not bool:
            raise PiStoreError('invalid_arguments')
        with self._db() as db:
            activity = ',a.activity_at' if include_activity else ''
            join = (" LEFT JOIN (SELECT chat_id,MAX(updated_at) activity_at FROM pi_operations "
                    "WHERE status IN ('accepted','completed','failed','interrupted') GROUP BY chat_id) "
                    "a ON a.chat_id=b.chat_id") if include_activity else ''
            return [{'chat_id': _uuid(row['chat_id']), 'session_id': validate_session_id(row['session_id']),
                     **({'activity_at': row['activity_at']} if include_activity else {})}
                    for row in db.execute('SELECT b.chat_id,b.session_id' + activity + ' FROM pi_bindings b' + join +
                        ' WHERE b.session_id IS NOT NULL ORDER BY b.session_id')]

    def adopt(self, chat_id, cwd, session_dir, session_file, session_id):
        """Atomically reserve an existing identity, without an operation receipt.

        The caller proves the source tree/header. This primitive only anchors
        exact directories and a present regular file; it never repairs a draft
        or changes an old binding. Even rejected/unknown receipts block adopt.
        """
        chat_id, session_id = _uuid(chat_id), validate_session_id(session_id)
        descriptors = []
        try:
            cwd, session_dir, path = _path(cwd), _path(session_dir), _path(session_file)
            if path.suffix != '.jsonl' or path.parent != session_dir:
                raise PiStoreError('invalid_arguments')
            identities = []
            for directory in (cwd, session_dir):
                fd = _directory(directory); descriptors.append(fd)
                identities.extend(_identity(os.fstat(fd)))
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptors[1])
            descriptors.append(fd)
            info = os.fstat(fd); self._regular(info)
            expected = (str(cwd), *identities[:2], str(session_dir), *identities[2:], str(path), session_id)
            with self._db(write=True) as db:
                old = self._binding(db, chat_id)
                if db.execute('SELECT 1 FROM pi_operations WHERE chat_id=?', (chat_id,)).fetchone():
                    raise PiStoreError('conflict')
                if old is not None:
                    if tuple(old[key] for key in ('cwd','cwd_device','cwd_inode','session_dir',
                            'session_dir_device','session_dir_inode','session_file','session_id')) != expected:
                        raise PiStoreError('conflict')
                else:
                    now = _stamp()
                    db.execute('INSERT INTO pi_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?)', (chat_id,*expected,now,now))
                current = os.stat(path.name, dir_fd=descriptors[1], follow_symlinks=False)
                self._regular(current)
                if _identity(current) != _identity(info):
                    raise PiStoreError('scope_changed')
                return self._binding(db, chat_id)
        except PiStoreError:
            raise
        except NativeSessionStoreError:
            raise PiStoreError('invalid_arguments') from None
        except OSError:
            raise PiStoreError('scope_changed') from None
        finally:
            for fd in reversed(descriptors): os.close(fd)

    def attach(self, chat_id, session_file, session_id):
        chat_id, session_id = _uuid(chat_id), validate_session_id(session_id)
        try:
            path = _path(session_file)
        except NativeSessionStoreError:
            raise PiStoreError('invalid_arguments') from None
        with self._db(write=True) as db:
            old = self._binding(db, chat_id)
            if old is None:
                raise PiStoreError('not_found')
            if path.suffix != '.jsonl' or Path(old['session_dir']) not in path.parents:
                raise PiStoreError('invalid_arguments')
            if old['session_file'] is not None:
                if (old['session_file'], old['session_id']) != (str(path), session_id):
                    raise PiStoreError('conflict')
                return old
            fd = _directory(path.parent)
            try:
                try:
                    info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    info = None  # Native Pi can defer its first file flush.
                if info is not None:
                    self._regular(info)
            finally:
                os.close(fd)
            db.execute('UPDATE pi_bindings SET session_file=?,session_id=?,updated_at=? WHERE chat_id=?',
                (str(path), session_id, _stamp(), chat_id))
            return self._binding(db, chat_id)

    def begin(self, chat_id, client_id, request):
        chat_id = _uuid(chat_id)
        try:
            client_id, digest = _token(client_id), request_sha256(request)
        except (NativeSessionStoreError, PiMaterialProtocolError):
            raise PiStoreError('invalid_arguments') from None
        with self._db(write=True) as db:
            binding = self._binding(db, chat_id)
            if binding is None:
                raise PiStoreError('not_found')
            old = self._operation(db, client_id)
            if old:
                if (old['chat_id'], old['request_sha256']) != (chat_id, digest):
                    raise PiStoreError('conflict')
                return dict(old, created=False)
            if binding['session_id'] is None:
                raise PiStoreError('conflict')
            if db.execute("SELECT 1 FROM pi_operations WHERE chat_id=? AND status IN ('submitted_unknown','accepted')", (chat_id,)).fetchone():
                raise PiStoreError('busy')
            now = _stamp()
            db.execute("INSERT INTO pi_operations VALUES(?,?,?,'submitted_unknown',?,?)", (client_id, chat_id, digest, now, now))
            return dict(self._operation(db, client_id), created=True)

    def operation(self, client_id):
        try:
            client_id = _token(client_id)
        except NativeSessionStoreError:
            raise PiStoreError('invalid_arguments') from None
        with self._db() as db:
            return self._operation(db, client_id)

    def pending(self, chat_id):
        return self._operations(chat_id, pending=True)

    def operations(self, chat_id, limit=20):
        if type(limit) is not int or not 1 <= limit <= 128:
            raise PiStoreError('invalid_arguments')
        return self._operations(chat_id, limit=limit)

    def _operations(self, chat_id, *, pending=False, limit=128):
        chat_id = _uuid(chat_id)
        with self._db() as db:
            if self._binding(db, chat_id) is None:
                raise PiStoreError('not_found')
            where = " AND status IN ('submitted_unknown','accepted')" if pending else ''
            rows = db.execute('SELECT client_id FROM pi_operations WHERE chat_id=?' + where +
                ' ORDER BY created_at DESC,client_id LIMIT ?', (chat_id, limit)).fetchall()
            return [self._operation(db, row['client_id']) for row in rows]

    def _transition(self, client_id, source, target):
        try:
            client_id = _token(client_id)
        except NativeSessionStoreError:
            raise PiStoreError('invalid_arguments') from None
        with self._db(write=True) as db:
            old = self._operation(db, client_id)
            if old is None:
                raise PiStoreError('not_found')
            self._binding(db, old['chat_id'])
            if old['status'] == target:
                return old
            if old['status'] != source:
                raise PiStoreError('conflict')
            db.execute('UPDATE pi_operations SET status=?,updated_at=? WHERE client_id=?', (target, _stamp(), client_id))
            return self._operation(db, client_id)

    def accepted(self, client_id):
        return self._transition(client_id, 'submitted_unknown', 'accepted')

    def rejected(self, client_id):
        return self._transition(client_id, 'submitted_unknown', 'rejected')

    def finish(self, client_id, status):
        if type(status) is not str or status not in TERMINAL:
            raise PiStoreError('invalid_arguments')
        return self._transition(client_id, 'accepted', status)

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            # Close, rather than LOCK_UN, so a forked child's cleanup cannot
            # release its parent's shared open-file-description lock.
            for field in ('_file_fd', '_lock_fd', '_parent_fd'):
                fd = getattr(self, field)
                if fd is not None:
                    os.close(fd)
                    setattr(self, field, None)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
