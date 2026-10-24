"""Independent DSH metadata and durable request/message/turn correlation.

The original full profile owns the session event log and all model execution.
No compressed log or credential is opened here. A freshly committed created
flag grants one forward; reloaded unknown requests never grant another.
turn/end is recorded separately from a proven native flush durability barrier.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
from urllib.parse import quote
import uuid

from ..native_session_store import (NativeSessionStoreError, open_directory as _directory, file_identity as _identity,
    validate_path as _path, validate_token as _token, validate_native_id as _native_id, request_sha256)


APPLICATION_ID = 0x52444E53  # RDNS; independent from shared and other native ledgers.
SCHEMA_VERSION = 2
PENDING = frozenset(('submitted_unknown', 'accepted'))
REASON_STATUSES = {'completed': 'completed', 'aborted': 'interrupted',
    'interrupted': 'interrupted', 'blocked': 'failed', 'error': 'failed',
    'max-tokens': 'failed'}
TERMINAL = frozenset(('completed', 'failed', 'interrupted', 'command_completed'))
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_MESSAGES = {
    'invalid_arguments': 'DSH 原生记录参数无效。',
    'conflict': 'DSH 原生身份、事件或请求指纹不一致。',
    'busy': 'DSH 记录库已有持有者，或原请求仍待核对。',
    'closed': 'DSH 原生记录库已关闭。',
    'readonly': 'DSH 原生记录库为只读。',
    'not_found': '找不到 DSH 原生绑定或请求回执。',
    'scope_changed': 'DSH 记录库或固定工作目录身份变化。',
    'invalid_schema': '这不是受支持的独立 DSH 记录库。',
    'io_failed': 'DSH 原生记录暂时无法读取或保存。',
    'wrong_process': '不能借用其他进程的 DSH 记录库持有权。',
}


class DSHStoreError(ValueError):
    def __init__(self, code):
        self.code = code if code in _MESSAGES else 'io_failed'
        super().__init__(_MESSAGES[self.code])


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise DSHStoreError('invalid_arguments') from None
    return value


def _id(value):
    try:
        return _native_id(value)
    except NativeSessionStoreError:
        raise DSHStoreError('invalid_arguments') from None


def _epoch(value):
    try:
        return _token(value)
    except NativeSessionStoreError:
        raise DSHStoreError('invalid_arguments') from None


def _finite_text(value, limit=256):
    if (type(value) is not str or not 1 <= len(value) <= limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise DSHStoreError('invalid_arguments')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise DSHStoreError('invalid_arguments') from None
    return value


def _seq(value, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value <= 2**53-1:
        raise DSHStoreError('invalid_arguments')
    return value


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


_V1_DDL = (
    '''CREATE TABLE dsh_bindings (
        chat_id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE,
        cwd TEXT NOT NULL, cwd_device INTEGER NOT NULL, cwd_inode INTEGER NOT NULL,
        agent_preset TEXT, preset_known INTEGER NOT NULL CHECK(preset_known IN (0,1)),
        native_location TEXT, native_revision TEXT, durable_seq INTEGER,
        created_at TEXT NOT NULL,
        CHECK(preset_known=1 OR agent_preset IS NULL),
        CHECK(native_revision IS NOT NULL OR (native_location IS NULL AND durable_seq IS NULL)))''',
    '''CREATE TABLE dsh_creations (
        chat_id TEXT PRIMARY KEY REFERENCES dsh_bindings(chat_id),
        client_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL, epoch TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('submitted_unknown','confirmed')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
    '''CREATE TABLE dsh_prompts (
        client_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES dsh_bindings(chat_id),
        session_id TEXT NOT NULL, epoch TEXT NOT NULL,
        request_sha256 TEXT NOT NULL, message_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN
            ('submitted_unknown','accepted','rejected','completed','failed','interrupted','command_completed')),
        native_message_id TEXT, native_insert_seq INTEGER, native_turn INTEGER,
        native_turn_start_seq INTEGER, native_message_seq INTEGER,
        native_end_seq INTEGER, native_reason TEXT, durable_seq INTEGER,
        native_command_sha256 TEXT, native_error_code TEXT, native_error_sha256 TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK((native_message_id IS NULL)=(native_insert_seq IS NULL)),
        CHECK((native_turn IS NULL)=(native_turn_start_seq IS NULL)),
        CHECK((native_end_seq IS NULL)=(native_reason IS NULL)),
        CHECK((status='command_completed')=(native_command_sha256 IS NOT NULL)),
        CHECK(native_command_sha256 IS NULL OR (native_message_id IS NULL AND native_turn IS NULL
            AND native_end_seq IS NULL AND durable_seq IS NULL)),
        CHECK((status='rejected')=(native_error_sha256 IS NOT NULL)),
        CHECK((native_error_sha256 IS NULL)=(native_error_code IS NULL)),
        CHECK(status<>'accepted' OR native_message_id IS NOT NULL),
        CHECK(status NOT IN ('completed','failed','interrupted')
            OR (native_message_id IS NOT NULL AND native_turn IS NOT NULL
                AND native_end_seq IS NOT NULL AND durable_seq>=native_end_seq)))''',
    '''CREATE UNIQUE INDEX dsh_one_pending ON dsh_prompts(chat_id)
        WHERE status IN ('submitted_unknown','accepted')''',
    '''CREATE UNIQUE INDEX dsh_message_owner ON dsh_prompts(session_id,native_message_id)
        WHERE native_message_id IS NOT NULL''',
    '''CREATE UNIQUE INDEX dsh_turn_owner ON dsh_prompts(session_id,native_turn)
        WHERE native_turn IS NOT NULL''',
    '''CREATE TABLE dsh_controls (
        client_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES dsh_bindings(chat_id),
        session_id TEXT NOT NULL, epoch TEXT NOT NULL,
        active_client_id TEXT NOT NULL REFERENCES dsh_prompts(client_id),
        request_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('submitted_unknown','acknowledged')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
    '''CREATE UNIQUE INDEX dsh_one_interrupt ON dsh_controls(active_client_id)
        WHERE status='submitted_unknown' ''',
)

ADOPTIONS_DDL = '''CREATE TABLE dsh_adoptions (
    chat_id TEXT PRIMARY KEY REFERENCES dsh_bindings(chat_id),
    source_anchor TEXT NOT NULL, created_at TEXT NOT NULL)'''
_DDL = _V1_DDL + (ADOPTIONS_DDL,)
_ANCHOR_FIELDS = frozenset(('session_id','cwd','cwd_device','cwd_inode',
    'source_root','root_device','root_inode','project_dir','project_device','project_inode',
    'session_dir','session_device','session_inode','source_path','file_device','file_inode','header_sha256'))
_ANCHOR_DIRECTORIES = (('cwd','cwd_device','cwd_inode'),('source_root','root_device','root_inode'),
    ('project_dir','project_device','project_inode'),('session_dir','session_device','session_inode'))


def _source_anchor(value):
    """Closed metadata codec. The trusted caller verifies the actual header."""
    if (type(value) is not dict or value.keys() != _ANCHOR_FIELDS
            or type(value['session_id']) is not str
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',value['session_id']) is None
            or type(value['header_sha256']) is not str or _SHA.fullmatch(value['header_sha256']) is None):
        raise DSHStoreError('invalid_arguments')
    result=dict(value)
    for name in ('cwd','source_root','project_dir','session_dir','source_path'):
        try:
            if (type(value[name]) is not str or len(value[name]) > 8192
                    or any(ord(c)<32 or ord(c)==127 for c in value[name]) or str(_path(value[name])) != value[name]):
                raise DSHStoreError('invalid_arguments')
        except NativeSessionStoreError:
            raise DSHStoreError('invalid_arguments') from None
    for name in _ANCHOR_FIELDS-{'session_id','cwd','source_root','project_dir','session_dir','source_path','header_sha256'}:
        if type(value[name]) is not int or not 0 <= value[name] <= 2**63-1:
            raise DSHStoreError('invalid_arguments')
    root,project,session,path=(Path(value[name]) for name in ('source_root','project_dir','session_dir','source_path'))
    if (project.parent != root or session.parent != project or session.name != value['session_id']
            or path.parent != session or path.name not in ('session.jsonl','session.jsonl.zstd')
            or len(json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()) > 48*1024):
        raise DSHStoreError('invalid_arguments')
    return result


def _anchor_json(raw):
    def pairs(items):
        value={}
        for key,item in items:
            if key in value:raise DSHStoreError('invalid_schema')
            value[key]=item
        return value
    try:
        if type(raw) is not str or len(raw.encode()) > 48*1024:raise DSHStoreError('invalid_schema')
        return _source_anchor(json.loads(raw,object_pairs_hook=pairs,
            parse_constant=lambda _value:(_ for _ in ()).throw(DSHStoreError('invalid_schema'))))
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise DSHStoreError('invalid_schema') from None


@contextmanager
def _source_scope(anchor):
    """Hold only bounded metadata FDs; never read transcript bytes."""
    descriptors=[]
    try:
        for path,device,inode in _ANCHOR_DIRECTORIES:
            fd=_directory(_path(anchor[path]));descriptors.append(fd)
            if _identity(os.fstat(fd)) != (anchor[device],anchor[inode]):raise DSHStoreError('scope_changed')
        path=Path(anchor['source_path'])
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=descriptors[-1]);descriptors.append(fd)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or _identity(info)!=(anchor['file_device'],anchor['file_inode']):
            raise DSHStoreError('scope_changed')
        alternate='session.jsonl' if path.name.endswith('.zstd') else 'session.jsonl.zstd'
        try:os.stat(alternate,dir_fd=descriptors[-2],follow_symlinks=False)
        except FileNotFoundError:pass
        else:raise DSHStoreError('scope_changed')
        yield
        # Recheck both physical objects and named paths before the DB commit.
        for (path,device,inode),opened in zip(_ANCHOR_DIRECTORIES,descriptors):
            current=_directory(_path(anchor[path]))
            try:
                if (_identity(os.fstat(current)) != (anchor[device],anchor[inode])
                        or _identity(os.fstat(opened)) != (anchor[device],anchor[inode])):
                    raise DSHStoreError('scope_changed')
            finally:os.close(current)
        named=os.stat(Path(anchor['source_path']).name,dir_fd=descriptors[-2],follow_symlinks=False)
        if (not stat.S_ISREG(named.st_mode) or named.st_nlink != 1
                or _identity(named) != _identity(os.fstat(fd)) or _identity(named)!=(anchor['file_device'],anchor['file_inode'])):
            raise DSHStoreError('scope_changed')
        try:os.stat(alternate,dir_fd=descriptors[-2],follow_symlinks=False)
        except FileNotFoundError:pass
        else:raise DSHStoreError('scope_changed')
    except DSHStoreError:raise
    except (OSError,NativeSessionStoreError):raise DSHStoreError('scope_changed') from None
    finally:
        for descriptor in reversed(descriptors):os.close(descriptor)


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


_EXPECTED_V1_SCHEMA = _expected_schema(_V1_DDL)
_EXPECTED_SCHEMA = _expected_schema(_DDL)


class DSHNativeStore:
    def __init__(self, path, *, readonly=False, allow_upgrade=False):
        """New stores use v2; only explicit writable opt-in upgrades exact v1."""
        if type(readonly) is not bool or type(allow_upgrade) is not bool:
            raise DSHStoreError('invalid_arguments')
        if readonly and allow_upgrade:raise DSHStoreError('readonly')
        try:
            self.path = _path(path)
        except NativeSessionStoreError:
            raise DSHStoreError('invalid_arguments') from None
        if not self.path.name:
            raise DSHStoreError('invalid_arguments')
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
                        raise DSHStoreError('busy') from None
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
            with self._db(write=created or allow_upgrade, initialize=created, upgrade=allow_upgrade):
                pass
        except BaseException as error:
            self.close()
            if isinstance(error, DSHStoreError):
                raise
            if isinstance(error, sqlite3.Error):
                raise DSHStoreError('invalid_schema') from None
            if isinstance(error, (OSError, NativeSessionStoreError)):
                raise DSHStoreError('scope_changed') from None
            raise

    @staticmethod
    def _regular(info):
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DSHStoreError('scope_changed')

    def maintenance_state(self):
        """Observe the three existing ledger counts without reading bodies."""
        from ..maintenance import count_rows
        specs = {'pending_creations':('dsh_creations', "status='submitted_unknown'", ()),
            'pending_prompts':('dsh_prompts', "status IN ('submitted_unknown','accepted')", ()),
            'unknown_controls':('dsh_controls', "status='submitted_unknown'", ())}
        return count_rows(self.path, specs, lock=self.lock, expected_identity=self._file_identity,
            check_path=self._check_path)

    def _check_path(self):
        if self._closed:
            raise DSHStoreError('closed')
        if self._owner_pid != os.getpid():
            raise DSHStoreError('wrong_process')
        parent = _directory(self.path.parent)
        try:
            if _identity(os.fstat(parent)) != self._parent_identity:
                raise DSHStoreError('scope_changed')
        finally:
            os.close(parent)
        for name in (self.path.name, self._lock_name, self.path.name+'-journal',
                     self.path.name+'-wal', self.path.name+'-shm'):
            try:
                info = os.stat(name, dir_fd=self._parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if name in (self.path.name, self._lock_name):
                    raise DSHStoreError('scope_changed') from None
                continue
            self._regular(info)
            expected = self._file_identity if name == self.path.name else self._lock_identity if name == self._lock_name else None
            if expected is not None and _identity(info) != expected:
                raise DSHStoreError('scope_changed')

    @contextmanager
    def _db(self, *, write=False, initialize=False, upgrade=False):
        with self.lock:
            if write and self.readonly:
                raise DSHStoreError('readonly')
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
                        raise DSHStoreError('invalid_schema')
                    db.execute('BEGIN IMMEDIATE')
                    for sql in _DDL:
                        db.execute(sql)
                    db.execute('PRAGMA application_id=' + str(APPLICATION_ID))
                    db.execute('PRAGMA user_version=' + str(SCHEMA_VERSION))
                else:
                    if not write:
                        db.execute('PRAGMA query_only=ON')
                    db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                    if db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
                        raise DSHStoreError('invalid_schema')
                    version=db.execute('PRAGMA user_version').fetchone()[0]
                    if version == 1 and upgrade:
                        if not write or self.readonly:raise DSHStoreError('readonly')
                        if _schema(db) != _EXPECTED_V1_SCHEMA or db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                            raise DSHStoreError('invalid_schema')
                        db.execute(ADOPTIONS_DDL)
                        db.execute('PRAGMA user_version='+str(SCHEMA_VERSION))
                    elif version != SCHEMA_VERSION:
                        raise DSHStoreError('invalid_schema')
                    if _schema(db) != _EXPECTED_SCHEMA:raise DSHStoreError('invalid_schema')
                yield db
                self._check_path()
                if write:
                    db.commit()
            except DSHStoreError:
                raise
            except sqlite3.IntegrityError:
                raise DSHStoreError('conflict') from None
            except sqlite3.Error:
                raise DSHStoreError('io_failed') from None
            except (OSError, NativeSessionStoreError):
                raise DSHStoreError('scope_changed') from None
            finally:
                if db is not None:
                    if db.in_transaction:
                        db.rollback()
                    db.close()


    @staticmethod
    def _binding(db, chat_id):
        row = db.execute('SELECT * FROM dsh_bindings WHERE chat_id=?', (chat_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        _uuid(value['chat_id']); _id(value['session_id'])
        fd = _directory(_path(value['cwd']))
        try:
            if _identity(os.fstat(fd)) != (value['cwd_device'], value['cwd_inode']):
                raise DSHStoreError('scope_changed')
        finally:
            os.close(fd)
        value['preset_known'] = bool(value['preset_known'])
        if value['agent_preset'] is not None:
            _finite_text(value['agent_preset'])
        value['native_location'] = json.loads(value['native_location']) if value['native_location'] is not None else None
        value['materialized'] = value['native_revision'] is not None
        if value['materialized']:
            _finite_text(value['native_revision'],4096)
        return value

    @staticmethod
    def _receipt(db, table, key, *, field='client_id'):
        # Names are private fixed literals, never caller-controlled SQL.
        row = db.execute('SELECT * FROM '+table+' WHERE '+field+'=?', (key,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        _uuid(value['chat_id']); _uuid(value['client_id']); _id(value['session_id']); _epoch(value['epoch'])
        for name in ('request_sha256','message_sha256'):
            if name in value and not _SHA.fullmatch(value[name]):
                raise DSHStoreError('invalid_schema')
        if table=='dsh_prompts':
            value['kind']='prompt'
            value['native_command_kind']='success' if value['native_command_sha256'] is not None else None
        elif table=='dsh_controls':
            value['kind']='interrupt'
        return value

    def bind(self, chat_id, cwd, session_id=None):
        """Reserve without observing a preset, starting an Agent or native I/O."""
        chat_id = _uuid(chat_id)
        if session_id is not None:
            session_id = _id(session_id)
        try:
            cwd = _path(cwd)
            fd = _directory(cwd)
            try:
                identity = _identity(os.fstat(fd))
            finally:
                os.close(fd)
        except NativeSessionStoreError:
            raise DSHStoreError('invalid_arguments') from None
        except OSError:
            raise DSHStoreError('scope_changed') from None
        with self._db(write=True) as db:
            old = self._binding(db,chat_id)
            if old:
                if ((old['cwd'],old['cwd_device'],old['cwd_inode'])!=(str(cwd),*identity)
                        or session_id is not None and old['session_id']!=session_id):
                    raise DSHStoreError('conflict')
                return old
            session_id = session_id or str(uuid.uuid4())
            db.execute('INSERT INTO dsh_bindings VALUES(?,?,?,?,?,NULL,0,NULL,NULL,NULL,?)',
                       (chat_id,session_id,str(cwd),*identity,_stamp()))
            return self._binding(db,chat_id)

    def binding(self, chat_id):
        with self._db() as db:
            return self._binding(db,_uuid(chat_id))

    def bindings(self):
        with self._db() as db:
            return [self._binding(db,row[0]) for row in db.execute(
                'SELECT chat_id FROM dsh_bindings ORDER BY created_at,chat_id').fetchall()]

    @staticmethod
    def _adoption(db, chat_id):
        row=db.execute('SELECT a.*,b.session_id,b.cwd,b.cwd_device,b.cwd_inode,b.agent_preset,b.preset_known '
            'FROM dsh_adoptions a JOIN dsh_bindings b ON b.chat_id=a.chat_id WHERE a.chat_id=?',(chat_id,)).fetchone()
        if row is None:return None
        anchor=_anchor_json(row['source_anchor'])
        if (row['session_id'] != anchor['session_id'] or row['cwd'] != anchor['cwd']
                or (row['cwd_device'],row['cwd_inode']) != (anchor['cwd_device'],anchor['cwd_inode'])
                or row['preset_known'] != 1):raise DSHStoreError('invalid_schema')
        _uuid(row['chat_id'])
        if row['agent_preset'] is not None:_finite_text(row['agent_preset'])
        return dict(chat_id=row['chat_id'],session_id=row['session_id'],agent_preset=row['agent_preset'],
            source_anchor=anchor,created_at=row['created_at'])

    def adoption(self, chat_id):
        """Detached saved metadata only; no source file/header/Agent read."""
        with self._db() as db:return self._adoption(db,_uuid(chat_id))

    def adopt(self, chat_id, cwd, session_id, agent_preset, source_anchor):
        """Reserve an existing source atomically, without native ACK or body.

        Header semantics/hash are the trusted adopter's responsibility. This
        primitive pins physical identities and permits only an unused exact
        reservation replay, never conversion of an ordinary creation/prompt.
        """
        chat_id=_uuid(chat_id);session_id=_id(session_id);anchor=_source_anchor(source_anchor)
        try:cwd=_path(cwd)
        except NativeSessionStoreError:raise DSHStoreError('invalid_arguments') from None
        if anchor['session_id'] != session_id or anchor['cwd'] != str(cwd):raise DSHStoreError('invalid_arguments')
        if agent_preset is not None:
            _finite_text(agent_preset)
            if any(c.isspace() for c in agent_preset):raise DSHStoreError('invalid_arguments')
        raw=json.dumps(anchor,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        with self._db(write=True) as db:
            with _source_scope(anchor):
                if any(db.execute('SELECT 1 FROM '+table+' WHERE chat_id=? LIMIT 1',(chat_id,)).fetchone()
                        for table in ('dsh_creations','dsh_prompts','dsh_controls')):
                    raise DSHStoreError('conflict')
                old=self._binding(db,chat_id);adoption=self._adoption(db,chat_id)
                if old is not None:
                    if (adoption is None or adoption['source_anchor'] != anchor or old['session_id'] != session_id
                            or old['agent_preset'] != agent_preset):raise DSHStoreError('conflict')
                else:
                    stamp=_stamp()
                    db.execute('INSERT INTO dsh_bindings VALUES(?,?,?,?,?,?,1,NULL,NULL,NULL,?)',
                        (chat_id,session_id,str(cwd),anchor['cwd_device'],anchor['cwd_inode'],agent_preset,stamp))
                    db.execute('INSERT INTO dsh_adoptions VALUES(?,?,?)',(chat_id,raw,stamp))
                return self._binding(db,chat_id)

    def sources(self):
        with self._db() as db:
            rows=db.execute('SELECT b.chat_id,b.session_id,EXISTS(SELECT 1 FROM dsh_adoptions a WHERE a.chat_id=b.chat_id) adopted '
                'FROM dsh_bindings b WHERE b.native_revision IS NOT NULL OR EXISTS '
                '(SELECT 1 FROM dsh_adoptions a WHERE a.chat_id=b.chat_id) ORDER BY b.session_id').fetchall()
            for row in rows:
                if row['adopted']:self._adoption(db,row['chat_id'])
            return [('dsh',_id(row['session_id'])) for row in rows]

    def source_bindings(self, *, include_activity=False):
        """Saved/adopted identities only; never resolve cwd or a transcript."""
        if type(include_activity) is not bool:
            raise DSHStoreError('invalid_arguments')
        with self._db() as db:
            activity = ',p.activity_at' if include_activity else ''
            join = (" LEFT JOIN (SELECT chat_id,MAX(updated_at) activity_at FROM dsh_prompts "
                    "WHERE status IN ('accepted','completed','failed','interrupted') GROUP BY chat_id) "
                    "p ON p.chat_id=b.chat_id") if include_activity else ''
            rows=db.execute('SELECT b.chat_id,b.session_id,EXISTS(SELECT 1 FROM dsh_adoptions a WHERE a.chat_id=b.chat_id) adopted' + activity +
                ' FROM dsh_bindings b' + join + ' WHERE b.native_revision IS NOT NULL OR EXISTS '
                '(SELECT 1 FROM dsh_adoptions a WHERE a.chat_id=b.chat_id) ORDER BY b.chat_id,b.session_id').fetchall()
            for row in rows:
                if row['adopted']:self._adoption(db,row['chat_id'])
            return [dict(chat_id=_uuid(row['chat_id']),session_id=_id(row['session_id']),
                         **({'activity_at': row['activity_at']} if include_activity else {})) for row in rows]

    def freeze_preset(self, chat_id, value):
        chat_id = _uuid(chat_id)
        if value is not None:
            value = _finite_text(value)
        with self._db(write=True) as db:
            old = self._binding(db,chat_id)
            if old is None:
                raise DSHStoreError('not_found')
            if old['preset_known']:
                if old['agent_preset']!=value:
                    raise DSHStoreError('conflict')
                return old
            db.execute('UPDATE dsh_bindings SET agent_preset=?,preset_known=1 WHERE chat_id=?',(value,chat_id))
            return self._binding(db,chat_id)

    @staticmethod
    def _proof(binding, proof, *, flush=False):
        required = {'session_id','cwd','agent_preset','materialized','location','revision'}
        optional = {'through_seq','live','flushed','durable_through_seq'}
        if type(proof) is not dict or not required<=proof.keys() or proof.keys()-required-optional:
            raise DSHStoreError('invalid_arguments')
        if (not binding['preset_known'] or any(proof[key]!=binding[key] for key in ('session_id','cwd','agent_preset'))
                or proof['materialized'] is not True):
            raise DSHStoreError('conflict')
        revision = _finite_text(proof['revision'],4096)
        location = proof['location']
        if location is not None:
            if type(location) is not dict or set(location)!={'kind','path'}:
                raise DSHStoreError('invalid_arguments')
            _finite_text(location['kind'],64)
            try:
                path = _path(location['path'])
            except NativeSessionStoreError:
                raise DSHStoreError('invalid_arguments') from None
            location = {'kind':location['kind'],'path':str(path)}
        if binding['materialized'] and location!=binding['native_location']:
            raise DSHStoreError('conflict')
        for field in ('through_seq','durable_through_seq'):
            if field in proof:
                _seq(proof[field])
        if 'live' in proof and type(proof['live']) is not bool:
            raise DSHStoreError('invalid_arguments')
        if 'flushed' in proof and type(proof['flushed']) is not bool:
            raise DSHStoreError('invalid_arguments')
        physical = proof.get('durable_through_seq') if proof.get('flushed') is True else None
        if flush and proof.get('flushed') is not True:
            raise DSHStoreError('conflict')
        if proof.get('flushed') is True and (physical is None or 'through_seq' not in proof
                or physical<proof['through_seq']):
            raise DSHStoreError('conflict')
        # Native through_seq is captured BEFORE awaiting flush. A later physical
        # prefix may legitimately be longer; only the captured barrier is the
        # app's durability claim, never a post-await moving watermark.
        durable = proof['through_seq'] if physical is not None else None
        if durable is not None and binding['durable_seq'] is not None and durable<binding['durable_seq']:
            raise DSHStoreError('conflict')
        return location,revision,durable

    @classmethod
    def _anchor(cls, db, binding, proof, *, flush=False):
        location,revision,durable = cls._proof(binding,proof,flush=flush)
        db.execute('UPDATE dsh_bindings SET native_location=?,native_revision=?,durable_seq=? WHERE chat_id=?',
            (json.dumps(location,ensure_ascii=False,sort_keys=True,separators=(',',':')) if location is not None else None,
             revision,durable if durable is not None else binding['durable_seq'],binding['chat_id']))

    def begin_create(self, chat_id, client_id, epoch):
        chat_id,client_id,epoch = _uuid(chat_id),_uuid(client_id),_epoch(epoch)
        with self._db(write=True) as db:
            binding = self._binding(db,chat_id)
            if binding is None:
                raise DSHStoreError('not_found')
            if self._adoption(db,chat_id) is not None:raise DSHStoreError('conflict')
            if not binding['preset_known']:
                raise DSHStoreError('conflict')
            old = self._receipt(db,'dsh_creations',chat_id,field='chat_id')
            if old:
                if old['client_id']!=client_id:
                    raise DSHStoreError('conflict')
                return dict(old,created=False)
            now = _stamp()
            db.execute("INSERT INTO dsh_creations VALUES(?,?,?,?,'submitted_unknown',?,?)",
                       (chat_id,client_id,binding['session_id'],epoch,now,now))
            return dict(self._receipt(db,'dsh_creations',chat_id,field='chat_id'),created=True)

    def creation(self, chat_id):
        with self._db() as db:
            self._binding(db,_uuid(chat_id))
            return self._receipt(db,'dsh_creations',chat_id,field='chat_id')

    def confirm_create(self, chat_id, client_id, session_id, epoch, proof):
        chat_id,client_id,session_id,epoch = _uuid(chat_id),_uuid(client_id),_id(session_id),_epoch(epoch)
        with self._db(write=True) as db:
            binding = self._binding(db,chat_id)
            if self._adoption(db,chat_id) is not None:raise DSHStoreError('conflict')
            old = self._receipt(db,'dsh_creations',chat_id,field='chat_id')
            if binding is None or old is None:
                raise DSHStoreError('not_found')
            if (old['client_id'],old['session_id'],old['epoch'])!=(client_id,session_id,epoch):
                raise DSHStoreError('conflict')
            self._anchor(db,binding,proof)
            db.execute("UPDATE dsh_creations SET status='confirmed',updated_at=? WHERE chat_id=?",(_stamp(),chat_id))
            return self._receipt(db,'dsh_creations',chat_id,field='chat_id')

    def confirm_existing(self, chat_id, proof):
        """Anchor actual native inspection; never create a native receipt."""
        chat_id=_uuid(chat_id)
        with self._db(write=True) as db:
            binding=self._binding(db,chat_id);adoption=self._adoption(db,chat_id)
            if binding is None or adoption is None:raise DSHStoreError('not_found')
            anchor=adoption['source_anchor']
            if (type(proof) is not dict or proof.get('location') != {'kind':'jsonl','path':anchor['source_path']}
                    or self._receipt(db,'dsh_creations',chat_id,field='chat_id') is not None):
                raise DSHStoreError('conflict')
            with _source_scope(anchor):
                self._anchor(db,binding,proof)
                return self._binding(db,chat_id)

    def begin(self, chat_id, client_id, message, epoch, *, request=None):
        chat_id,client_id,epoch = _uuid(chat_id),_uuid(client_id),_epoch(epoch)
        if type(message) is not str or not message or len(message)>1024*1024:
            raise DSHStoreError('invalid_arguments')
        try:
            if request is None:
                request = {'type':'prompt','message':message}
            elif (type(request) is not dict or set(request) != {'type','message'}
                    or request['type'] != 'prompt' or type(request['message']) is not str
                    or not (request['message'] == message or request['message'].startswith(message+'\n\n'))
                    or len(json.dumps(request,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()) > 900*1024):
                raise DSHStoreError('invalid_arguments')
            digest = request_sha256(request)
            message_digest = hashlib.sha256(message.encode('utf-8')).hexdigest()
        except (NativeSessionStoreError,UnicodeError):
            raise DSHStoreError('invalid_arguments') from None
        with self._db(write=True) as db:
            binding = self._binding(db,chat_id)
            if binding is None:
                raise DSHStoreError('not_found')
            old = self._receipt(db,'dsh_prompts',client_id)
            if old:
                if (old['chat_id'],old['request_sha256'],old['message_sha256'])!=(chat_id,digest,message_digest):
                    raise DSHStoreError('conflict')
                return dict(old,created=False)
            creation = self._receipt(db,'dsh_creations',chat_id,field='chat_id')
            if creation is None:
                if self._adoption(db,chat_id) is None or not binding['materialized']:
                    raise DSHStoreError('conflict')
            elif creation['status']!='confirmed' and creation['epoch']!=epoch:raise DSHStoreError('conflict')
            if self._active(db,chat_id) is not None:
                raise DSHStoreError('busy')
            now = _stamp()
            db.execute("INSERT INTO dsh_prompts(client_id,chat_id,session_id,epoch,request_sha256,message_sha256,status,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,'submitted_unknown',?,?)",
                       (client_id,chat_id,binding['session_id'],epoch,digest,message_digest,now,now))
            return dict(self._receipt(db,'dsh_prompts',client_id),created=True)

    @classmethod
    def _active(cls, db, chat_id):
        row = db.execute("SELECT client_id FROM dsh_prompts WHERE chat_id=? AND status IN ('submitted_unknown','accepted')",(chat_id,)).fetchone()
        return cls._receipt(db,'dsh_prompts',row[0]) if row else None

    def active_for_chat(self, chat_id):
        with self._db() as db:
            self._binding(db,_uuid(chat_id))
            return self._active(db,chat_id)

    def lookup(self, client_id):
        with self._db() as db:
            value = self._receipt(db,'dsh_prompts',_uuid(client_id))
            if value:
                self._binding(db,value['chat_id'])
            return value

    operation = lookup

    def operations(self, chat_id, limit=20):
        if type(limit) is not int or not 1<=limit<=128:
            raise DSHStoreError('invalid_arguments')
        with self._db() as db:
            chat_id = _uuid(chat_id)
            if self._binding(db,chat_id) is None:
                raise DSHStoreError('not_found')
            ids = db.execute('SELECT client_id FROM dsh_prompts WHERE chat_id=? '
                             'ORDER BY created_at DESC,client_id LIMIT ?',(chat_id,limit)).fetchall()
            return [self._receipt(db,'dsh_prompts',row[0]) for row in ids]

    def _target(self, db, client_id, session_id, epoch, *, table='dsh_prompts'):
        old = self._receipt(db,table,_uuid(client_id))
        if old is None:
            raise DSHStoreError('not_found')
        binding = self._binding(db,old['chat_id'])
        if (old['session_id'],old['epoch'])!=(_id(session_id),_epoch(epoch)) or binding['session_id']!=session_id:
            raise DSHStoreError('conflict')
        return old,binding

    def accepted(self, client_id, session_id, epoch, message_id, source_rpc_id, insert_seq, *, message_sha256=None):
        """Original identified inbox/message evidence, never just accepted:true."""
        client_id,message_id,insert_seq = _uuid(client_id),_id(message_id),_seq(insert_seq)
        if _uuid(source_rpc_id)!=client_id:
            raise DSHStoreError('conflict')
        with self._db(write=True) as db:
            old,_ = self._target(db,client_id,session_id,epoch)
            if message_sha256 is not None and message_sha256!=old['message_sha256']:
                raise DSHStoreError('conflict')
            if old['native_message_id'] is not None:
                if (old['native_message_id'],old['native_insert_seq'])!=(message_id,insert_seq) or old['status']=='rejected':
                    raise DSHStoreError('conflict')
                return old
            if old['status']!='submitted_unknown':
                raise DSHStoreError('conflict')
            db.execute("UPDATE dsh_prompts SET status='accepted',native_message_id=?,native_insert_seq=?,updated_at=? WHERE client_id=?",
                       (message_id,insert_seq,_stamp(),client_id))
            return self._receipt(db,'dsh_prompts',client_id)

    def claimed(self, client_id, session_id, epoch, message_id, turn, turn_start_seq, message_seq=None):
        """Caller correlates original claimed/user-message with its exact turn."""
        message_id,turn,turn_start_seq = _id(message_id),_seq(turn,positive=True),_seq(turn_start_seq)
        if message_seq is not None:
            message_seq = _seq(message_seq)
            if message_seq<=turn_start_seq:
                raise DSHStoreError('conflict')
        with self._db(write=True) as db:
            old,_ = self._target(db,client_id,session_id,epoch)
            if (old['native_message_id']!=message_id or old['native_insert_seq'] is None
                    or turn_start_seq<=old['native_insert_seq']):
                raise DSHStoreError('conflict')
            if old['native_turn'] is not None:
                if ((old['native_turn'],old['native_turn_start_seq'])!=(turn,turn_start_seq)
                        or old['native_message_seq'] is not None and message_seq is not None and old['native_message_seq']!=message_seq):
                    raise DSHStoreError('conflict')
                if old['native_message_seq'] is not None or message_seq is None:
                    return old
            if (old['native_end_seq'] is not None and message_seq is not None
                    and message_seq>=old['native_end_seq']):
                raise DSHStoreError('conflict')
            if old['status'] not in ({'accepted'} | TERMINAL):
                raise DSHStoreError('conflict')
            db.execute('UPDATE dsh_prompts SET native_turn=?,native_turn_start_seq=?,native_message_seq=?,updated_at=? WHERE client_id=?',
                       (turn,turn_start_seq,message_seq,_stamp(),client_id))
            return self._receipt(db,'dsh_prompts',client_id)

    def ended(self, client_id, session_id, epoch, turn, end_seq, reason_kind):
        """Record the original end even if its later flush fails or is unknown."""
        turn,end_seq,reason_kind = _seq(turn,positive=True),_seq(end_seq),_finite_text(reason_kind,128)
        with self._db(write=True) as db:
            old,_ = self._target(db,client_id,session_id,epoch)
            if (old['native_turn']!=turn or old['native_turn_start_seq'] is None
                    or end_seq<=old['native_turn_start_seq']
                    or old['native_message_seq'] is not None and end_seq<=old['native_message_seq']):
                raise DSHStoreError('conflict')
            if old['native_end_seq'] is not None:
                if (old['native_end_seq'],old['native_reason'])!=(end_seq,reason_kind):
                    raise DSHStoreError('conflict')
                return old
            if old['status']!='accepted':
                raise DSHStoreError('conflict')
            db.execute('UPDATE dsh_prompts SET native_end_seq=?,native_reason=?,updated_at=? WHERE client_id=?',
                       (end_seq,reason_kind,_stamp(),client_id))
            return self._receipt(db,'dsh_prompts',client_id)

    def finish(self, client_id, session_id, epoch, proof):
        """Only a real native flush covering the original turn end is terminal."""
        with self._db(write=True) as db:
            old,binding = self._target(db,client_id,session_id,epoch)
            _,_,durable = self._proof(binding,proof,flush=True)
            status = REASON_STATUSES.get(old['native_reason'])
            if (old['native_end_seq'] is None or status is None or durable<old['native_end_seq']):
                raise DSHStoreError('conflict')
            if old['status'] in TERMINAL:
                if old['status']!=status:
                    raise DSHStoreError('conflict')
                return old
            if old['status']!='accepted':
                raise DSHStoreError('conflict')
            self._anchor(db,binding,proof,flush=True)
            db.execute('UPDATE dsh_prompts SET status=?,durable_seq=?,updated_at=? WHERE client_id=?',
                       (status,durable,_stamp(),client_id))
            return self._receipt(db,'dsh_prompts',client_id)

    @staticmethod
    def _native_response(client_id, response, *, success):
        if (type(response) is not dict or set(response)!={'rpcId','result'}
                or response['rpcId']!=_uuid(client_id) or type(response['result']) is not dict):
            raise DSHStoreError('invalid_arguments')
        result = response['result']
        key = 'value' if success else 'error'
        if set(result)!={'ok',key} or result['ok'] is not success or type(result[key]) is not dict:
            raise DSHStoreError('invalid_arguments')
        try:
            digest = request_sha256(response)
        except NativeSessionStoreError:
            raise DSHStoreError('invalid_arguments') from None
        return result[key],digest

    def command_finished(self, client_id, session_id, epoch, response):
        """Original slash-command result, without inventing a message or turn."""
        value,digest = self._native_response(client_id,response,success=True)
        command = value.get('command')
        if (set(value)!={'accepted','command'} or value['accepted'] is not True
                or type(command) is not dict or set(command) not in ({'kind'},{'kind','text'})
                or command.get('kind')!='success' or 'text' in command and type(command['text']) is not str):
            raise DSHStoreError('invalid_arguments')
        with self._db(write=True) as db:
            old,_ = self._target(db,client_id,session_id,epoch)
            if old['status']=='command_completed' and old['native_command_sha256']==digest:
                return old
            if old['status']!='submitted_unknown' or old['native_message_id'] is not None:
                raise DSHStoreError('conflict')
            db.execute("UPDATE dsh_prompts SET status='command_completed',native_command_sha256=?,updated_at=? WHERE client_id=?",
                       (digest,_stamp(),client_id))
            return self._receipt(db,'dsh_prompts',client_id)

    def rejected(self, client_id, session_id, epoch, response):
        """Original explicit RPC error, never a locally invented timeout result.

        This records the native error; it makes no claim that a failed command
        had no earlier side effects. An admitted message never becomes rejected.
        """
        error,digest = self._native_response(client_id,response,success=False)
        if (not {'code','message'}<=error.keys() or error.keys()-{'code','message','details'}
                or type(error['message']) is not str):
            raise DSHStoreError('invalid_arguments')
        code = _finite_text(error['code'],128)
        with self._db(write=True) as db:
            old,_ = self._target(db,client_id,session_id,epoch)
            if old['status']=='rejected' and (old['native_error_sha256'],old['native_error_code'])==(digest,code):
                return old
            if old['status']!='submitted_unknown':
                raise DSHStoreError('conflict')
            db.execute("UPDATE dsh_prompts SET status='rejected',native_error_code=?,native_error_sha256=?,updated_at=? WHERE client_id=?",
                       (code,digest,_stamp(),client_id))
            return self._receipt(db,'dsh_prompts',client_id)

    def interrupt(self, chat_id, client_id, epoch, active_client_id):
        chat_id,client_id,epoch,active_client_id = _uuid(chat_id),_uuid(client_id),_epoch(epoch),_uuid(active_client_id)
        with self._db(write=True) as db:
            binding = self._binding(db,chat_id)
            if binding is None:
                raise DSHStoreError('not_found')
            old = self._receipt(db,'dsh_controls',client_id)
            if old:
                if (old['chat_id'],old['active_client_id'])!=(chat_id,active_client_id):
                    raise DSHStoreError('conflict')
                return dict(old,created=False)
            active = self._active(db,chat_id)
            if active is None or (active['client_id'],active['epoch'])!=(active_client_id,epoch):
                raise DSHStoreError('conflict')
            digest = request_sha256({'type':'interrupt','session_id':binding['session_id'],'active_client_id':active_client_id})
            now = _stamp()
            db.execute("INSERT INTO dsh_controls VALUES(?,?,?,?,?,?,'submitted_unknown',?,?)",
                       (client_id,chat_id,binding['session_id'],epoch,active_client_id,digest,now,now))
            return dict(self._receipt(db,'dsh_controls',client_id),created=True)

    def finish_interrupt(self, client_id, session_id, epoch):
        with self._db(write=True) as db:
            self._target(db,client_id,session_id,epoch,table='dsh_controls')
            db.execute("UPDATE dsh_controls SET status='acknowledged',updated_at=? WHERE client_id=?",(_stamp(),client_id))
            return self._receipt(db,'dsh_controls',client_id)

    def interrupt_receipt(self, client_id):
        with self._db() as db:
            value = self._receipt(db,'dsh_controls',_uuid(client_id))
            if value:
                self._binding(db,value['chat_id'])
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
