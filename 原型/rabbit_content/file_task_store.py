"""Persistent, host-owned task directories and immutable text artifact snapshots.

Model tools receive only one task work directory. Public artifact IDs are UUIDs;
an artifact is never an arbitrary path. SQLite records publication intent before
disk publication so an interrupted snapshot can be completed by reconciliation.
No model call, home-directory lookup, or original-project write occurs here.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
from urllib.parse import quote
import uuid

from .file_task_files import (CANCEL_NAME, INPUT_MANIFEST, ERROR_MESSAGES, FileTaskError, FileTaskFiles,
                             MAX_DEPTH, MAX_DIRECTORIES, MAX_FILES, MAX_TOTAL_BYTES,
                             MAX_FILE_BYTES, TOOLS, _identity, _parts, _text_bytes)


TERMINAL = frozenset(('completed', 'failed', 'cancelled', 'interrupted'))
STATES = frozenset(('preparing', 'prepared', 'running')) | TERMINAL
SHA = re.compile(r'[0-9a-f]{64}\Z')
MAX_INPUTS = 4
MAX_EVENTS = 128
MAX_SOURCES = 200
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MESSAGES = {
    'invalid_arguments': '文件任务参数无效。',
    'not_found': '这份文件成果或任务不属于当前聊天，或暂时找不到。',
    'task_conflict': '同一任务已冻结其他输入或归属，未重新选择。',
    'input_changed': '输入文件版本已变化，未替换本轮冻结的输入。',
    'artifact_changed': '文件成果未通过完整内容核验，暂时不能下载或引用。',
    'scope_changed': '文件任务目录已变化，没有跟随换目录。',
    'capacity': '本轮最多引用4份文本，任务最多20份、合计4 MiB。',
    'cancelled': '本轮文件任务已停止。',
    'io_failed': '文件任务记录暂未完成，未把未经核验的内容当作成果。',
    'closed': '文件任务成果库已关闭。',
    'source_capacity': '材料超过容量：单文件512 KiB，材料库最多200份、合计64 MiB。',
}


class FileTaskStoreError(ValueError):
    def __init__(self, code):
        self.code = code if code in MESSAGES else 'io_failed'
        super().__init__(MESSAGES[self.code])


def _uuid(value):
    if not isinstance(value, str):
        raise FileTaskStoreError('invalid_arguments')
    try:
        normalized = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise FileTaskStoreError('invalid_arguments') from None
    if normalized != value:
        raise FileTaskStoreError('invalid_arguments')
    return normalized


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _input_selection(ids, expected):
    if not isinstance(ids, (list, tuple)):
        raise FileTaskStoreError('invalid_arguments')
    if len(ids) > MAX_INPUTS:
        raise FileTaskStoreError('capacity')
    ids = [_uuid(value) for value in ids]
    if len(ids) != len(set(ids)):
        raise FileTaskStoreError('invalid_arguments')
    if expected is not None:
        if not isinstance(expected, (list, tuple)) or len(expected) != len(ids):
            raise FileTaskStoreError('invalid_arguments')
        for identifier, row in zip(ids, expected):
            if (not isinstance(row, dict) or set(row) != {'id', 'sha256'}
                    or row['id'] != identifier or not isinstance(row['sha256'], str)
                    or SHA.fullmatch(row['sha256']) is None):
                raise FileTaskStoreError('invalid_arguments')
    return ids


def _source_provenance(value, name, sha256):
    if not isinstance(value, dict):
        raise FileTaskStoreError('invalid_arguments')
    if value == {'kind': 'upload'}:
        return {'kind': 'upload'}
    if set(value) == {'kind', 'from_markdown'} and value['kind'] == 'upload':
        origin = value['from_markdown']
        if (type(origin) is not dict or set(origin) != {
                'kind', 'binding_id', 'head_id', 'relative_path', 'sha256'}
                or origin['kind'] != 'markdown_preview' or type(origin['sha256']) is not str
                or SHA.fullmatch(origin['sha256']) is None or origin['sha256'] != sha256):
            raise FileTaskStoreError('invalid_arguments')
        try:
            parts = _parts(origin['relative_path'])
            if Path(parts[-1]).suffix.casefold() != '.md' or parts[-1] != name:
                raise FileTaskError('invalid_arguments')
        except (FileTaskError, TypeError):
            raise FileTaskStoreError('invalid_arguments') from None
        return {'kind': 'upload', 'from_markdown': {
            'kind': 'markdown_preview', 'binding_id': _uuid(origin['binding_id']),
            'head_id': _uuid(origin['head_id']), 'relative_path': origin['relative_path'], 'sha256': sha256}}
    if set(value) == {'kind', 'from_artifact'} and value['kind'] == 'upload':
        origin = value['from_artifact']
        if (not isinstance(origin, dict) or set(origin) != {'chat_id', 'run_id', 'artifact_id', 'sha256'}
                or not isinstance(origin['sha256'], str) or SHA.fullmatch(origin['sha256']) is None
                or origin['sha256'] != sha256):
            raise FileTaskStoreError('invalid_arguments')
        return {'kind': 'upload', 'from_artifact': {
            key: _uuid(origin[key]) for key in ('chat_id', 'run_id', 'artifact_id')
        } | {'sha256': origin['sha256']}}
    if (set(value) != {'kind', 'relative_path', 'snapshot', 'updated_at'}
            or value['kind'] != 'project_preview' or type(value['snapshot']) is not bool):
        raise FileTaskStoreError('invalid_arguments')
    path, stamp = value['relative_path'], value['updated_at']
    if (not isinstance(path, str) or not 1 <= len(path) <= 8192 or path.startswith('/')
            or '\\' in path or ':' in path or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or any(not part or part.startswith('.') for part in path.split('/'))
            or path.split('/')[-1] != name):
        raise FileTaskStoreError('invalid_arguments')
    if stamp is not None and (not isinstance(stamp, str) or not 1 <= len(stamp) <= 100
                              or any(ord(char) < 32 or ord(char) == 127 for char in stamp)):
        raise FileTaskStoreError('invalid_arguments')
    try:
        path.encode('utf-8')
        if stamp is not None:
            stamp.encode('utf-8')
            datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except (UnicodeError, ValueError):
        raise FileTaskStoreError('invalid_arguments') from None
    return {'kind': 'project_preview', 'relative_path': path, 'snapshot': value['snapshot'], 'updated_at': stamp}


def _stored_source_provenance(row):
    try:
        provenance = _source_provenance(json.loads(row['provenance']), row['name'], row['sha256'])
    except (ValueError, TypeError, KeyError, RecursionError):
        raise FileTaskStoreError('artifact_changed') from None
    if provenance['kind'] != row['source']:
        raise FileTaskStoreError('artifact_changed')
    return provenance


class FileTaskStore:
    def __init__(self, data_root, *, db_path=None):
        self.lock = threading.RLock()
        self._closed = False
        self._connection = None
        self._db_identity = None
        try:
            self.root = Path(os.path.abspath(os.fspath(data_root)))
            # This path is supplied by the host, never by a model or browser.
            self._create_root()
            self.guard = FileTaskFiles(self.root, host_readonly=True)
            self.runs_root = self.root / 'runs'
            self.artifacts_root = self.root / 'artifacts'
            self.sources_root = self.root / 'sources'
            self._ensure_child(self.root, 'runs')
            self._ensure_child(self.root, 'artifacts')
            self._ensure_child(self.root, 'sources')
            self.db_path = self.root / 'file_tasks.sqlite' if db_path is None else Path(os.path.abspath(os.fspath(db_path)))
            if self.db_path.parent != self.root or self.db_path.name.startswith('.'):
                raise FileTaskStoreError('invalid_arguments')
            if self.db_path.exists() or self.db_path.is_symlink():
                info = self.db_path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise FileTaskStoreError('scope_changed')
            with self._db() as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS file_tasks (
                        run_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('preparing','prepared','running','completed','failed','cancelled','interrupted')),
                        inputs TEXT NOT NULL, root_device INTEGER, root_inode INTEGER,
                        warnings TEXT NOT NULL DEFAULT '[]', events TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS file_artifacts (
                        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES file_tasks(run_id),
                        chat_id TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL,
                        sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                        extension TEXT NOT NULL CHECK(extension IN ('.md','.txt')),
                        state TEXT NOT NULL CHECK(state IN ('publishing','verified')),
                        created_at TEXT NOT NULL, UNIQUE(run_id,path)
                    );
                    CREATE TABLE IF NOT EXISTS file_task_revisions (
                        chat_id TEXT PRIMARY KEY, revision INTEGER NOT NULL
                    );
                    CREATE TRIGGER IF NOT EXISTS file_task_identity_immutable
                    BEFORE UPDATE OF run_id,chat_id,inputs,created_at,root_device,root_inode ON file_tasks
                    WHEN NEW.run_id IS NOT OLD.run_id OR NEW.chat_id IS NOT OLD.chat_id
                        OR NEW.inputs IS NOT OLD.inputs OR NEW.created_at IS NOT OLD.created_at
                        OR (OLD.root_inode IS NOT NULL AND (NEW.root_inode IS NOT OLD.root_inode
                            OR NEW.root_device IS NOT OLD.root_device))
                    BEGIN SELECT RAISE(ABORT,'file_task_identity_immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS file_task_terminal_immutable
                    BEFORE UPDATE OF status ON file_tasks
                    WHEN OLD.status IN ('completed','failed','cancelled','interrupted') AND NEW.status IS NOT OLD.status
                    BEGIN SELECT RAISE(ABORT,'file_task_terminal_immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS file_artifact_identity_immutable
                    BEFORE UPDATE OF id,run_id,chat_id,path,name,sha256,size_bytes,extension,created_at ON file_artifacts
                    BEGIN SELECT RAISE(ABORT,'file_artifact_identity_immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS file_artifact_verified_immutable
                    BEFORE UPDATE OF state ON file_artifacts WHEN OLD.state='verified' AND NEW.state IS NOT OLD.state
                    BEGIN SELECT RAISE(ABORT,'file_artifact_verified_immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS file_artifact_no_delete BEFORE DELETE ON file_artifacts
                    BEGIN SELECT RAISE(ABORT,'file_artifact_no_delete'); END;
                    CREATE TRIGGER IF NOT EXISTS file_task_no_delete BEFORE DELETE ON file_tasks
                    BEGIN SELECT RAISE(ABORT,'file_task_no_delete'); END;
                ''')
                # Sources are explicitly selected workspace materials, never
                # synthetic task runs. Existing task/artifact definitions and
                # rows remain untouched; only new tables/guards are appended.
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS file_sources (
                        id TEXT PRIMARY KEY, client_id TEXT NOT NULL UNIQUE,
                        name TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                        extension TEXT NOT NULL CHECK(extension IN ('.md','.txt')),
                        source TEXT NOT NULL CHECK(source IN ('upload','project_preview')),
                        provenance TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('publishing','verified')),
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS file_source_payloads (
                        id TEXT PRIMARY KEY REFERENCES file_sources(id), payload BLOB NOT NULL
                    );
                    CREATE TRIGGER IF NOT EXISTS file_source_payload_immutable BEFORE UPDATE ON file_source_payloads
                    BEGIN SELECT RAISE(ABORT,'file_source_payload_immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS file_source_identity_immutable
                    BEFORE UPDATE OF id,client_id,name,sha256,size_bytes,extension,source,provenance,created_at ON file_sources
                    BEGIN SELECT RAISE(ABORT,'file_source_identity_immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS file_source_verified_immutable
                    BEFORE UPDATE OF state ON file_sources WHEN OLD.state='verified' AND NEW.state IS NOT OLD.state
                    BEGIN SELECT RAISE(ABORT,'file_source_verified_immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS file_source_no_delete BEFORE DELETE ON file_sources
                    BEGIN SELECT RAISE(ABORT,'file_source_no_delete'); END;
                    CREATE TRIGGER IF NOT EXISTS file_source_no_artifact_id BEFORE INSERT ON file_sources
                    WHEN EXISTS(SELECT 1 FROM file_artifacts WHERE id=NEW.id)
                    BEGIN SELECT RAISE(ABORT,'file_input_id_collision'); END;
                    CREATE TRIGGER IF NOT EXISTS file_artifact_no_source_id BEFORE INSERT ON file_artifacts
                    WHEN EXISTS(SELECT 1 FROM file_sources WHERE id=NEW.id)
                    BEGIN SELECT RAISE(ABORT,'file_input_id_collision'); END;
                ''')
        except FileTaskStoreError:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            raise
        except (OSError, ValueError, sqlite3.Error, TypeError):
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            raise FileTaskStoreError('io_failed') from None

    def _create_root(self):
        descriptors = []
        try:
            current = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(current)
            for component in self.root.parts[1:]:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
                current = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                descriptors.append(current)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def close(self):
        with self.lock:
            self._closed = True
            self.guard.close()
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _check(self):
        if self._closed:
            raise FileTaskStoreError('closed')
        try:
            self.guard.check_cancelled()
        except FileTaskError:
            raise FileTaskStoreError('scope_changed') from None

    @contextmanager
    def _db(self, *, write=False):
        # One retained SQLite handle anchors the opened database for this store
        # lifetime. Repeated operations never reconnect through a replaced path.
        with self.lock:
            self._check()
            db = None
            try:
                self._verify_db_file(create=self._connection is None)
                if self._connection is None:
                    db = sqlite3.connect('file:' + quote(str(self.db_path), safe='/') + '?mode=rw',
                                         uri=True, timeout=10, isolation_level=None, check_same_thread=False)
                    self._verify_db_file()
                    reported = db.execute('PRAGMA database_list').fetchone()[2]
                    if reported != str(self.db_path):
                        raise FileTaskStoreError('scope_changed')
                    db.row_factory = sqlite3.Row
                    db.execute('PRAGMA foreign_keys=ON')
                    self._connection = db
                else:
                    db = self._connection
                if write:
                    db.execute('BEGIN IMMEDIATE')
                yield db
                self._check()
                self._verify_db_file()
                if write:
                    db.commit()
            except FileTaskStoreError:
                if db is not None and write and db.in_transaction:
                    db.rollback()
                if db is not None and self._connection is not db:
                    db.close()
                raise
            except sqlite3.Error:
                if db is not None and write and db.in_transaction:
                    db.rollback()
                if db is not None and self._connection is not db:
                    db.close()
                raise FileTaskStoreError('io_failed') from None
            except BaseException:
                if db is not None and write and db.in_transaction:
                    db.rollback()
                if db is not None and self._connection is not db:
                    db.close()
                raise

    def _verify_db_file(self, *, create=False):
        descriptor = None
        try:
            if create:
                try:
                    descriptor = os.open(self.db_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                except FileExistsError:
                    pass
            if descriptor is None:
                descriptor = os.open(self.db_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(descriptor)
            named = self.db_path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or _identity(named) != _identity(info)):
                raise FileTaskStoreError('scope_changed')
            if self._db_identity is None:
                self._db_identity = _identity(info)
            elif self._db_identity != _identity(info):
                raise FileTaskStoreError('scope_changed')
        except OSError:
            raise FileTaskStoreError('scope_changed') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _ensure_child(self, parent, name):
        self._check()
        with FileTaskFiles(parent, host_readonly=True) as files:
            descriptor = files._open_named_root()
            try:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    raise FileTaskStoreError('scope_changed')
                files._check_root(descriptor)
                return parent / name
            finally:
                os.close(descriptor)

    @staticmethod
    def _touch(db, chat_id):
        db.execute('''INSERT INTO file_task_revisions(chat_id,revision) VALUES(?,1)
            ON CONFLICT(chat_id) DO UPDATE SET revision=revision+1''', (chat_id,))

    def revision(self, chat_id):
        chat_id = _uuid(chat_id)
        with self._db() as db:
            row = db.execute('SELECT revision FROM file_task_revisions WHERE chat_id=?', (chat_id,)).fetchone()
        return str(row[0] if row else 0)

    @staticmethod
    def _owned_task(db, run_id, chat_id):
        row = db.execute('SELECT * FROM file_tasks WHERE run_id=? AND chat_id=?', (run_id, chat_id)).fetchone()
        if row is None:
            raise FileTaskStoreError('not_found')
        return dict(row)

    @staticmethod
    def _owned_artifact(db, identifier, chat_id):
        row = db.execute('SELECT * FROM file_artifacts WHERE id=? AND chat_id=?', (identifier, chat_id)).fetchone()
        if row is None:
            raise FileTaskStoreError('not_found')
        return dict(row)

    @staticmethod
    def _fresh_identifier(db):
        for _attempt in range(10):
            identifier = str(uuid.uuid4())
            if (db.execute('SELECT 1 FROM file_artifacts WHERE id=?', (identifier,)).fetchone() is None
                    and db.execute('SELECT 1 FROM file_sources WHERE id=?', (identifier,)).fetchone() is None):
                return identifier
        raise FileTaskStoreError('io_failed')

    @staticmethod
    def _source_in(db, identifier):
        row = db.execute('SELECT * FROM file_sources WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise FileTaskStoreError('not_found')
        return dict(row)

    @staticmethod
    def _input_in(db, identifier, chat_id):
        # Generated IDs retain their original chat boundary even if a damaged
        # legacy database somehow contains the same UUID in the source table.
        artifact = db.execute('SELECT * FROM file_artifacts WHERE id=?', (identifier,)).fetchone()
        if artifact is not None:
            if artifact['chat_id'] != chat_id:
                raise FileTaskStoreError('not_found')
            return 'artifact', dict(artifact)
        return 'source', FileTaskStore._source_in(db, identifier)

    def _task_path(self, row):
        path = self.runs_root / row['run_id'] / 'work'
        try:
            with FileTaskFiles(path, host_readonly=True) as files:
                if (row['root_inode'] is None or files._root_identity != (row['root_device'], row['root_inode'])):
                    raise FileTaskStoreError('scope_changed')
            return path
        except FileTaskError:
            raise FileTaskStoreError('scope_changed') from None

    def task_root(self, run_id, *, chat_id):
        run_id, chat_id = _uuid(run_id), _uuid(chat_id)
        with self._db() as db:
            row = self._owned_task(db, run_id, chat_id)
        return self._task_path(row)

    def _artifact_path(self, row):
        return self.artifacts_root / row['id'], 'content' + row['extension']

    def _artifact_read(self, row):
        directory, name = self._artifact_path(row)
        return self._snapshot_read(row, directory, name)

    def _source_path(self, row):
        return self.sources_root / row['id'], 'content' + row['extension']

    def _source_read(self, row):
        _stored_source_provenance(row)
        directory, name = self._source_path(row)
        return self._snapshot_read(row, directory, name)

    @staticmethod
    def _snapshot_read(row, directory, name):
        try:
            with FileTaskFiles(directory, host_readonly=True) as files:
                result = files.call('rabbit_file_read', {'path': name})
            if result['sha256'] != row['sha256'] or result['size_bytes'] != row['size_bytes']:
                raise FileTaskStoreError('artifact_changed')
            return result
        except FileTaskError:
            raise FileTaskStoreError('artifact_changed') from None

    def _public_artifact(self, row, *, verify=True):
        available = row['state'] == 'verified'
        if available and verify:
            try:
                self._artifact_read(row)
            except FileTaskStoreError:
                available = False
        return {key: row[key] for key in ('id', 'run_id', 'chat_id', 'name', 'path', 'sha256', 'size_bytes', 'created_at')} | {
            'available': available, 'content_verified': available}

    def get_artifact(self, identifier, *, chat_id):
        identifier, chat_id = _uuid(identifier), _uuid(chat_id)
        with self._db() as db:
            kind, row = self._input_in(db, identifier, chat_id)
        return self._public_source(row) if kind == 'source' else self._public_artifact(row)

    def artifact_content(self, identifier, *, chat_id):
        identifier, chat_id = _uuid(identifier), _uuid(chat_id)
        with self._db() as db:
            kind, row = self._input_in(db, identifier, chat_id)
        if row['state'] != 'verified':
            raise FileTaskStoreError('artifact_changed')
        content = self._source_read(row) if kind == 'source' else self._artifact_read(row)
        public = self._public_source(row, verify=False) if kind == 'source' else self._public_artifact(row, verify=False)
        return {'artifact': public, 'text': content['text'],
                'size_bytes': content['size_bytes'], 'sha256': content['sha256']}

    def input_snapshot(self, identifier, *, chat_id=None, expected_sha256=None, content=True):
        """Host-only bounded source/artifact read; IDs never designate paths.

        Metadata can be inspected before allocating the immutable body. A
        generated artifact keeps its chat boundary, while an unbound/new draft
        can only read an explicitly imported global source.
        """
        identifier = _uuid(identifier)
        if chat_id is not None:
            chat_id = _uuid(chat_id)
        if expected_sha256 is not None and (not isinstance(expected_sha256, str)
                                           or SHA.fullmatch(expected_sha256) is None):
            raise FileTaskStoreError('invalid_arguments')
        with self._db() as db:
            if chat_id is None:
                kind, row = 'source', self._source_in(db, identifier)
            else:
                kind, row = self._input_in(db, identifier, chat_id)
        if expected_sha256 is not None and row['sha256'] != expected_sha256:
            raise FileTaskStoreError('input_changed')
        public = (self._public_source(row, verify=False) if kind == 'source'
                  else dict(self._public_artifact(row, verify=False), source='generated'))
        if not content:
            return {'artifact': public}
        if row['state'] != 'verified':
            raise FileTaskStoreError('artifact_changed')
        body = self._source_read(row) if kind == 'source' else self._artifact_read(row)
        return {'artifact': public, 'text': body['text'], 'size_bytes': body['size_bytes'],
                'sha256': body['sha256']}

    def _public_source(self, row, *, verify=True):
        provenance = _stored_source_provenance(row)
        available = row['state'] == 'verified'
        if available and verify:
            try:
                self._source_read(row)
            except FileTaskStoreError:
                available = False
        return {'id': row['id'], 'name': row['name'], 'path': row['name'], 'sha256': row['sha256'],
                'size_bytes': row['size_bytes'], 'source': row['source'], 'provenance': provenance,
                'created_at': row['created_at'], 'available': available, 'content_verified': available}

    def get_source(self, identifier):
        identifier = _uuid(identifier)
        with self._db() as db:
            row = self._source_in(db, identifier)
        return self._public_source(row)

    def source_content(self, identifier):
        identifier = _uuid(identifier)
        with self._db() as db:
            row = self._source_in(db, identifier)
        if row['state'] != 'verified':
            raise FileTaskStoreError('artifact_changed')
        content = self._source_read(row)
        return {'artifact': self._public_source(row, verify=False), 'text': content['text'],
                'size_bytes': content['size_bytes'], 'sha256': content['sha256']}

    def find_source_receipt(self, client_id):
        client_id = _uuid(client_id)
        with self._db() as db:
            row = db.execute('SELECT * FROM file_sources WHERE client_id=?', (client_id,)).fetchone()
        if row is None:
            return {'received': False}
        row = dict(row)
        return {'received': True, 'material': self._public_source(row), 'state': row['state']}

    def list_sources(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= MAX_SOURCES:
            raise FileTaskStoreError('invalid_arguments')
        with self._db() as db:
            rows = db.execute('SELECT * FROM file_sources ORDER BY created_at DESC,id LIMIT ?', (limit,)).fetchall()
        return [self._public_source(dict(row)) for row in rows]

    def sources_revision(self):
        with self._db() as db:
            rows = [list(row) for row in db.execute('SELECT id,state FROM file_sources ORDER BY id').fetchall()]
        return hashlib.sha256(_json(rows).encode('utf-8')).hexdigest()

    def add_source(self, name, text, provenance, client_id):
        client_id = _uuid(client_id)
        with self.lock:
            # Receipt lookup is deliberately before new payload validation.
            # The first accepted identity remains authoritative on any retry.
            receipt = self.find_source_receipt(client_id)
            if receipt['received']:
                return receipt['material']
            try:
                parts = _parts(name)
                if len(parts) != 1:
                    raise FileTaskError('invalid_path')
                raw = _text_bytes(text)
            except FileTaskError as error:
                raise FileTaskStoreError('source_capacity' if error.code == 'capacity' else 'invalid_arguments') from None
            digest = hashlib.sha256(raw).hexdigest()
            origin = _source_provenance(provenance, name, digest)
            with self._db(write=True) as db:
                winner = db.execute('SELECT * FROM file_sources WHERE client_id=?', (client_id,)).fetchone()
                if winner is not None:
                    row = dict(winner)
                else:
                    counts = db.execute('SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM file_sources').fetchone()
                    if counts[0] >= MAX_SOURCES or counts[1] + len(raw) > MAX_SOURCE_BYTES:
                        raise FileTaskStoreError('source_capacity')
                    row = {'id': self._fresh_identifier(db), 'client_id': client_id, 'name': name,
                           'sha256': digest, 'size_bytes': len(raw),
                           'extension': Path(name).suffix.casefold(), 'source': origin['kind'],
                           'provenance': _json(origin), 'state': 'publishing', 'created_at': _stamp()}
                    db.execute('''INSERT INTO file_sources(id,client_id,name,sha256,size_bytes,extension,source,provenance,state,created_at)
                        VALUES(:id,:client_id,:name,:sha256,:size_bytes,:extension,:source,:provenance,:state,:created_at)''', row)
                    db.execute('INSERT INTO file_source_payloads(id,payload) VALUES(?,?)', (row['id'], sqlite3.Binary(raw)))
            # Publish only the persisted first payload, never this caller's
            # possibly different text when another instance won acceptance.
            self._publish_source(row)
            return self.get_source(row['id'])

    def import_artifact_source(self, client_id, *, source_chat_id, artifact_id, expected_sha256):
        """Explicitly copy one verified, chat-owned artifact into global inputs.

        The original artifact stays owned by its original chat. The new input
        uses the existing immutable source ledger and first-accepted receipt;
        neither paths nor browser-supplied text participate in this copy.
        """
        client_id = _uuid(client_id)
        with self.lock:
            receipt = self.find_source_receipt(client_id)
            if receipt['received']:
                return receipt['material']
            source_chat_id, artifact_id = _uuid(source_chat_id), _uuid(artifact_id)
            if not isinstance(expected_sha256, str) or SHA.fullmatch(expected_sha256) is None:
                raise FileTaskStoreError('invalid_arguments')
            with self._db() as db:
                # This is deliberately artifact-only; the ordinary input
                # resolver also accepts already-global upload/project sources.
                row = self._owned_artifact(db, artifact_id, source_chat_id)
                self._owned_task(db, row['run_id'], source_chat_id)
            if row['sha256'] != expected_sha256:
                raise FileTaskStoreError('input_changed')
            if row['state'] != 'verified':
                raise FileTaskStoreError('artifact_changed')
            content = self._artifact_read(row)
            provenance = {'kind': 'upload', 'from_artifact': {
                'chat_id': source_chat_id, 'run_id': row['run_id'],
                'artifact_id': artifact_id, 'sha256': expected_sha256}}
            return self.add_source(row['name'], content['text'], provenance, client_id)

    def list_artifacts(self, chat_id):
        chat_id = _uuid(chat_id)
        with self._db() as db:
            rows = db.execute('SELECT * FROM file_artifacts WHERE chat_id=? AND state=? ORDER BY created_at,id',
                              (chat_id, 'verified')).fetchall()
        return [self._public_artifact(dict(row)) for row in rows]

    def handoff_artifacts(self, chat_id):
        """One metadata snapshot; byte verification remains with input_snapshot.

        A local savepoint also preserves any transaction already owned by the
        caller. Neither paths nor claims about current file contents are read.
        """
        chat_id = _uuid(chat_id)
        with self._db() as db:
            db.execute('SAVEPOINT handoff_artifacts')
            try:
                revision = db.execute('SELECT revision FROM file_task_revisions WHERE chat_id=?',
                                      (chat_id,)).fetchone()
                rows = db.execute('''SELECT artifact.id,artifact.run_id,artifact.chat_id,
                    artifact.name,artifact.sha256,artifact.size_bytes
                    FROM file_artifacts AS artifact JOIN file_tasks AS task
                      ON task.run_id=artifact.run_id AND task.chat_id=artifact.chat_id
                    WHERE artifact.chat_id=? AND artifact.state='verified' AND task.status='completed'
                    ORDER BY artifact.created_at,artifact.id''', (chat_id,)).fetchall()
                result = {'revision': str(revision[0] if revision else 0),
                          'artifacts': [dict(row) for row in rows]}
            finally:
                db.execute('RELEASE handoff_artifacts')
        return result

    def _receipt(self, row):
        with self._db() as db:
            artifacts = [dict(item) for item in db.execute('SELECT * FROM file_artifacts WHERE run_id=? ORDER BY path',
                                                          (row['run_id'],)).fetchall()]
        warnings = set(json.loads(row['warnings']))
        public = []
        for item in artifacts:
            if item['state'] != 'verified':
                warnings.add('artifact_incomplete')
                continue
            value = self._public_artifact(item)
            public.append(value)
            if not value['available']:
                warnings.add('artifact_changed')
        return {'run_id': row['run_id'], 'chat_id': row['chat_id'], 'status': row['status'],
                'inputs': json.loads(row['inputs']), 'artifacts': public,
                'file_state': 'warnings' if warnings else ('verified' if public else 'no_files'),
                'warnings': sorted(warnings), 'events': json.loads(row['events']),
                'created_at': row['created_at'], 'updated_at': row['updated_at']}

    def get_task(self, run_id, *, chat_id):
        run_id, chat_id = _uuid(run_id), _uuid(chat_id)
        with self._db() as db:
            row = self._owned_task(db, run_id, chat_id)
        return self._receipt(row)

    def all_tasks(self, chat_id=None):
        if chat_id is not None:
            chat_id = _uuid(chat_id)
        with self._db() as db:
            rows = db.execute('SELECT * FROM file_tasks' + (' WHERE chat_id=?' if chat_id else '') + ' ORDER BY created_at,run_id',
                              (chat_id,) if chat_id else ()).fetchall()
        return [self._receipt(dict(row)) for row in rows]

    def list_tasks(self, chat_id):
        return self.all_tasks(chat_id)

    def prepare(self, run_id, chat_id, input_ids=None, expected_inputs=None):
        run_id, chat_id = _uuid(run_id), _uuid(chat_id)
        provided = input_ids is not None
        identifiers = _input_selection([] if input_ids is None else input_ids, expected_inputs)
        with self.lock:
            with self._db() as db:
                existing = db.execute('SELECT * FROM file_tasks WHERE run_id=?', (run_id,)).fetchone()
            if existing is not None:
                row = dict(existing)
                frozen = json.loads(row['inputs'])
                if row['chat_id'] != chat_id or (provided and identifiers != [item['id'] for item in frozen]):
                    raise FileTaskStoreError('task_conflict')
                if expected_inputs is not None and list(expected_inputs) != [{'id': item['id'], 'sha256': item['sha256']} for item in frozen]:
                    raise FileTaskStoreError('input_changed')
                if row['status'] != 'preparing':
                    return self._receipt(row)
            else:
                frozen = []
                for number, identifier in enumerate(identifiers, 1):
                    content = self.artifact_content(identifier, chat_id=chat_id)
                    if expected_inputs is not None and expected_inputs[number - 1]['sha256'] != content['sha256']:
                        raise FileTaskStoreError('input_changed')
                    source = content['artifact']
                    frozen.append({'id': identifier, 'sha256': content['sha256'], 'size_bytes': content['size_bytes'],
                                   'name': source['name'], 'path': '输入-%02d-%s%s' % (number, identifier, Path(source['name']).suffix.casefold())})
                if sum(item['size_bytes'] for item in frozen) > MAX_TOTAL_BYTES or len(frozen) > MAX_FILES:
                    raise FileTaskStoreError('capacity')
                stamp = _stamp()
                with self._db(write=True) as db:
                    winner = db.execute('SELECT * FROM file_tasks WHERE run_id=?', (run_id,)).fetchone()
                    if winner is not None:
                        winner = dict(winner)
                        if winner['chat_id'] != chat_id or json.loads(winner['inputs']) != frozen:
                            raise FileTaskStoreError('task_conflict')
                        row = winner
                    else:
                        db.execute('''INSERT INTO file_tasks(run_id,chat_id,status,inputs,created_at,updated_at)
                                      VALUES(?,?,?,?,?,?)''', (run_id, chat_id, 'preparing', _json(frozen), stamp, stamp))
                        self._touch(db, chat_id)
                        row = self._owned_task(db, run_id, chat_id)
            preparation_owned = False
            try:
                run_dir = self._ensure_child(self.runs_root, run_id)
                # A distinct parent-directory lock serializes the entire
                # preparation across store instances. Child tool locks remain
                # available for each copy, so this does not nest the same lock.
                with FileTaskFiles(run_dir, host_readonly=True) as gate:
                    with gate._scope(write=True) as gate_fd:
                        preparation_owned = True
                        with self._db() as db:
                            current = self._owned_task(db, run_id, chat_id)
                        if current['status'] != 'preparing':
                            if current['root_inode'] is None:
                                raise FileTaskStoreError('cancelled')
                            return self._receipt(current)
                        result = self._complete_preparation(run_dir, current, frozen)
                        gate._check_root(gate_fd)
                        return result
            except (OSError, FileTaskError, FileTaskStoreError) as error:
                code = getattr(error, 'code', 'io_failed')
                if preparation_owned:
                    self._fail_preparing(run_id, chat_id, 'input_changed' if code in ('input_changed', 'file_changed') else 'preparation_failed')
                raise FileTaskStoreError(code if code in MESSAGES else 'io_failed') from None

    def _complete_preparation(self, run_dir, row, frozen):
        run_id, chat_id = row['run_id'], row['chat_id']
        directory = self._ensure_child(run_dir, 'work')
        with FileTaskFiles(directory) as files:
            identity = files._root_identity
            with self._db(write=True) as db:
                current = self._owned_task(db, run_id, chat_id)
                if current['root_inode'] is None:
                    db.execute('UPDATE file_tasks SET root_device=?,root_inode=?,updated_at=? WHERE run_id=?',
                               (identity[0], identity[1], _stamp(), run_id))
                    self._touch(db, chat_id)
                elif (current['root_device'], current['root_inode']) != identity:
                    raise FileTaskStoreError('scope_changed')
            if current['status'] in TERMINAL:
                current['root_device'], current['root_inode'] = identity
                self._mark_stopped(current)
                raise FileTaskStoreError('cancelled')
            for item in frozen:
                content = self.artifact_content(item['id'], chat_id=chat_id)
                if content['sha256'] != item['sha256'] or content['size_bytes'] != item['size_bytes']:
                    raise FileTaskStoreError('input_changed')
                try:
                    files.call('rabbit_file_save', {'path': item['path'], 'text': content['text']})
                except FileTaskError as error:
                    if error.code != 'file_exists':
                        raise
                copied = files.call('rabbit_file_read', {'path': item['path']})
                if copied['sha256'] != item['sha256'] or copied['size_bytes'] != item['size_bytes']:
                    raise FileTaskStoreError('input_changed')
                self._readonly_file(directory, item['path'], expected=item)
            files.check_cancelled()
            self._write_manifest(directory, frozen)
            with self._db() as db:
                current = self._owned_task(db, run_id, chat_id)
            self._verify_inputs(current)
        with self._db(write=True) as db:
            current = self._owned_task(db, run_id, chat_id)
            if current['status'] == 'preparing':
                db.execute('UPDATE file_tasks SET status=?,updated_at=? WHERE run_id=?', ('prepared', _stamp(), run_id))
                self._touch(db, chat_id)
        return self.get_task(run_id, chat_id=chat_id)

    def _fail_preparing(self, run_id, chat_id, code):
        with self._db(write=True) as db:
            row = self._owned_task(db, run_id, chat_id)
            if row['status'] == 'preparing':
                warnings = sorted(set(json.loads(row['warnings'])) | {code})
                db.execute('UPDATE file_tasks SET status=?,warnings=?,updated_at=? WHERE run_id=?',
                           ('failed', _json(warnings), _stamp(), run_id))
                self._touch(db, chat_id)

    def _readonly_file(self, directory, name, *, expected=None):
        with FileTaskFiles(directory, host_readonly=True) as files:
            with files._scope(write=True) as root_fd:
                descriptor = None
                try:
                    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise FileTaskStoreError('scope_changed')
                    text, raw, verified = files._read(root_fd, name)
                    del text
                    if (_identity(info) != _identity(verified) or (expected is not None
                            and (len(raw) != expected['size_bytes'] or hashlib.sha256(raw).hexdigest() != expected['sha256']))):
                        raise FileTaskStoreError('input_changed' if expected else 'artifact_changed')
                    if stat.S_IMODE(info.st_mode) != 0o400:
                        os.fchmod(descriptor, 0o400)
                    text, raw, verified = files._read(root_fd, name)
                    del text
                    if (_identity(info) != _identity(verified) or (expected is not None
                            and (len(raw) != expected['size_bytes'] or hashlib.sha256(raw).hexdigest() != expected['sha256']))):
                        raise FileTaskStoreError('input_changed' if expected else 'artifact_changed')
                    files._check_root(root_fd)
                finally:
                    if descriptor is not None:
                        os.close(descriptor)

    @staticmethod
    def _manifest_bytes(frozen):
        return _json({'version': 1, 'inputs': [{key: item[key] for key in ('path', 'sha256', 'size_bytes')}
                                              for item in frozen]}).encode('utf-8')

    def _write_manifest(self, directory, frozen):
        raw = self._manifest_bytes(frozen)
        with FileTaskFiles(directory, host_readonly=True) as files:
            with files._scope(write=True) as root_fd:
                temporary = '.rabbit-manifest-' + secrets.token_hex(16)
                descriptor = None
                try:
                    descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o400, dir_fd=root_fd)
                    files._write(descriptor, raw)
                    try:
                        os.link(temporary, INPUT_MANIFEST, src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
                    except FileExistsError:
                        pass
                    os.unlink(temporary, dir_fd=root_fd)
                    temporary = None
                    os.fsync(root_fd)
                    signature, _expected = files._manifest_value(root_fd)
                    if signature is None or signature[1] != hashlib.sha256(raw).hexdigest():
                        raise FileTaskStoreError('input_changed')
                    files._check_root(root_fd)
                finally:
                    try:
                        if temporary is not None:
                            os.unlink(temporary, dir_fd=root_fd)
                    finally:
                        if descriptor is not None:
                            os.close(descriptor)

    def _verify_inputs(self, row):
        frozen = json.loads(row['inputs'])
        with FileTaskFiles(self._task_path(row), host_readonly=True) as files:
            with files._scope() as root_fd:
                signature, _expected = files._manifest_value(root_fd)
                if signature is None or signature[1] != hashlib.sha256(self._manifest_bytes(frozen)).hexdigest():
                    raise FileTaskStoreError('input_changed')
                for item in frozen:
                    text, raw, _info = files._read(root_fd, item['path'])
                    del text
                    if len(raw) != item['size_bytes'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
                        raise FileTaskStoreError('input_changed')

    def _warning(self, run_id, chat_id, code):
        with self._db(write=True) as db:
            row = self._owned_task(db, run_id, chat_id)
            warnings = sorted(set(json.loads(row['warnings'])) | {code})
            if warnings != json.loads(row['warnings']):
                db.execute('UPDATE file_tasks SET warnings=?,updated_at=? WHERE run_id=?', (_json(warnings), _stamp(), run_id))
                self._touch(db, chat_id)

    def _set_status(self, run_id, chat_id, status):
        with self._db(write=True) as db:
            row = self._owned_task(db, run_id, chat_id)
            if row['status'] not in TERMINAL and row['status'] != status:
                db.execute('UPDATE file_tasks SET status=?,updated_at=? WHERE run_id=?', (status, _stamp(), run_id))
                self._touch(db, chat_id)

    def start(self, run_id, *, chat_id):
        run_id, chat_id = _uuid(run_id), _uuid(chat_id)
        with self._db() as db:
            row = self._owned_task(db, run_id, chat_id)
        if row['status'] == 'preparing':
            raise FileTaskStoreError('task_conflict')
        if row['status'] not in TERMINAL:
            try:
                self._verify_inputs(row)
                with FileTaskFiles(self._task_path(row)) as files:
                    files.check_cancelled()
            except FileTaskError as error:
                raise FileTaskStoreError('cancelled' if error.code == 'cancelled' else 'input_changed') from None
            self._set_status(run_id, chat_id, 'running')
        return self.get_task(run_id, chat_id=chat_id)

    def _mark_stopped(self, row):
        directory = self._task_path(row)
        with FileTaskFiles(directory, host_readonly=True) as files:
            descriptor = files._open_named_root()
            try:
                try:
                    stopped = os.open(CANCEL_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                      0o600, dir_fd=descriptor)
                except FileExistsError:
                    pass  # Any existing entry is already a stop marker; never follow it.
                else:
                    try:
                        os.fsync(stopped)
                    finally:
                        os.close(stopped)
                os.fsync(descriptor)
                files._check_root(descriptor)
            finally:
                os.close(descriptor)

    def _scan(self, row):
        """One shared tool lock; independently verify good files despite bad peers."""
        directory = self._task_path(row)
        found, warnings, counts = {}, set(), [0, 0, 0]
        with FileTaskFiles(directory, host_readonly=True) as files:
            with files._scope() as root_fd:
                try:
                    signature, _expected = files._manifest_value(root_fd)
                    if signature is None or signature[1] != hashlib.sha256(self._manifest_bytes(json.loads(row['inputs']))).hexdigest():
                        warnings.add('input_changed')
                except FileTaskError:
                    warnings.add('input_changed')
                def walk(parent, prefix, lineage):
                    for name in sorted(os.listdir(parent)):
                        if name.startswith('.'):
                            continue
                        path = '/'.join(prefix + [name])
                        try:
                            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                            if stat.S_ISDIR(info.st_mode):
                                counts[2] += 1
                                if counts[2] > MAX_DIRECTORIES or len(prefix) + 1 >= MAX_DEPTH:
                                    warnings.add('capacity')
                                    continue
                                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                                try:
                                    identity = _identity(os.fstat(child))
                                    if identity != _identity(info):
                                        raise FileTaskError('scope_changed')
                                    child_lineage = lineage + [(parent, name, identity)]
                                    walk(child, prefix + [name], child_lineage)
                                    files._check_lineage(root_fd, child_lineage)
                                finally:
                                    os.close(child)
                                continue
                            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                                warnings.add('unsafe_file')
                                continue
                            if Path(name).suffix.casefold() not in ('.md', '.txt'):
                                continue
                            _parts(path)
                            text, raw, _info = files._read(parent, name)
                            counts[0] += 1
                            counts[1] += len(raw)
                            if counts[0] > MAX_FILES or counts[1] > MAX_TOTAL_BYTES:
                                warnings.add('capacity')
                                continue
                            found[path] = {'path': path, 'name': name, 'text': text, 'size_bytes': len(raw),
                                           'sha256': hashlib.sha256(raw).hexdigest()}
                        except (FileTaskError, OSError) as error:
                            warnings.add(getattr(error, 'code', 'file_rejected'))
                    files._check_lineage(root_fd, lineage)
                walk(root_fd, [], [])
        return found, warnings

    def _publish(self, artifact, text=None):
        directory, name = self._artifact_path(artifact)
        try:
            directory = self._ensure_child(self.artifacts_root, artifact['id'])
            if artifact['state'] == 'publishing':
                self._recover_artifact_staging(artifact)
            try:
                existing = self._artifact_read(artifact)
            except FileTaskStoreError:
                existing = None
            if existing is None:
                if text is None:
                    return False
                with FileTaskFiles(directory) as files:
                    try:
                        files.call('rabbit_file_save', {'path': name, 'text': text})
                    except FileTaskError as error:
                        if error.code != 'file_exists':
                            raise
                self._artifact_read(artifact)
            self._readonly_file(directory, name, expected=artifact)
            with self._db(write=True) as db:
                current = self._owned_artifact(db, artifact['id'], artifact['chat_id'])
                if current['state'] != 'verified':
                    db.execute('UPDATE file_artifacts SET state=? WHERE id=?', ('verified', artifact['id']))
                    self._touch(db, artifact['chat_id'])
            return True
        except (FileTaskError, FileTaskStoreError, OSError):
            return False

    def _source_payload(self, row):
        with self._db() as db:
            value = db.execute('SELECT payload FROM file_source_payloads WHERE id=?', (row['id'],)).fetchone()
        if value is None:
            return None
        raw = value['payload']
        if (not isinstance(raw, bytes) or len(raw) != row['size_bytes'] or len(raw) > MAX_FILE_BYTES
                or hashlib.sha256(raw).hexdigest() != row['sha256']):
            raise FileTaskStoreError('artifact_changed')
        try:
            text = raw.decode('utf-8')
            if _text_bytes(text) != raw:
                raise FileTaskStoreError('artifact_changed')
        except (UnicodeError, FileTaskError):
            raise FileTaskStoreError('artifact_changed') from None
        return text

    def _publish_source(self, row):
        directory, name = self._source_path(row)
        try:
            text = self._source_payload(row)
            if text is None:
                with FileTaskFiles(directory, host_readonly=True):
                    pass
            else:
                directory = self._ensure_child(self.sources_root, row['id'])
            if row['state'] == 'publishing':
                self._recover_snapshot_staging(row, directory, name)
            try:
                self._source_read(row)
            except FileTaskStoreError:
                if text is None:
                    return False
                with FileTaskFiles(directory) as files:
                    try:
                        files.call('rabbit_file_save', {'path': name, 'text': text})
                    except FileTaskError as error:
                        if error.code != 'file_exists':
                            raise
                self._source_read(row)
            self._readonly_file(directory, name, expected=row)
            with self._db(write=True) as db:
                current = self._source_in(db, row['id'])
                if current['state'] != 'verified':
                    db.execute('UPDATE file_sources SET state=? WHERE id=?', ('verified', row['id']))
                # Only a verified immutable snapshot permits discarding the
                # temporary durable payload. The source identity is retained.
                db.execute('DELETE FROM file_source_payloads WHERE id=?', (row['id'],))
            return True
        except (FileTaskError, FileTaskStoreError, OSError):
            return False

    def recover_sources(self):
        with self._db() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM file_sources WHERE state=? ORDER BY id', ('publishing',)).fetchall()]
        result = {'verified': 0, 'pending': 0}
        for row in rows:
            if self._publish_source(row):
                result['verified'] += 1
            else:
                result['pending'] += 1
        return result

    def _recover_artifact_staging(self, artifact):
        directory, name = self._artifact_path(artifact)
        return self._recover_snapshot_staging(artifact, directory, name)

    def _recover_snapshot_staging(self, artifact, directory, name):
        """Recover a killed link/unlink publication only inside a pending vault.

        The ledger already freezes the expected bytes/hash. Every extra hard
        link must be one of our private staging aliases in this same directory;
        any unaccounted link or changed body is retained and refused.
        """
        with FileTaskFiles(directory, host_readonly=True) as files:
            with files._scope(write=True) as root_fd:
                descriptor = None
                try:
                    try:
                        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
                    except FileNotFoundError:
                        # A kill after fsync but before publication can leave a
                        # complete single-link staging snapshot. Its frozen
                        # ledger hash permits publishing it without a browser
                        # repost or re-reading an original project path.
                        for candidate in sorted(os.listdir(root_fd)):
                            if re.fullmatch(r'\.rabbit-file-[0-9a-f]{32}', candidate) is None:
                                continue
                            try:
                                staged_fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
                                try:
                                    info = os.fstat(staged_fd)
                                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                            or info.st_size != artifact['size_bytes'] or info.st_size > MAX_FILE_BYTES):
                                        continue
                                    raw = bytearray()
                                    while len(raw) <= MAX_FILE_BYTES:
                                        part = os.read(staged_fd, min(65536, MAX_FILE_BYTES + 1 - len(raw)))
                                        if not part:
                                            break
                                        raw.extend(part)
                                    after = os.fstat(staged_fd)
                                    named = os.stat(candidate, dir_fd=root_fd, follow_symlinks=False)
                                    if (len(raw) != artifact['size_bytes'] or hashlib.sha256(raw).hexdigest() != artifact['sha256']
                                            or _identity(named) != _identity(info) or _identity(after) != _identity(info)
                                            or after.st_size != info.st_size or after.st_mtime_ns != info.st_mtime_ns
                                            or after.st_ctime_ns != info.st_ctime_ns or after.st_nlink != 1):
                                        continue
                                    try:
                                        text = raw.decode('utf-8')
                                    except UnicodeDecodeError:
                                        continue
                                    if '\0' in text:
                                        continue
                                    os.link(candidate, name, src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
                                    os.unlink(candidate, dir_fd=root_fd)
                                    os.fsync(root_fd)
                                    files._check_root(root_fd)
                                    break
                                finally:
                                    os.close(staged_fd)
                            except (FileNotFoundError, FileExistsError):
                                continue
                        return
                    before = os.fstat(descriptor)
                    if (not stat.S_ISREG(before.st_mode) or before.st_nlink <= 1
                            or before.st_size != artifact['size_bytes'] or before.st_size > MAX_FILE_BYTES):
                        return
                    raw = bytearray()
                    while len(raw) <= MAX_FILE_BYTES:
                        part = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - len(raw)))
                        if not part:
                            break
                        raw.extend(part)
                    after = os.fstat(descriptor)
                    named = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                    if (len(raw) != artifact['size_bytes'] or hashlib.sha256(raw).hexdigest() != artifact['sha256']
                            or _identity(named) != _identity(before) or _identity(after) != _identity(before)
                            or before.st_mtime_ns != after.st_mtime_ns or before.st_ctime_ns != after.st_ctime_ns
                            or before.st_nlink != after.st_nlink):
                        return
                    try:
                        text = raw.decode('utf-8')
                    except UnicodeDecodeError:
                        return
                    if '\0' in text:
                        return
                    aliases = []
                    for candidate in os.listdir(root_fd):
                        if re.fullmatch(r'\.rabbit-file-[0-9a-f]{32}', candidate) is None:
                            continue
                        info = os.stat(candidate, dir_fd=root_fd, follow_symlinks=False)
                        if stat.S_ISREG(info.st_mode) and _identity(info) == _identity(before):
                            aliases.append(candidate)
                    if len(aliases) != before.st_nlink - 1:
                        return
                    for alias in aliases:
                        info = os.stat(alias, dir_fd=root_fd, follow_symlinks=False)
                        if _identity(info) != _identity(before):
                            raise FileTaskStoreError('artifact_changed')
                        os.unlink(alias, dir_fd=root_fd)
                    os.fsync(root_fd)
                    files._check_root(root_fd)
                finally:
                    if descriptor is not None:
                        os.close(descriptor)

    def reconcile(self, run_id, *, chat_id):
        run_id, chat_id = _uuid(run_id), _uuid(chat_id)
        with self.lock:
            with self._db() as db:
                row = self._owned_task(db, run_id, chat_id)
                prior = [dict(item) for item in db.execute('SELECT * FROM file_artifacts WHERE run_id=?', (run_id,)).fetchall()]
            try:
                found, warnings = self._scan(row)
            except (FileTaskError, FileTaskStoreError, OSError) as error:
                found, warnings = {}, {getattr(error, 'code', 'file_rejected')}
            frozen_paths = set()
            for item in json.loads(row['inputs']):
                frozen_paths.add(item['path'])
                current = found.get(item['path'])
                if current is None or current['sha256'] != item['sha256'] or current['size_bytes'] != item['size_bytes']:
                    warnings.add('input_changed')
            existing_paths = set()
            for artifact in prior:
                existing_paths.add(artifact['path'])
                current = found.get(artifact['path'])
                if artifact['state'] == 'publishing':
                    compatible = current if current and current['sha256'] == artifact['sha256'] else None
                    if not self._publish(artifact, compatible['text'] if compatible else None):
                        warnings.add('artifact_incomplete')
                else:
                    try:
                        self._artifact_read(artifact)
                    except FileTaskStoreError:
                        warnings.add('artifact_changed')
                if current is None or current['sha256'] != artifact['sha256'] or current['size_bytes'] != artifact['size_bytes']:
                    warnings.add('file_changed')
            for path, content in found.items():
                if path in frozen_paths or path in existing_paths:
                    continue
                artifact = {'id': str(uuid.uuid4()), 'run_id': run_id, 'chat_id': chat_id,
                            'path': path, 'name': content['name'], 'sha256': content['sha256'],
                            'size_bytes': content['size_bytes'], 'extension': Path(content['name']).suffix.casefold(),
                            'state': 'publishing', 'created_at': _stamp()}
                with self._db(write=True) as db:
                    winner = db.execute('SELECT * FROM file_artifacts WHERE run_id=? AND path=?', (run_id, path)).fetchone()
                    if winner is None:
                        artifact['id'] = self._fresh_identifier(db)
                        db.execute('''INSERT INTO file_artifacts(id,run_id,chat_id,path,name,sha256,size_bytes,extension,state,created_at)
                            VALUES(:id,:run_id,:chat_id,:path,:name,:sha256,:size_bytes,:extension,:state,:created_at)''', artifact)
                        self._touch(db, chat_id)
                    else:
                        artifact = dict(winner)
                if artifact['sha256'] != content['sha256'] or not self._publish(artifact, content['text']):
                    warnings.add('artifact_incomplete')
            for warning in sorted(warnings):
                self._warning(run_id, chat_id, warning)
            return self.get_task(run_id, chat_id=chat_id)

    def finish(self, run_id, status, *, chat_id):
        run_id, chat_id = _uuid(run_id), _uuid(chat_id)
        if not isinstance(status, str) or status not in TERMINAL:
            raise FileTaskStoreError('invalid_arguments')
        self._set_status(run_id, chat_id, status)
        with self._db() as db:
            row = self._owned_task(db, run_id, chat_id)
        try:
            self._mark_stopped(row)
        except (FileTaskError, FileTaskStoreError, OSError):
            self._warning(run_id, chat_id, 'scope_changed')
        return self.reconcile(run_id, chat_id=chat_id)

    def cancel(self, run_id, *, chat_id):
        run_id, chat_id = _uuid(run_id), _uuid(chat_id)
        with self._db() as db:
            row = self._owned_task(db, run_id, chat_id)
        # The marker precedes the DB transition and scanning. In-flight tool
        # saves see it in their own pre/post-publication cancellation checks.
        try:
            self._mark_stopped(row)
        except (FileTaskError, FileTaskStoreError, OSError):
            self._warning(run_id, chat_id, 'scope_changed')
        self._set_status(run_id, chat_id, 'cancelled')
        return self.reconcile(run_id, chat_id=chat_id)

    def recover(self, active_run_ids=()):
        if not isinstance(active_run_ids, (tuple, list, set, frozenset)):
            raise FileTaskStoreError('invalid_arguments')
        self.recover_sources()
        active = set(_uuid(value) for value in active_run_ids)
        with self._db() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM file_tasks ORDER BY run_id').fetchall()]
        result = {'interrupted': 0, 'reconciled': 0}
        for row in rows:
            if row['status'] not in TERMINAL and row['run_id'] not in active:
                self.finish(row['run_id'], 'interrupted', chat_id=row['chat_id'])
                result['interrupted'] += 1
            else:
                self.reconcile(row['run_id'], chat_id=row['chat_id'])
            result['reconciled'] += 1
        return result

    def record_event(self, run_id, event, *, chat_id):
        run_id, chat_id = _uuid(run_id), _uuid(chat_id)
        if (not isinstance(event, dict) or not isinstance(event.get('name'), str)
                or event.get('name') not in {row['name'] for row in TOOLS}
                or event.get('state') not in ('started', 'completed', 'failed')):
            return False
        safe = {'name': event['name'], 'state': event['state']}
        if 'path' in event:
            try:
                safe['path'] = '/'.join(_parts(event['path']))
            except FileTaskError:
                pass
        if isinstance(event.get('sha256'), str) and SHA.fullmatch(event['sha256']):
            safe['sha256'] = event['sha256']
        if type(event.get('size_bytes')) is int and 0 <= event['size_bytes'] <= MAX_FILE_BYTES:
            safe['size_bytes'] = event['size_bytes']
        if type(event.get('content_verified')) is bool:
            safe['content_verified'] = event['content_verified']
        if (isinstance(event.get('error_code'), str) and event['error_code'] in set(ERROR_MESSAGES)
                | {'file_tool_unavailable', 'tool_budget_exhausted', 'tool_result_limit'}):
            safe['error_code'] = event['error_code']
        with self._db(write=True) as db:
            row = self._owned_task(db, run_id, chat_id)
            events = json.loads(row['events'])
            if len(events) >= MAX_EVENTS:
                return False
            events.append(safe)
            db.execute('UPDATE file_tasks SET events=?,updated_at=? WHERE run_id=?', (_json(events), _stamp(), run_id))
            self._touch(db, chat_id)
        return True
