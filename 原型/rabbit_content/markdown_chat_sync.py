"""Durable explicit updates of a registered Markdown snapshot.

The file commit is a recoverable exchange, not a SHA-conditional filesystem
CAS. Recovery only observes recorded objects; it never exchanges them again.
Original create-only publication records remain immutable.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid

from .markdown_chat_document import parse
from .markdown_chat_registry import MAX_FILE_BYTES, source_identity
from .project_publish_files import ProjectPublishFiles, _parts, _valid_identity
from .project_publish_store import MAX_PAYLOAD_BYTES, MAX_PUBLICATIONS, PREVIEW_SECONDS, _stamp, _uuid


SYNC_OPERATIONS_DDL = '''CREATE TABLE markdown_sync_operations (
    id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES project_publications(id),
    expected_head_id TEXT NOT NULL,
    previous_source TEXT NOT NULL,
    source TEXT NOT NULL,
    target_path TEXT NOT NULL,
    scope TEXT NOT NULL,
    old_identity TEXT NOT NULL,
    payload BLOB NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('prepared','publishing','verified','conflict','blocked')),
    client_id TEXT UNIQUE,
    stage_identity TEXT,
    error_code TEXT,
    retained INTEGER NOT NULL CHECK(retained IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at REAL NOT NULL
)'''
SYNC_HEADS_DDL = '''CREATE TABLE markdown_sync_heads (
    publication_id TEXT PRIMARY KEY REFERENCES project_publications(id),
    head_id TEXT NOT NULL
)'''
SYNC_PENDING_INDEX_DDL = '''CREATE UNIQUE INDEX markdown_sync_one_publishing
    ON markdown_sync_operations(binding_id) WHERE state='publishing' '''.strip()
SYNC_IDENTITY_DDL = '''CREATE TRIGGER markdown_sync_identity_immutable
    BEFORE UPDATE OF id,binding_id,expected_head_id,previous_source,source,target_path,
        scope,old_identity,payload,created_at,expires_at ON markdown_sync_operations
    BEGIN SELECT RAISE(ABORT,'markdown_sync_identity_immutable'); END'''
SYNC_CLIENT_DDL = '''CREATE TRIGGER markdown_sync_client_immutable
    BEFORE UPDATE OF client_id ON markdown_sync_operations
    WHEN OLD.client_id IS NOT NULL AND NEW.client_id IS NOT OLD.client_id
    BEGIN SELECT RAISE(ABORT,'markdown_sync_client_immutable'); END'''
SYNC_STAGE_DDL = '''CREATE TRIGGER markdown_sync_stage_immutable
    BEFORE UPDATE OF stage_identity ON markdown_sync_operations
    WHEN OLD.stage_identity IS NOT NULL AND NEW.stage_identity IS NOT OLD.stage_identity
    BEGIN SELECT RAISE(ABORT,'markdown_sync_stage_immutable'); END'''
SYNC_TERMINAL_DDL = '''CREATE TRIGGER markdown_sync_terminal_immutable
    BEFORE UPDATE OF state,error_code,retained,updated_at ON markdown_sync_operations
    WHEN OLD.state IN ('verified','conflict','blocked')
    BEGIN SELECT RAISE(ABORT,'markdown_sync_terminal_immutable'); END'''
SYNC_NO_DELETE_DDL = '''CREATE TRIGGER markdown_sync_no_delete
    BEFORE DELETE ON markdown_sync_operations
    BEGIN SELECT RAISE(ABORT,'markdown_sync_no_delete'); END'''

MESSAGES = {
    'invalid_arguments': '整理更新参数无效。',
    'invalid_document': '新的整理全文无法核对。',
    'reference_only': '索引引用不保存聊天快照，不需要更新正文。',
    'document_changed': '新的整理全文与已核摘要不一致。',
    'directory_unavailable': '文件夹没有完整核对，未更新文件。',
    'head_changed': '已关联快照版本已有变化，请重新预览。',
    'source_changed': '新旧快照不属于同一个来源。',
    'file_changed': '文件或保留对象已有变化，请查看冲突记录。',
    'stage_missing': '没有已登记的暂存证据，未再次写入。',
    'stage_unapplied': '原更新未得到交换完成证明，未再次交换。',
    'observation_unknown': '原更新结果无法确认，未再次交换。',
    'current_changed': '已完成更新的文件目前已有变化。',
    'preview_expired': '更新预览已过期，请重新预览。',
    'operation_conflict': '这个确认编号已有不同更新归属。',
    'capacity': '整理更新记录或全文已达到容量。',
    'not_found': '找不到这份整理更新记录。',
    'schema_changed': '整理更新记录结构无法核对。',
    'closed': '整理更新接口已经关闭。',
    'io_failed': '整理更新尚未完成，请查看原回执。',
    'unsupported': '当前环境不支持保留原对象的文件交换。',
    'invalid_path': '整理更新的相对路径无效。',
    'invalid_text': '整理更新需要完整UTF-8文字。',
    'scope_unavailable': '原整理目录暂不可用。',
    'scope_changed': '原整理目录或父目录已经变化。',
    'unsafe_file': '整理更新遇到链接或特殊文件。',
    'stage_exists': '原暂存位置已有文件，未改用其他位置。',
    'file_exists': '目标位置已有其它文件。',
    'unsupported_platform': '当前平台不支持保留原对象的文件交换。',
    'swap_unsupported': '当前卷或系统不支持保留原对象的文件交换。',
    'readonly_file': '原整理文件不可写，没有替换文件。',
}
ERROR_CODES = frozenset(MESSAGES)
STATES = frozenset(('prepared', 'publishing', 'verified', 'conflict', 'blocked'))


class MarkdownChatSyncError(ValueError):
    def __init__(self, code, *, unchanged=False):
        self.code = code if code in ERROR_CODES else 'io_failed'
        super().__init__('这份快照已是当前版本，没有创建更新请求' if unchanged else MESSAGES[self.code])


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _files(root):
    # Lazy import also lets pure ledger tests supply a controlled file peer.
    from .markdown_sync_files import MarkdownSyncFiles
    return MarkdownSyncFiles(root)


@contextmanager
def _errors():
    try:
        yield
    except MarkdownChatSyncError:
        raise
    except (ValueError, TypeError, OSError, sqlite3.Error) as error:
        raise MarkdownChatSyncError(getattr(error, 'code', 'io_failed')) from None


class MarkdownChatSync:
    def __init__(self, registry, *, clock=time.time):
        self.registry = registry
        self.publisher = registry.publisher
        self.lock = registry.lock
        self.clock = clock
        self.files = None
        self._support_error = None
        try:
            self.files = _files(registry.directory.root)
        except ValueError as error:
            if getattr(error, 'code', None) not in ('unsupported_platform', 'swap_unsupported'):
                raise
            self._support_error = error.code
        self._closed = False
        definitions = {
            'markdown_sync_operations': SYNC_OPERATIONS_DDL,
            'markdown_sync_heads': SYNC_HEADS_DDL,
            'markdown_sync_one_publishing': SYNC_PENDING_INDEX_DDL,
            'markdown_sync_identity_immutable': SYNC_IDENTITY_DDL,
            'markdown_sync_client_immutable': SYNC_CLIENT_DDL,
            'markdown_sync_stage_immutable': SYNC_STAGE_DDL,
            'markdown_sync_terminal_immutable': SYNC_TERMINAL_DDL,
            'markdown_sync_no_delete': SYNC_NO_DELETE_DDL,
        }
        try:
            with self.lock, _errors(), self.publisher._db(write=True) as db:
                for name, ddl in definitions.items():
                    row = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
                    if row is None:
                        db.execute(ddl)
                    elif type(row['sql']) is not str or ' '.join(row['sql'].split()) != ' '.join(ddl.split()):
                        raise MarkdownChatSyncError('schema_changed')
                if db.execute('''SELECT 1 FROM markdown_sync_operations operation
                    JOIN project_publications publication ON publication.id=operation.binding_id
                    WHERE NOT EXISTS (SELECT 1 FROM markdown_sync_heads head
                        WHERE head.publication_id=publication.id) LIMIT 1''').fetchone():
                    raise MarkdownChatSyncError('schema_changed')
                db.execute('''INSERT OR IGNORE INTO markdown_sync_heads
                    SELECT id,id FROM project_publications WHERE state='verified' AND client_id IS NOT NULL''')
        except BaseException:
            if self.files is not None:
                self.files.close()
            raise

    @property
    def supported(self):
        """Platform/symbol capability only; the selected volume may still fail."""
        return self.files is not None and not self._closed

    def _check(self):
        if self._closed:
            raise MarkdownChatSyncError('closed')

    def close(self):
        with self.lock:
            if not self._closed:
                self._closed = True
                if self.files is not None:
                    self.files.close()

    @contextmanager
    def _ownership(self):
        """Coordinate our exchangers/recovery on the existing database inode.

        A separate open description is intentional: independent registry
        instances in this process must contend too. A process exit releases
        the advisory lock; no persistent lock file or lease is introduced.
        This has no authority over editors writing the Markdown document.
        """
        descriptor, locked = None, False
        try:
            self.publisher._verify_db()
            descriptor = os.open(self.publisher.db_path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.publisher._root_fd)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != self.publisher._db_identity):
                raise MarkdownChatSyncError('scope_changed')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                pass
            self.publisher._check()
            self.publisher._verify_db()
            named = os.stat(self.publisher.db_path.name,
                dir_fd=self.publisher._root_fd, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
                raise MarkdownChatSyncError('scope_changed')
            yield locked
        finally:
            if descriptor is not None:
                try:
                    if locked:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def register_binding(self, publication_id):
        with self.lock, _errors(), self.publisher._db(write=True) as db:
            publication_id = _uuid(publication_id)
            if (not db.execute('SELECT 1 FROM markdown_sync_heads WHERE publication_id=?', (publication_id,)).fetchone()
                    and db.execute('SELECT 1 FROM markdown_sync_operations WHERE binding_id=?', (publication_id,)).fetchone()):
                raise MarkdownChatSyncError('schema_changed')
            db.execute('''INSERT OR IGNORE INTO markdown_sync_heads
                SELECT id,id FROM project_publications
                WHERE id=? AND state='verified' AND client_id IS NOT NULL''', (publication_id,))

    def _checked(self, row):
        row = dict(row)
        for key in ('id', 'binding_id', 'expected_head_id'):
            _uuid(row[key])
        previous = self.registry._source(json.loads(row['previous_source']))
        source = self.registry._source(json.loads(row['source']))
        if 'history_reference' in (previous['kind'], source['kind']):
            raise MarkdownChatSyncError('reference_only')
        if source_identity(previous) != source_identity(source):
            raise MarkdownChatSyncError('source_changed')
        raw = bytes(row['payload'])
        if (len(raw) > MAX_FILE_BYTES or len(raw) != source['size_bytes']
                or hashlib.sha256(raw).hexdigest() != source['sha256']):
            raise MarkdownChatSyncError('document_changed')
        parsed = parse(raw, workspace_id=self.registry.workspace_id)
        metadata = parsed['metadata']
        if (not parsed['body_matches_header'] or metadata['rabbit_id'] != source['chat_id']
                or metadata['revision'] != source['revision']
                or (metadata['kind'] == 'native_index') != (source['kind'] == 'native_index')
                or source['kind'] == 'native_index' and metadata['source']['agent'] != source['agent']):
            raise MarkdownChatSyncError('document_changed')
        if '\0' in raw.decode('utf-8'):
            raise MarkdownChatSyncError('invalid_text')
        scope = json.loads(row['scope'])
        ProjectPublishFiles._validate_scope(scope, _parts(row['target_path']))
        identity = json.loads(row['old_identity'])
        if not _valid_identity(identity) or row['state'] not in STATES or row['retained'] not in (0, 1):
            raise MarkdownChatSyncError('schema_changed')
        if row['client_id'] is not None:
            _uuid(row['client_id'])
        if (row['state'] == 'prepared') != (row['client_id'] is None):
            raise MarkdownChatSyncError('schema_changed')
        if row['error_code'] is not None and row['error_code'] not in ERROR_CODES:
            raise MarkdownChatSyncError('schema_changed')
        stage = json.loads(row['stage_identity']) if row['stage_identity'] is not None else None
        if stage is not None and (type(stage) is not dict or set(stage) != {'name', 'dev', 'ino'}
                or stage['name'] != '.rabbit-sync-' + row['id']
                or not _valid_identity({key: stage.get(key) for key in ('dev', 'ino')})):
            raise MarkdownChatSyncError('schema_changed')
        return dict(row, previous_source=previous, source=source, scope=scope,
                    old_identity=identity, stage_identity=stage, payload=raw)

    def _row(self, identifier, db=None):
        if db is None:
            with self.publisher._db() as connection:
                return self._row(identifier, connection)
        row = db.execute('SELECT * FROM markdown_sync_operations WHERE id=?', (_uuid(identifier),)).fetchone()
        if row is None:
            raise MarkdownChatSyncError('not_found')
        return self._checked(row)

    def _head(self, db, binding_id, *, base_verified=False):
        row = db.execute('SELECT head_id FROM markdown_sync_heads WHERE publication_id=?', (binding_id,)).fetchone()
        if row is None:
            if base_verified:
                if db.execute('SELECT 1 FROM markdown_sync_operations WHERE binding_id=?', (binding_id,)).fetchone():
                    raise MarkdownChatSyncError('schema_changed')
                # The original publication can become verified just before
                # its separate head-registration transaction. Reading that
                # trusted base is safe; reading never creates a head row.
                return binding_id
            raise MarkdownChatSyncError('not_found')
        return _uuid(row['head_id'])

    def overlay_bindings(self, base_bindings):
        with self.lock, _errors(), self.publisher._db() as db:
            self._check()
            result = []
            for base in base_bindings:
                binding = deepcopy(base)
                head = self._head(db, base['publication_id'], base_verified=True)
                binding['head_id'] = head
                if head != base['publication_id']:
                    row = self._row(head, db)
                    if (row['state'] != 'verified' or row['binding_id'] != base['publication_id']
                            or source_identity(row['source']) != source_identity(base['source'])):
                        raise MarkdownChatSyncError('schema_changed')
                    binding.update(source=row['source'], target_path=row['target_path'], accepted_at=row['updated_at'])
                result.append(binding)
            return result

    def binding(self, binding_id):
        with self.lock, _errors():
            self._check()
            binding_id = _uuid(binding_id)
            for row in self.overlay_bindings(self.registry._base_bindings()):
                if row['publication_id'] == binding_id:
                    return row
            raise MarkdownChatSyncError('not_found')

    def _observation(self, row):
        if self.files is None:
            raise MarkdownChatSyncError(self._support_error or 'unsupported')
        return self.files.observe(row['target_path'], row['id'], row['scope'], row['old_identity'],
            row['previous_source']['sha256'], row['previous_source']['size_bytes'], row['stage_identity'],
            row['source']['sha256'], row['source']['size_bytes'])

    def _public(self, row):
        current = False
        error = row['error_code']
        if row['state'] == 'verified':
            try:
                current = self._observation(row)['state'] == 'applied'
            except (ValueError, OSError):
                pass
            if not current:
                error = 'current_changed'
        return deepcopy({key: row[key] for key in (
            'id', 'binding_id', 'expected_head_id', 'previous_source', 'source', 'target_path',
            'state', 'created_at', 'updated_at')} | dict(current_verified=current,
                can_confirm=row['state'] == 'prepared' and self.clock() <= row['expires_at'],
                error_code=error, retained=bool(row['retained'])))

    def prepare(self, binding_id, expected_head_id, host_render_document):
        with self.lock, _errors():
            self._check()
            if not self.supported:
                raise MarkdownChatSyncError(self._support_error or 'unsupported')
            binding_id, expected_head_id = _uuid(binding_id), _uuid(expected_head_id)
            binding = self.binding(binding_id)
            if binding['source']['kind'] == 'history_reference':
                raise MarkdownChatSyncError('reference_only')
            if binding['head_id'] != expected_head_id:
                raise MarkdownChatSyncError('head_changed')
            document = host_render_document
            if type(document) is not dict or set(document) != {'data', 'sha256', 'size_bytes', 'metadata'}:
                raise MarkdownChatSyncError('invalid_document')
            raw = document['data']
            if type(raw) is not bytes or len(raw) > MAX_FILE_BYTES:
                raise MarkdownChatSyncError('capacity' if type(raw) is bytes else 'invalid_document')
            parsed = parse(raw, workspace_id=self.registry.workspace_id)
            if (parsed['metadata'] != document['metadata'] or parsed['sha256'] != document['sha256']
                    or type(document['size_bytes']) is not int or len(raw) != document['size_bytes']
                    or not parsed['body_matches_header'] or '\0' in raw.decode('utf-8')):
                raise MarkdownChatSyncError('document_changed')
            metadata = parsed['metadata']
            source = dict(kind='native_index' if metadata['kind'] == 'native_index' else 'markdown_chat',
                workspace_id=self.registry.workspace_id, chat_id=metadata['rabbit_id'],
                revision=metadata['revision'], sha256=parsed['sha256'], size_bytes=len(raw))
            if source['kind'] == 'native_index':
                source['agent'] = metadata['source']['agent']
            source = self.registry._source(source)
            if source_identity(source) != source_identity(binding['source']):
                raise MarkdownChatSyncError('source_changed')
            if source['revision'] == binding['source']['revision']:
                raise MarkdownChatSyncError('invalid_arguments', unchanged=True)
            bindings = self.overlay_bindings(self.registry._base_bindings())
            scan = self.registry.scan({source_identity(item['source']): item['source']['revision'] for item in bindings})
            selected = next((item for item in scan['chats']
                if (item.get('kind', 'markdown_chat'), item.get('agent', ''), item['rabbit_id']) ==
                   (source['kind'], source.get('agent', ''), source['chat_id'])), None)
            if not scan['complete']:
                raise MarkdownChatSyncError('directory_unavailable')
            if selected is None or selected['status'] not in ('current', 'db_stale') or len(selected['paths']) != 1:
                raise MarkdownChatSyncError('file_changed')
            inspected = self.files.inspect(selected['paths'][0])
            if (not inspected['exists'] or inspected['sha256'] != binding['source']['sha256']
                    or inspected['size_bytes'] != binding['source']['size_bytes']):
                raise MarkdownChatSyncError('file_changed')
            now = self.clock()
            identifier = str(uuid.uuid4())
            with self.publisher._db(write=True) as db:
                # Populate a newly verified original only if no sync history
                # exists. Never repair a missing head over prior operations.
                if self._head(db, binding_id, base_verified=True) == binding_id:
                    db.execute('INSERT OR IGNORE INTO markdown_sync_heads VALUES(?,?)', (binding_id, binding_id))
                if self._head(db, binding_id) != expected_head_id:
                    raise MarkdownChatSyncError('head_changed')
                count, size = db.execute('SELECT COUNT(*),COALESCE(SUM(length(payload)),0) FROM markdown_sync_operations').fetchone()
                if count >= MAX_PUBLICATIONS or size + len(raw) > MAX_PAYLOAD_BYTES:
                    raise MarkdownChatSyncError('capacity')
                db.execute('''INSERT INTO markdown_sync_operations VALUES(
                    ?,?,?,?,?,?,?,?,?, 'prepared',NULL,NULL,NULL,0,?,?,?)''',
                    (identifier, binding_id, expected_head_id, _json(binding['source']), _json(source),
                     inspected['path'], _json(inspected['scope']), _json(inspected['identity']), sqlite3.Binary(raw),
                     _stamp(now), _stamp(now), now + PREVIEW_SECONDS))
            return self.preview(identifier)

    def preview(self, identifier):
        with self.lock, _errors():
            self._check()
            row = self._row(identifier)
            dto = self._public(row)
            return dict(update=dto, preview=dict(source_text=row['payload'].decode('utf-8'), can_confirm=dto['can_confirm']))

    def find_receipt(self, client_id):
        with self.lock, _errors(), self.publisher._db() as db:
            self._check()
            row = db.execute('SELECT * FROM markdown_sync_operations WHERE client_id=?', (_uuid(client_id),)).fetchone()
            if row is None:
                return {'received': False}
            row = self._checked(row)
            return dict(received=True, state=row['state'], update=self._public(row))

    def _record_stage(self, identifier, stage):
        with self.publisher._db(write=True) as db:
            row = self._row(identifier, db)
            # Validate the recorded identity using the same persisted codec.
            raw = db.execute('SELECT * FROM markdown_sync_operations WHERE id=?', (identifier,)).fetchone()
            self._checked(dict(raw, stage_identity=_json(stage)))
            if row['state'] != 'publishing' or row['stage_identity'] is not None:
                raise MarkdownChatSyncError('operation_conflict')
            db.execute('UPDATE markdown_sync_operations SET stage_identity=? WHERE id=?', (_json(stage), identifier))

    def _settle(self, row, *, apply_error=None):
        state, code, retained = 'blocked', apply_error or 'stage_missing', False
        if row['stage_identity'] is not None:
            try:
                observed = self._observation(row)
                saved = observed.get('retained')
                target = observed.get('target')
                # Before exchange the slot is only the new candidate, not a
                # retained old version. Do not present that as a backup.
                retained = (type(saved) is dict and saved.get('exists') is True
                    and (saved.get('identity') == row['old_identity']
                         or type(target) is dict and target.get('identity') == {
                             key: row['stage_identity'][key] for key in ('dev', 'ino')}
                         and saved.get('identity') != target.get('identity')))
                outcome = observed['state']
                if outcome == 'applied':
                    state, code = 'verified', None
                elif outcome == 'conflict':
                    state, code = 'conflict', 'file_changed'
                elif outcome == 'not_applied':
                    code = apply_error or 'stage_unapplied'
                else:
                    code = apply_error or 'observation_unknown'
            except (ValueError, OSError) as error:
                code = getattr(error, 'code', 'observation_unknown')
        elif code in ('file_changed', 'scope_changed', 'unsafe_file'):
            state = 'conflict'
        code = code if code is None or code in ERROR_CODES else 'io_failed'
        with self.publisher._db(write=True) as db:
            current = self._row(row['id'], db)
            if current['state'] != 'publishing':
                return
            if state == 'verified':
                changed = db.execute('UPDATE markdown_sync_heads SET head_id=? WHERE publication_id=? AND head_id=?',
                    (row['id'], row['binding_id'], row['expected_head_id'])).rowcount
                if changed != 1:
                    state, code = 'conflict', 'head_changed'
            db.execute('UPDATE markdown_sync_operations SET state=?,error_code=?,retained=?,updated_at=? WHERE id=?',
                (state, code, int(retained), _stamp(self.clock()), row['id']))

    def confirm(self, identifier, client_id):
        with self.lock, _errors():
            self._check()
            identifier, client_id = _uuid(identifier), _uuid(client_id)
            previous = self.find_receipt(client_id)
            if previous['received']:
                if previous['update']['id'] != identifier:
                    raise MarkdownChatSyncError('operation_conflict')
                return previous
            with self._ownership() as owned:
                if not owned:
                    # The original preview stays confirmable. The client can
                    # read receipt/preview, then explicitly continue its same
                    # request; nothing is accepted or written while busy.
                    raise MarkdownChatSyncError('operation_conflict')
                return self._confirm_owned(identifier, client_id)

    def _confirm_owned(self, identifier, client_id):
        with self.lock, _errors():
            self._check()
            identifier, client_id = _uuid(identifier), _uuid(client_id)
            with self.publisher._db(write=True) as db:
                previous = db.execute('SELECT id FROM markdown_sync_operations WHERE client_id=?', (client_id,)).fetchone()
                if previous is not None:
                    if previous['id'] != identifier:
                        raise MarkdownChatSyncError('operation_conflict')
                    replay = True
                else:
                    replay = False
                    row = self._row(identifier, db)
                    if row['state'] != 'prepared' or row['client_id'] is not None:
                        raise MarkdownChatSyncError('operation_conflict')
                    code, state = None, 'publishing'
                    if self.clock() > row['expires_at']:
                        code, state = 'preview_expired', 'blocked'
                    elif self._head(db, row['binding_id']) != row['expected_head_id']:
                        code, state = 'head_changed', 'conflict'
                    elif db.execute("SELECT 1 FROM markdown_sync_operations WHERE binding_id=? AND state='publishing'",
                                    (row['binding_id'],)).fetchone():
                        code, state = 'operation_conflict', 'conflict'
                    db.execute('UPDATE markdown_sync_operations SET client_id=?,state=?,error_code=?,updated_at=? WHERE id=?',
                        (client_id, state, code, _stamp(self.clock()), identifier))
            if replay:
                return self.find_receipt(client_id)
            row = self._row(identifier)
            if row['state'] == 'publishing':
                failure = None
                try:
                    self.files.apply(row['target_path'], row['payload'].decode('utf-8'), row['id'],
                        row['scope'], row['old_identity'], row['previous_source']['sha256'],
                        row['previous_source']['size_bytes'], lambda stage: self._record_stage(identifier, stage))
                except Exception as error:
                    failure = getattr(error, 'code', 'io_failed')
                self._settle(self._row(identifier), apply_error=failure)
            return self.find_receipt(client_id)

    def recover_accepted(self):
        with self.lock, _errors(), self.publisher._db() as db:
            self._check()
            identifiers = [row['id'] for row in db.execute("SELECT id FROM markdown_sync_operations WHERE state='publishing' ORDER BY created_at,id")]
        counts = dict(verified=0, conflict=0, blocked=0, publishing=0)
        with self.lock, _errors():
            with self._ownership() as owned:
                for identifier in identifiers:
                    if owned:
                        self._settle(self._row(identifier))
                    counts[self._row(identifier)['state']] += 1
        return counts

    def state_counts(self):
        with self.lock, _errors(), self.publisher._db() as db:
            self._check()
            return {state: db.execute('SELECT COUNT(*) FROM markdown_sync_operations WHERE state=?', (state,)).fetchone()[0]
                    for state in ('prepared', 'publishing', 'verified', 'conflict', 'blocked')}
