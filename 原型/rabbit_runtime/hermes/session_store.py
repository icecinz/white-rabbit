"""Hermes metadata ownership and conservative receipts; no transcript copy.

The file/lease discipline follows the existing Pi ledger. Hermes owns state.db
and messages. This independent database stores only a profile/cwd anchor,
verified native keys and application receipts. Unknown sends never auto-replay.
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
    validate_path as _path, validate_token as _token, request_sha256)


APPLICATION_ID = 0x52484E53  # RHNS; separate from shared chat and Codex ledgers.
SCHEMA_VERSION = 1
PENDING = frozenset(('submitted_unknown', 'accepted'))
TERMINAL = frozenset(('completed', 'failed', 'interrupted'))
STATUSES = PENDING | TERMINAL | {'rejected'}
_SESSION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
MAX_ADOPT_KEYS = 128
_MESSAGES = {
    'invalid_arguments': 'Hermes 会话记录参数无效。',
    'conflict': 'Hermes 会话身份或请求指纹不一致，未覆盖原记录。',
    'busy': 'Hermes 记录库已有持有者，或这段会话仍有待确认请求。',
    'closed': 'Hermes 会话记录库已关闭。',
    'readonly': 'Hermes 会话记录库为只读。',
    'not_found': '找不到这段 Hermes 会话或请求回执。',
    'scope_changed': 'Hermes 记录库或绑定目录身份变化，未继续操作。',
    'invalid_schema': '这不是受支持的独立 Hermes 记录库。',
    'io_failed': 'Hermes 会话记录暂时无法读取或保存。',
    'wrong_process': '当前进程不能借用其他进程的 Hermes 记录库持有权。',
}


class HermesStoreError(ValueError):
    def __init__(self, code):
        self.code = code if code in _MESSAGES else 'io_failed'
        super().__init__(_MESSAGES[self.code])


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise HermesStoreError('invalid_arguments') from None
    return value


def validate_session_id(value):
    # Hermes permits explicit alphanumeric/dot/dash/underscore IDs, not only UUIDs.
    if type(value) is not str or not _SESSION_ID.fullmatch(value):
        raise HermesStoreError('invalid_arguments')
    return value


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


_DDL = (
    '''CREATE TABLE hermes_bindings (
        chat_id TEXT PRIMARY KEY, cwd TEXT NOT NULL, cwd_device INTEGER NOT NULL, cwd_inode INTEGER NOT NULL,
        profile_home TEXT, home_device INTEGER, home_inode INTEGER, session_key TEXT UNIQUE,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK((profile_home IS NULL) = (home_device IS NULL)),
        CHECK((profile_home IS NULL) = (home_inode IS NULL)),
        CHECK(session_key IS NULL OR profile_home IS NOT NULL))''',
    '''CREATE TABLE hermes_keys (
        session_key TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES hermes_bindings(chat_id))''',
    '''CREATE TABLE hermes_operations (
        client_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES hermes_bindings(chat_id),
        request_sha256 TEXT NOT NULL, status TEXT NOT NULL
        CHECK(status IN ('submitted_unknown','accepted','rejected','completed','failed','interrupted')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
    '''CREATE UNIQUE INDEX hermes_one_pending ON hermes_operations(chat_id)
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


class HermesNativeStore:
    def __init__(self, path, *, readonly=False):
        if type(readonly) is not bool:
            raise HermesStoreError('invalid_arguments')
        try:
            self.path = _path(path)
        except NativeSessionStoreError:
            raise HermesStoreError('invalid_arguments') from None
        if not self.path.name:
            raise HermesStoreError('invalid_arguments')
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
                        raise HermesStoreError('busy') from None
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
            if isinstance(error, HermesStoreError):
                raise
            if isinstance(error, sqlite3.Error):
                raise HermesStoreError('invalid_schema') from None
            if isinstance(error, (OSError, NativeSessionStoreError)):
                raise HermesStoreError('scope_changed') from None
            raise

    @staticmethod
    def _regular(info):
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise HermesStoreError('scope_changed')

    def maintenance_state(self):
        """Observe the original pending-operation count without reading bodies."""
        from ..maintenance import count_rows
        specs = {'pending_prompts':('hermes_operations', "status IN ('submitted_unknown','accepted')", ())}
        return count_rows(self.path, specs, lock=self.lock, expected_identity=self._file_identity,
            check_path=self._check_path)

    def _check_path(self):
        if self._closed:
            raise HermesStoreError('closed')
        if self._owner_pid != os.getpid():
            raise HermesStoreError('wrong_process')
        parent = _directory(self.path.parent)
        try:
            if _identity(os.fstat(parent)) != self._parent_identity:
                raise HermesStoreError('scope_changed')
        finally:
            os.close(parent)
        for name in (self.path.name, self._lock_name, self.path.name+'-journal', self.path.name+'-wal', self.path.name+'-shm'):
            try:
                info = os.stat(name, dir_fd=self._parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if name in (self.path.name, self._lock_name):
                    raise HermesStoreError('scope_changed') from None
                continue
            self._regular(info)
            expected = self._file_identity if name == self.path.name else self._lock_identity if name == self._lock_name else None
            if expected is not None and _identity(info) != expected:
                raise HermesStoreError('scope_changed')

    @contextmanager
    def _db(self, *, write=False, initialize=False):
        with self.lock:
            if self._closed:
                raise HermesStoreError('closed')
            if write and self.readonly:
                raise HermesStoreError('readonly')
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
                        raise HermesStoreError('invalid_schema')
                    db.execute('BEGIN IMMEDIATE')
                    for sql in _DDL:
                        db.execute(sql)
                    db.execute('PRAGMA application_id=' + str(APPLICATION_ID))
                    db.execute('PRAGMA user_version=' + str(SCHEMA_VERSION))
                else:
                    if (db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                            or db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION
                            or _schema(db) != _EXPECTED_SCHEMA):
                        raise HermesStoreError('invalid_schema')
                    if not write:
                        db.execute('PRAGMA query_only=ON')
                    db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                yield db
                self._check_path()
                if write:
                    db.commit()
            except HermesStoreError:
                raise
            except sqlite3.IntegrityError:
                raise HermesStoreError('conflict') from None
            except sqlite3.Error:
                raise HermesStoreError('io_failed') from None
            except (OSError, NativeSessionStoreError):
                raise HermesStoreError('scope_changed') from None
            finally:
                if db is not None:
                    if db.in_transaction:
                        db.rollback()
                    db.close()

    @staticmethod
    def _binding(db, chat_id):
        row = db.execute('SELECT * FROM hermes_bindings WHERE chat_id=?', (chat_id,)).fetchone()
        if row is None: return None
        result = dict(row); _uuid(result['chat_id'])
        for field, prefix in (('cwd', 'cwd'), ('profile_home', 'home')):
            if result[field] is None: continue
            descriptor = _directory(_path(result[field]))
            try:
                if _identity(os.fstat(descriptor)) != (result[prefix+'_device'], result[prefix+'_inode']):
                    raise HermesStoreError('scope_changed')
            finally: os.close(descriptor)
        if result['session_key'] is not None: validate_session_id(result['session_key'])
        return result

    @staticmethod
    def _operation(db, client_id):
        row = db.execute('SELECT * FROM hermes_operations WHERE client_id=?', (client_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        _token(result['client_id']); _uuid(result['chat_id'])
        if result['status'] not in STATUSES or type(result['request_sha256']) is not str or not _SHA.fullmatch(result['request_sha256']):
            raise HermesStoreError('invalid_schema')
        return result

    def bind(self, chat_id, cwd):
        chat_id, cwd = _uuid(chat_id), _path(cwd)
        descriptor = _directory(cwd)
        try: identity = _identity(os.fstat(descriptor))
        finally: os.close(descriptor)
        with self._db(write=True) as db:
            old = self._binding(db, chat_id)
            if old:
                if (old['cwd'], old['cwd_device'], old['cwd_inode']) != (str(cwd), *identity):
                    raise HermesStoreError('conflict')
                return old
            now = _stamp()
            db.execute('INSERT INTO hermes_bindings VALUES(?,?,?,?,NULL,NULL,NULL,NULL,?,?)',
                       (chat_id, str(cwd), *identity, now, now))
            return self._binding(db, chat_id)

    def binding(self, chat_id):
        with self._db() as db: return self._binding(db, _uuid(chat_id))

    def anchor_home(self, chat_id, home):
        chat_id, home = _uuid(chat_id), _path(home)
        descriptor = _directory(home)
        try: identity = _identity(os.fstat(descriptor))
        finally: os.close(descriptor)
        with self._db(write=True) as db:
            old = self._binding(db, chat_id)
            if old is None: raise HermesStoreError('not_found')
            if old['profile_home'] is not None:
                if (old['profile_home'], old['home_device'], old['home_inode']) != (str(home), *identity):
                    raise HermesStoreError('conflict')
                return old
            db.execute('UPDATE hermes_bindings SET profile_home=?,home_device=?,home_inode=?,updated_at=? WHERE chat_id=?',
                       (str(home), *identity, _stamp(), chat_id))
            return self._binding(db, chat_id)

    def attach(self, chat_id, session_key, *, previous=None):
        """Caller proves a compression edge before advancing an existing key."""
        chat_id, session_key = _uuid(chat_id), validate_session_id(session_key)
        if previous is not None: previous = validate_session_id(previous)
        with self._db(write=True) as db:
            old = self._binding(db, chat_id)
            if old is None: raise HermesStoreError('not_found')
            if old['profile_home'] is None: raise HermesStoreError('conflict')
            if old['session_key'] == session_key: return old
            if old['session_key'] != previous: raise HermesStoreError('conflict')
            db.execute('INSERT INTO hermes_keys VALUES(?,?)', (session_key, chat_id))
            db.execute('UPDATE hermes_bindings SET session_key=?,updated_at=? WHERE chat_id=?',
                       (session_key, _stamp(), chat_id))
            return self._binding(db, chat_id)

    def sources(self):
        with self._db() as db:
            return [('hermes', validate_session_id(row['session_key'])) for row in
                    db.execute('SELECT session_key FROM hermes_keys ORDER BY session_key')]

    def source_bindings(self, *, include_activity=False):
        """All persisted key owners, including unpublished rows; metadata only."""
        if type(include_activity) is not bool:
            raise HermesStoreError('invalid_arguments')
        with self._db() as db:
            activity = ',a.activity_at' if include_activity else ''
            join = (" LEFT JOIN (SELECT chat_id,MAX(updated_at) activity_at FROM hermes_operations "
                    "WHERE status IN ('accepted','completed','failed','interrupted') GROUP BY chat_id) "
                    "a ON a.chat_id=b.chat_id") if include_activity else ''
            return [{'chat_id': _uuid(row['chat_id']), 'session_id': validate_session_id(row['session_key']),
                     **({'activity_at': row['activity_at']} if include_activity else {})}
                    for row in db.execute('SELECT b.chat_id,b.session_key' + activity + ' FROM hermes_keys b' + join +
                        ' ORDER BY b.session_key')]

    def adopt(self, chat_id, cwd, profile_home, session_keys):
        """Reserve a proven root-to-tip chain without inventing a native receipt.

        Only an unused exact binding can be retried or extended here. Once an
        operation exists, the published owner's ordinary attach path owns key
        advances. No old keys, receipts or schema objects are replaced.
        """
        chat_id, cwd, home = _uuid(chat_id), _path(cwd), _path(profile_home)
        if (type(session_keys) not in (list, tuple) or not 1 <= len(session_keys) <= MAX_ADOPT_KEYS):
            raise HermesStoreError('invalid_arguments')
        keys = tuple(validate_session_id(key) for key in session_keys)
        if len(set(keys)) != len(keys):
            raise HermesStoreError('invalid_arguments')
        scopes = []
        for directory in (cwd, home):
            descriptor = _directory(directory)
            try: scopes.append(_identity(os.fstat(descriptor)))
            finally: os.close(descriptor)
        with self._db(write=True) as db:
            old = self._binding(db, chat_id)
            if db.execute('SELECT 1 FROM hermes_operations WHERE chat_id=? LIMIT 1', (chat_id,)).fetchone():
                raise HermesStoreError('conflict')
            owned = {row[0] for row in db.execute('SELECT session_key FROM hermes_keys WHERE chat_id=?', (chat_id,))}
            if old is not None:
                if ((old['cwd'], old['cwd_device'], old['cwd_inode'], old['profile_home'],
                     old['home_device'], old['home_inode']) != (str(cwd), *scopes[0], str(home), *scopes[1])
                        or old['session_key'] not in keys
                        or owned != set(keys[:keys.index(old['session_key'])+1])):
                    raise HermesStoreError('conflict')
            for key in keys:
                owner = db.execute('SELECT chat_id FROM hermes_keys WHERE session_key=?', (key,)).fetchone()
                if owner is not None and owner[0] != chat_id:
                    raise HermesStoreError('conflict')
            if old is None:
                now = _stamp()
                db.execute('INSERT INTO hermes_bindings VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (chat_id, str(cwd), *scopes[0], str(home), *scopes[1], keys[-1], now, now))
            elif old['session_key'] != keys[-1]:
                db.execute('UPDATE hermes_bindings SET session_key=?,updated_at=? WHERE chat_id=?',
                           (keys[-1], _stamp(), chat_id))
            db.executemany('INSERT OR IGNORE INTO hermes_keys VALUES(?,?)', [(key, chat_id) for key in keys])
            return self._binding(db, chat_id)

    def begin(self, chat_id, client_id, request):
        chat_id = _uuid(chat_id)
        try:
            client_id, digest = _token(client_id), request_sha256(request)
        except NativeSessionStoreError:
            raise HermesStoreError('invalid_arguments') from None
        with self._db(write=True) as db:
            binding = self._binding(db, chat_id)
            if binding is None:
                raise HermesStoreError('not_found')
            old = self._operation(db, client_id)
            if old:
                if (old['chat_id'], old['request_sha256']) != (chat_id, digest):
                    raise HermesStoreError('conflict')
                return dict(old, created=False)
            if binding['session_key'] is None:
                raise HermesStoreError('conflict')
            if db.execute("SELECT 1 FROM hermes_operations WHERE chat_id=? AND status IN ('submitted_unknown','accepted')", (chat_id,)).fetchone():
                raise HermesStoreError('busy')
            now = _stamp()
            db.execute("INSERT INTO hermes_operations VALUES(?,?,?,'submitted_unknown',?,?)", (client_id, chat_id, digest, now, now))
            return dict(self._operation(db, client_id), created=True)

    def operation(self, client_id):
        try:
            client_id = _token(client_id)
        except NativeSessionStoreError:
            raise HermesStoreError('invalid_arguments') from None
        with self._db() as db:
            return self._operation(db, client_id)

    def pending(self, chat_id):
        return self._operations(chat_id, pending=True)

    def operations(self, chat_id, limit=20):
        if type(limit) is not int or not 1 <= limit <= 128:
            raise HermesStoreError('invalid_arguments')
        return self._operations(chat_id, limit=limit)

    def _operations(self, chat_id, *, pending=False, limit=128):
        chat_id = _uuid(chat_id)
        with self._db() as db:
            if self._binding(db, chat_id) is None:
                raise HermesStoreError('not_found')
            where = " AND status IN ('submitted_unknown','accepted')" if pending else ''
            rows = db.execute('SELECT client_id FROM hermes_operations WHERE chat_id=?' + where +
                ' ORDER BY created_at DESC,client_id LIMIT ?', (chat_id, limit)).fetchall()
            return [self._operation(db, row['client_id']) for row in rows]

    def _transition(self, client_id, source, target):
        try:
            client_id = _token(client_id)
        except NativeSessionStoreError:
            raise HermesStoreError('invalid_arguments') from None
        with self._db(write=True) as db:
            old = self._operation(db, client_id)
            if old is None:
                raise HermesStoreError('not_found')
            self._binding(db, old['chat_id'])
            if old['status'] == target:
                return old
            if old['status'] != source:
                raise HermesStoreError('conflict')
            db.execute('UPDATE hermes_operations SET status=?,updated_at=? WHERE client_id=?', (target, _stamp(), client_id))
            return self._operation(db, client_id)

    def accepted(self, client_id):
        return self._transition(client_id, 'submitted_unknown', 'accepted')

    def rejected(self, client_id):
        return self._transition(client_id, 'submitted_unknown', 'rejected')

    def finish(self, client_id, status):
        if type(status) is not str or status not in TERMINAL:
            raise HermesStoreError('invalid_arguments')
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
