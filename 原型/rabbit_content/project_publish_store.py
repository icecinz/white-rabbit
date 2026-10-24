"""Durable receipts for explicit, create-only publication into one host project.

The browser supplies an opaque generated artifact and a relative destination.
Prepared previews never write the project. Confirmation accepts one immutable
payload before any project I/O; interrupted accepted operations reconcile only
host-recorded staging identities. Verified receipts never repair later edits.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from urllib.parse import quote
import uuid

from .file_task_files import FileTaskError, FileTaskFiles, _parts, _text_bytes
from .project_publish_files import ProjectPublishError, ProjectPublishFiles

PREVIEW_SECONDS = 1800
MAX_PUBLICATIONS = 1000
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
SHA = re.compile(r'[0-9a-f]{64}\Z')
STATES = frozenset(('prepared', 'publishing', 'verified', 'conflict', 'blocked'))
MESSAGES = {
    'invalid_arguments': '项目新增发布参数无效。',
    'not_found': '找不到这份项目新增发布预览。',
    'artifact_changed': '这份成果未通过完整内容核验，没有准备项目发布。',
    'source_not_generated': '只能发布本轮实际生成并核验过的文件成果。',
    'preview_expired': '项目发布预览已过期，请重新预览后确认。',
    'target_exists': '目标已有文件，请换一个新文件名；没有覆盖原文件。',
    'operation_conflict': '这份预览已被另一次确认接收，请查看原发布记录。',
    'scope_changed': '发布目录或目标父目录已变化，没有跟随换目录。',
    'scope_unavailable': '原项目目录暂时无法使用，没有改用其他目录。',
    'capacity': '项目发布记录或暂存内容已达到容量。',
    'io_failed': '项目新增发布暂未完成，请查看原回执。',
    'closed': '项目新增发布记录库已关闭。',
}


class ProjectPublishStoreError(ValueError):
    def __init__(self, code):
        self.code = code if code in MESSAGES else 'io_failed'
        super().__init__(MESSAGES[self.code])


def _uuid(value):
    if not isinstance(value, str):
        raise ProjectPublishStoreError('invalid_arguments')
    try:
        result = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise ProjectPublishStoreError('invalid_arguments') from None
    if result != value:
        raise ProjectPublishStoreError('invalid_arguments')
    return result


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _stamp(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec='microseconds')


def _identity(info):
    return info.st_dev, info.st_ino


class ProjectPublishStore:
    def __init__(self, data_root, documents_root, *, clock=time.time):
        self.lock = threading.RLock()
        self._closed = False
        self._connection = None
        self._db_identity = None
        self._root_fd = None
        self.clock = clock
        self.guard = None
        self.files = None
        self.root = Path(os.path.abspath(os.fspath(data_root)))
        self.project_root = Path(os.path.abspath(os.fspath(documents_root)))
        self.project_name = self.project_root.name
        self.db_path = self.root / 'project_publications.sqlite'
        try:
            self._create_root()
            self.guard = FileTaskFiles(self.root, host_readonly=True)
            self._root_fd = self.guard._open_named_root()
            self.guard._check_root(self._root_fd)
            self.files = ProjectPublishFiles(self.project_root)
            with self._db() as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('PRAGMA synchronous=FULL')
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS project_publications (
                        id TEXT PRIMARY KEY, artifact TEXT NOT NULL, chat_id TEXT NOT NULL,
                        project_name TEXT NOT NULL, project_root TEXT NOT NULL,
                        target_path TEXT NOT NULL, scope TEXT NOT NULL,
                        target_exists INTEGER NOT NULL CHECK(target_exists IN (0,1)),
                        target_text TEXT,
                        sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('prepared','publishing','verified','conflict','blocked')),
                        client_id TEXT UNIQUE, error_code TEXT,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        expires_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS project_publication_payloads (
                        publication_id TEXT PRIMARY KEY REFERENCES project_publications(id),
                        payload BLOB NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS project_publication_stages (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        publication_id TEXT NOT NULL REFERENCES project_publications(id),
                        name TEXT NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TRIGGER IF NOT EXISTS publication_identity_immutable
                    BEFORE UPDATE OF id,artifact,chat_id,project_name,project_root,target_path,scope,target_exists,target_text,sha256,size_bytes,created_at,expires_at
                    ON project_publications
                    BEGIN SELECT RAISE(ABORT,'publication_identity_immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS publication_client_immutable
                    BEFORE UPDATE OF client_id ON project_publications
                    WHEN OLD.client_id IS NOT NULL AND NEW.client_id IS NOT OLD.client_id
                    BEGIN SELECT RAISE(ABORT,'publication_client_immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS publication_terminal_immutable
                    BEFORE UPDATE OF state ON project_publications
                    WHEN OLD.state IN ('verified','conflict','blocked') AND NEW.state IS NOT OLD.state
                    BEGIN SELECT RAISE(ABORT,'publication_terminal_immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS publication_no_delete BEFORE DELETE ON project_publications
                    BEGIN SELECT RAISE(ABORT,'publication_no_delete'); END;
                    CREATE TRIGGER IF NOT EXISTS publication_payload_immutable BEFORE UPDATE ON project_publication_payloads
                    BEGIN SELECT RAISE(ABORT,'publication_payload_immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS publication_payload_no_delete BEFORE DELETE ON project_publication_payloads
                    BEGIN SELECT RAISE(ABORT,'publication_payload_no_delete'); END;
                    CREATE TRIGGER IF NOT EXISTS publication_stage_immutable BEFORE UPDATE ON project_publication_stages
                    BEGIN SELECT RAISE(ABORT,'publication_stage_immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS publication_stage_no_delete BEFORE DELETE ON project_publication_stages
                    BEGIN SELECT RAISE(ABORT,'publication_stage_no_delete'); END;
                ''')
        except BaseException as error:
            if self.files is not None:
                self.files.close()
            if self.guard is not None:
                self.guard.close()
            if self._connection is not None:
                self._connection.close()
            if self._root_fd is not None:
                os.close(self._root_fd)
                self._root_fd = None
            if isinstance(error, ProjectPublishError):
                raise ProjectPublishStoreError('scope_unavailable') from None
            if isinstance(error, FileTaskError):
                raise ProjectPublishStoreError('scope_changed') from None
            if isinstance(error, sqlite3.Error):
                raise ProjectPublishStoreError('io_failed') from None
            raise

    def _create_root(self):
        descriptors = []
        try:
            descriptor = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(descriptor)
            for part in self.root.parts[1:]:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                descriptors.append(descriptor)
        except OSError:
            raise ProjectPublishStoreError('scope_changed') from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            self.files.close()
            self.guard.close()
            self._connection.close()
            self._connection = None
            os.close(self._root_fd)
            self._root_fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _check(self):
        if self._closed:
            raise ProjectPublishStoreError('closed')
        try:
            self.guard.check_cancelled()
            self.guard._check_root(self._root_fd)
        except FileTaskError:
            raise ProjectPublishStoreError('scope_changed') from None

    def _verify_db(self, create=False):
        descriptor = None
        try:
            self.guard._check_root(self._root_fd)
            if create:
                try:
                    descriptor = os.open(self.db_path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=self._root_fd)
                    os.fsync(descriptor)
                    os.fsync(self._root_fd)
                except FileExistsError:
                    pass
            if descriptor is None:
                descriptor = os.open(self.db_path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=self._root_fd)
            info = os.fstat(descriptor)
            named = os.stat(self.db_path.name, dir_fd=self._root_fd, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or _identity(info) != _identity(named)):
                raise ProjectPublishStoreError('scope_changed')
            if self._db_identity is None:
                self._db_identity = _identity(info)
            elif self._db_identity != _identity(info):
                raise ProjectPublishStoreError('scope_changed')
            self.guard._check_root(self._root_fd)
        except (OSError, FileTaskError):
            raise ProjectPublishStoreError('scope_changed') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @contextmanager
    def _db(self, *, write=False):
        with self.lock:
            self._check()
            db = None
            try:
                self._verify_db(create=self._connection is None)
                if self._connection is None:
                    db = sqlite3.connect('file:' + quote(str(self.db_path), safe='/') + '?mode=rw', uri=True,
                                         timeout=10, isolation_level=None, check_same_thread=False)
                    self._verify_db()
                    if db.execute('PRAGMA database_list').fetchone()[2] != str(self.db_path):
                        raise ProjectPublishStoreError('scope_changed')
                    db.row_factory = sqlite3.Row
                    db.execute('PRAGMA foreign_keys=ON')
                    self._connection = db
                else:
                    db = self._connection
                if write:
                    db.execute('BEGIN IMMEDIATE')
                yield db
                self._check()
                self._verify_db()
                if write:
                    db.commit()
            except BaseException as error:
                if db is not None and write and db.in_transaction:
                    db.rollback()
                if db is not None and db is not self._connection:
                    db.close()
                if isinstance(error, sqlite3.Error):
                    raise ProjectPublishStoreError('io_failed') from None
                raise

    def _row(self, identifier):
        with self._db() as db:
            row = db.execute('SELECT * FROM project_publications WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ProjectPublishStoreError('not_found')
        return dict(row)

    def _stage(self, identifier):
        with self._db() as db:
            row = db.execute('''SELECT name,device,inode FROM project_publication_stages
                                WHERE publication_id=? ORDER BY sequence DESC LIMIT 1''', (identifier,)).fetchone()
        return None if row is None else {'name': row['name'], 'dev': row['device'], 'ino': row['inode']}

    def _payload(self, row):
        with self._db() as db:
            record = db.execute('SELECT payload FROM project_publication_payloads WHERE publication_id=?', (row['id'],)).fetchone()
        if record is None:
            raise ProjectPublishStoreError('io_failed')
        raw = bytes(record['payload'])
        if len(raw) != row['size_bytes'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ProjectPublishStoreError('artifact_changed')
        try:
            text = raw.decode('utf-8')
            if _text_bytes(text) != raw:
                raise ProjectPublishStoreError('artifact_changed')
        except (UnicodeError, FileTaskError):
            raise ProjectPublishStoreError('artifact_changed') from None
        return text

    def _public(self, row):
        current_verified, error = False, row['error_code']
        if row['state'] == 'verified':
            try:
                if row['project_root'] != str(self.project_root):
                    raise ProjectPublishStoreError('scope_changed')
                stage = self._stage(row['id'])
                if stage is None:
                    raise ProjectPublishStoreError('io_failed')
                self.files.verify(row['target_path'], json.loads(row['scope']), stage,
                                  row['sha256'], row['size_bytes'])
                current_verified = True
                error = None
            except (ProjectPublishError, ProjectPublishStoreError, OSError):
                error = 'current_changed'
        return {'id': row['id'], 'project_name': row['project_name'], 'target_path': row['target_path'],
                'artifact': json.loads(row['artifact']), 'state': row['state'],
                'created_at': row['created_at'], 'updated_at': row['updated_at'],
                'current_verified': current_verified, 'error_code': error,
                'can_confirm': (row['state'] == 'prepared' and not row['target_exists']
                                and row['project_root'] == str(self.project_root)
                                and self.clock() <= row['expires_at'])}

    def prepare(self, artifact, text, relative_path):
        with self.lock:
            self._check()
            if not isinstance(artifact, dict) or any(key not in artifact for key in
                    ('id', 'chat_id', 'run_id', 'name', 'sha256', 'size_bytes', 'available', 'content_verified')):
                raise ProjectPublishStoreError('source_not_generated')
            if 'source' in artifact or 'provenance' in artifact:
                raise ProjectPublishStoreError('source_not_generated')
            if artifact['available'] is not True or artifact['content_verified'] is not True:
                raise ProjectPublishStoreError('artifact_changed')
            saved = {key: artifact[key] for key in ('id', 'chat_id', 'run_id', 'name', 'sha256', 'size_bytes')}
            for key in ('id', 'chat_id', 'run_id'):
                _uuid(saved[key])
            try:
                if len(_parts(saved['name'])) != 1:
                    raise FileTaskError('invalid_path')
                raw = _text_bytes(text)
            except FileTaskError:
                raise ProjectPublishStoreError('invalid_arguments') from None
            if (not isinstance(saved['sha256'], str) or SHA.fullmatch(saved['sha256']) is None
                    or type(saved['size_bytes']) is not int or saved['size_bytes'] != len(raw)
                    or saved['sha256'] != hashlib.sha256(raw).hexdigest()):
                raise ProjectPublishStoreError('artifact_changed')
            return self._prepare_text(saved, saved['chat_id'], text, relative_path)

    def _prepare_text(self, source, chat_id, text, relative_path):
        """Host-only finite text preparation after the caller's source proof.

        The original artifact entry point keeps its generated-source checks.
        A separate host-owned registry can supply its own typed source instead
        of inventing a generated artifact or changing the publication engine.
        """
        with self.lock:
            self._check()
            chat_id = _uuid(chat_id)
            try:
                if type(source) is not dict:
                    raise ValueError
                saved = json.loads(json.dumps(source, ensure_ascii=False, sort_keys=True,
                                              separators=(',', ':'), allow_nan=False))
                if len(_json(saved).encode('utf-8')) > 16 * 1024:
                    raise ValueError
                raw = _text_bytes(text)
            except (ValueError, TypeError, UnicodeError, RecursionError, FileTaskError):
                raise ProjectPublishStoreError('invalid_arguments') from None
            try:
                inspected = self.files.inspect(relative_path)
            except ProjectPublishError as error:
                code = error.code if error.code in MESSAGES else 'invalid_arguments'
                raise ProjectPublishStoreError(code) from None
            timestamp = self.clock()
            row = {'id': str(uuid.uuid4()), 'artifact': _json(saved), 'chat_id': chat_id, 'target_path': inspected['path'],
                   'project_name': self.project_name, 'project_root': str(self.project_root),
                   'scope': _json(inspected['scope']), 'target_exists': int(inspected['exists']),
                   'target_text': inspected.get('text'), 'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw),
                   'state': 'prepared', 'client_id': None, 'error_code': None,
                   'created_at': _stamp(timestamp), 'updated_at': _stamp(timestamp),
                   'expires_at': timestamp + PREVIEW_SECONDS}
            with self._db(write=True) as db:
                count, total = db.execute('SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM project_publications').fetchone()
                if count >= MAX_PUBLICATIONS or total + len(raw) > MAX_PAYLOAD_BYTES:
                    raise ProjectPublishStoreError('capacity')
                db.execute('''INSERT INTO project_publications
                    (id,artifact,chat_id,project_name,project_root,target_path,scope,target_exists,target_text,sha256,size_bytes,state,client_id,error_code,created_at,updated_at,expires_at)
                    VALUES(:id,:artifact,:chat_id,:project_name,:project_root,:target_path,:scope,:target_exists,:target_text,:sha256,:size_bytes,:state,:client_id,:error_code,:created_at,:updated_at,:expires_at)''', row)
                db.execute('INSERT INTO project_publication_payloads(publication_id,payload) VALUES(?,?)',
                           (row['id'], sqlite3.Binary(raw)))
            return {'publication': self._public(row), 'preview': {'source_text': text,
                    'target_exists': bool(row['target_exists']), 'target_text': row['target_text'],
                    'can_confirm': self._public(row)['can_confirm']}}

    def get(self, publication_id):
        with self.lock:
            return self._public(self._row(_uuid(publication_id)))

    def preview(self, publication_id):
        with self.lock:
            row = self._row(_uuid(publication_id))
            return {'publication': self._public(row), 'preview': {'source_text': self._payload(row),
                    'target_exists': bool(row['target_exists']), 'target_text': row['target_text'],
                    'can_confirm': self._public(row)['can_confirm']}}

    def list(self, chat_id=None, *, include_prepared=False, limit=100):
        if type(limit) is not int or not 1 <= limit <= MAX_PUBLICATIONS or type(include_prepared) is not bool:
            raise ProjectPublishStoreError('invalid_arguments')
        if chat_id is not None:
            chat_id = _uuid(chat_id)
        clauses, parameters = [], []
        if chat_id is not None:
            clauses.append('chat_id=?')
            parameters.append(chat_id)
        if not include_prepared:
            clauses.append('client_id IS NOT NULL')
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        parameters.append(limit)
        with self.lock:
            with self._db() as db:
                rows = [dict(row) for row in db.execute('SELECT * FROM project_publications' + where +
                    ' ORDER BY created_at DESC,id LIMIT ?', parameters)]
            return [self._public(row) for row in rows]

    def revision(self, chat_id):
        chat_id = _uuid(chat_id)
        with self._db() as db:
            rows = [list(row) for row in db.execute('''SELECT id,state,error_code,updated_at
                FROM project_publications WHERE chat_id=? AND client_id IS NOT NULL ORDER BY id''', (chat_id,))]
        return hashlib.sha256(_json(rows).encode('utf-8')).hexdigest()

    def find_receipt(self, client_id):
        client_id = _uuid(client_id)
        with self.lock:
            with self._db() as db:
                row = db.execute('SELECT * FROM project_publications WHERE client_id=?', (client_id,)).fetchone()
            return {'received': False} if row is None else {'received': True, 'state': row['state'], 'publication': self._public(dict(row))}

    def confirm(self, publication_id, client_id):
        client_id = _uuid(client_id)
        with self.lock:
            receipt = self.find_receipt(client_id)
            if receipt['received']:
                return receipt
            row = self._row(_uuid(publication_id))
            if row['state'] != 'prepared':
                raise ProjectPublishStoreError('operation_conflict')
            if self.clock() > row['expires_at']:
                raise ProjectPublishStoreError('preview_expired')
            if row['target_exists']:
                raise ProjectPublishStoreError('target_exists')
            with self._db(write=True) as db:
                existing = db.execute('SELECT * FROM project_publications WHERE client_id=?', (client_id,)).fetchone()
                if existing is not None:
                    return {'received': True, 'state': existing['state'], 'publication': self._public(dict(existing))}
                accepted = db.execute('''UPDATE project_publications SET client_id=?,state='publishing',updated_at=?
                    WHERE id=? AND state='prepared' AND client_id IS NULL''', (client_id, _stamp(self.clock()), row['id'])).rowcount
                if accepted != 1:
                    raise ProjectPublishStoreError('operation_conflict')
            self._publish(self._row(row['id']))
            return self.find_receipt(client_id)

    def _record_stage(self, row, stage):
        if (not isinstance(stage, dict) or set(stage) != {'name', 'dev', 'ino'}
                or stage['name'] != '.rabbit-publish-' + row['id']
                or any(type(stage[key]) is not int or stage[key] < 0 for key in ('dev', 'ino'))):
            raise ProjectPublishStoreError('io_failed')
        with self._db(write=True) as db:
            current = db.execute('SELECT state FROM project_publications WHERE id=?', (row['id'],)).fetchone()
            if current is None or current['state'] != 'publishing':
                raise ProjectPublishStoreError('operation_conflict')
            db.execute('''INSERT INTO project_publication_stages(publication_id,name,device,inode,created_at)
                VALUES(?,?,?,?,?)''', (row['id'], stage['name'], stage['dev'], stage['ino'], _stamp(self.clock())))

    def _finish(self, row, state, code=None):
        with self._db(write=True) as db:
            db.execute("UPDATE project_publications SET state=?,error_code=?,updated_at=? WHERE id=? AND state='publishing'",
                       (state, code, _stamp(self.clock()), row['id']))

    def _publish(self, row):
        if row['state'] != 'publishing' or row['client_id'] is None:
            return
        try:
            if row['project_root'] != str(self.project_root):
                self._finish(row, 'conflict', 'scope_changed')
                return
            text = self._payload(row)
            stage = self._stage(row['id'])
            scope = json.loads(row['scope'])
            if stage is None:
                self.files.create_new(row['target_path'], text, row['id'], scope,
                                      lambda identity: self._record_stage(row, identity))
            else:
                self.files.recover(row['target_path'], scope, stage, row['sha256'], row['size_bytes'])
            stage = self._stage(row['id'])
            if stage is None:
                raise ProjectPublishStoreError('io_failed')
            self.files.verify(row['target_path'], scope, stage, row['sha256'], row['size_bytes'])
            self._finish(row, 'verified')
        except ProjectPublishError as error:
            conflict = error.code in ('file_exists', 'file_changed', 'scope_changed')
            self._finish(row, 'conflict' if conflict else 'blocked', error.code)
        except ProjectPublishStoreError as error:
            self._finish(row, 'blocked', error.code)
        except OSError:
            self._finish(row, 'blocked', 'io_failed')

    def recover_accepted(self):
        with self.lock:
            with self._db() as db:
                rows = [dict(row) for row in db.execute("SELECT * FROM project_publications WHERE state='publishing' AND client_id IS NOT NULL ORDER BY created_at,id")]
            counts = {'verified': 0, 'conflict': 0, 'blocked': 0, 'publishing': 0}
            for row in rows:
                self._publish(row)
                counts[self._row(row['id'])['state']] += 1
            return counts
