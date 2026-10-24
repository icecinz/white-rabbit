"""Application metadata fences for Hermes' non-atomic native image staging.

This module performs no RPC, image access or process cleanup. The caller must
persist each started phase before the corresponding external operation. Queue
counts or matching message contents never identify a request or settle a lost
ACK. In particular, prompt_started cannot be discarded as an unsent attachment.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import unicodedata
from urllib.parse import quote
import uuid


SCHEMA_VERSION = 1
MAX_REVISION = 2 ** 53 - 1
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
TERMINAL_PHASES = frozenset(('consumed', 'discarded'))
PHASES = frozenset(('prepared', 'attaching', 'staged', 'prompt_started',
                    'prompt_rejected', 'consumed', 'discarding', 'discarded'))
_IDENTITY_FIELDS = {'session_id', 'session_key', 'cwd', 'profile_home'}
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_EPOCH = re.compile(r'[0-9a-f]{32}\Z')
_SESSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_TABLE = 'hermes_attachment_transactions'
_INDEX = 'hermes_attachment_one_active'
_ERRORS = {
    'invalid_request': 'Hermes附件事务参数无效。',
    'attachment_conflict': 'Hermes附件事务身份或内容不一致。',
    'attachment_busy': '这段Hermes对话还有未完成的附件事务。',
    'attachment_not_found': '找不到这次Hermes附件事务。',
    'attachment_transition': 'Hermes附件事务不能按此顺序推进。',
    'attachment_terminal': 'Hermes附件事务已结束，不能重开。',
    'attachment_owner': '原附件进程所有者不匹配，未放弃附件。',
    'cleanup_unverified': '原附件进程退出尚未完整确认。',
    'invalid_record': 'Hermes附件事务记录无法核验。',
    'scope_changed': 'Hermes附件记录库身份变化。',
    'io_failed': 'Hermes附件事务暂时无法读取或保存。',
}
_DDL = (
    '''CREATE TABLE hermes_attachment_transactions (
        workspace_id TEXT NOT NULL, client_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>0), identity TEXT NOT NULL,
        owner_epoch TEXT NOT NULL, raw_request_sha256 TEXT NOT NULL, request_sha256 TEXT NOT NULL,
        images TEXT NOT NULL, phase TEXT NOT NULL
        CHECK(phase IN ('prepared','attaching','staged','prompt_started','prompt_rejected','consumed','discarding','discarded')),
        confirmed_count INTEGER NOT NULL CHECK(confirmed_count>=0 AND confirmed_count<=4),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(workspace_id,client_id))''',
    '''CREATE UNIQUE INDEX hermes_attachment_one_active
        ON hermes_attachment_transactions(workspace_id,chat_id)
        WHERE phase NOT IN ('consumed','discarded')''',
)


class HermesAttachmentError(ValueError):
    def __init__(self, code):
        self.code = code if code in _ERRORS else 'io_failed'
        super().__init__(_ERRORS[self.code])


def _fail(code):
    raise HermesAttachmentError(code)


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            _fail('invalid_request')
    except (ValueError, AttributeError):
        _fail('invalid_request')
    return value


def _hash(value, pattern=_SHA):
    if type(value) is not str or not pattern.fullmatch(value):
        _fail('invalid_request')
    return value


def _path(value):
    if (type(value) is not str or not 1 <= len(value) <= 4096
            or any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs') for char in value)
            or not os.path.isabs(value) or os.path.abspath(value) != value
            or str(Path(value)) != value or '..' in Path(value).parts):
        _fail('invalid_request')
    return value


def _identity(value):
    if type(value) is not dict or set(value) != _IDENTITY_FIELDS:
        _fail('invalid_request')
    if any(type(value[key]) is not str or not _SESSION.fullmatch(value[key])
           for key in ('session_id', 'session_key')):
        _fail('invalid_request')
    return {key: _path(value[key]) if key in ('cwd', 'profile_home') else value[key]
            for key in _IDENTITY_FIELDS}


def _images(value, *, identity, workspace_id, chat_id):
    if type(value) is not list or not 1 <= len(value) <= MAX_IMAGES:
        _fail('invalid_request')
    result, total = [], 0
    root = Path(identity['profile_home']) / 'images/rabbit-native' / workspace_id / chat_id / 'images'
    for image in value:
        if type(image) is not dict or set(image) != {'path', 'sha256', 'size_bytes', 'mime'}:
            _fail('invalid_request')
        digest = _hash(image['sha256'])
        size, mime = image['size_bytes'], image['mime']
        if (type(size) is not int or not 0 < size <= MAX_IMAGE_BYTES
                or type(mime) is not str or mime not in ('image/png', 'image/jpeg')):
            _fail('invalid_request')
        expected = str(root / (digest + ('.png' if mime == 'image/png' else '.jpg')))
        if _path(image['path']) != expected:
            _fail('invalid_request')
        total += size
        result.append(dict(image))
    if total > MAX_TOTAL_IMAGE_BYTES:
        _fail('invalid_request')
    return result


def _revision(value):
    if type(value) is not int or not 1 <= value <= MAX_REVISION:
        _fail('invalid_request')
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _decode(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                _fail('invalid_record')
            result[key] = item
        return result
    def nonfinite(_value):
        _fail('invalid_record')
    return json.loads(value, object_pairs_hook=pairs, parse_constant=nonfinite)


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _timestamp(value):
    if type(value) is not str or not 1 <= len(value) <= 64:
        _fail('invalid_record')
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            _fail('invalid_record')
    except ValueError:
        _fail('invalid_record')
    return value


def _schema(db):
    return tuple(tuple(row) for row in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name IN (?,?) ORDER BY type,name',
        (_TABLE, _INDEX)))


def _expected_schema():
    with sqlite3.connect(':memory:') as db:
        for statement in _DDL:
            db.execute(statement)
        return _schema(db)


_EXPECTED_SCHEMA = _expected_schema()


class HermesAttachmentTransactions:
    def __init__(self, path, *, workspace_id):
        self.workspace_id = _hash(workspace_id)
        try:
            self.path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
        except (TypeError, ValueError):
            _fail('invalid_request')
        _path(str(self.path))
        if (Path.home() / 'Library/Mobile Documents' in self.path.parents
                or any(parent.is_symlink() for parent in self.path.parents)):
            _fail('invalid_request')
        self._file_identity = self._observe_path()
        with self._db(write=True, initialize=True):
            pass

    def _observe_path(self):
        try:
            info = self.path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or any(parent.is_symlink() for parent in self.path.parents)):
                _fail('scope_changed')
            return info.st_dev, info.st_ino
        except OSError:
            _fail('scope_changed')

    @contextmanager
    def _db(self, *, write=False, initialize=False):
        db = None
        try:
            if self._observe_path() != self._file_identity:
                _fail('scope_changed')
            db = sqlite3.connect('file:' + quote(str(self.path), safe='/') +
                ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=15, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA trusted_schema=OFF')
            if not write:
                db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            if initialize and not _schema(db):
                for statement in _DDL:
                    db.execute(statement)
            if _schema(db) != _EXPECTED_SCHEMA:
                _fail('invalid_record')
            yield db
            if self._observe_path() != self._file_identity:
                _fail('scope_changed')
            if write:
                db.commit()
        except HermesAttachmentError:
            raise
        except sqlite3.IntegrityError:
            _fail('attachment_conflict')
        except (sqlite3.Error, OSError):
            _fail('io_failed')
        finally:
            if db is not None:
                if db.in_transaction:
                    db.rollback()
                db.close()

    def _row(self, db, client_id, *, chat_id=None):
        row = db.execute('SELECT * FROM hermes_attachment_transactions '
            'WHERE workspace_id=? AND client_id=?', (self.workspace_id, client_id)).fetchone()
        if row is None:
            return None
        try:
            record = dict(row)
            if (set(record) != {'workspace_id', 'chat_id', 'client_id', 'revision', 'identity',
                    'owner_epoch', 'raw_request_sha256', 'request_sha256', 'images', 'phase',
                    'confirmed_count', 'created_at', 'updated_at'}
                    or record['workspace_id'] != self.workspace_id
                    or record['client_id'] != client_id):
                _fail('invalid_record')
            _uuid(record['chat_id']); _uuid(record['client_id']); _revision(record['revision'])
            _hash(record['owner_epoch'], _EPOCH)
            _hash(record['raw_request_sha256']); _hash(record['request_sha256'])
            if (type(record['identity']) is not str or len(record['identity']) > 16384
                    or type(record['images']) is not str or len(record['images']) > 32768):
                _fail('invalid_record')
            identity = _identity(_decode(record['identity']))
            images = _images(_decode(record['images']), identity=identity,
                workspace_id=self.workspace_id, chat_id=record['chat_id'])
            count, phase = record['confirmed_count'], record['phase']
            if (type(count) is not int or not 0 <= count <= len(images)
                    or type(phase) is not str or phase not in PHASES
                    or phase in ('prepared', 'attaching') and count >= len(images)
                    or phase in ('staged', 'prompt_started', 'prompt_rejected', 'consumed') and count != len(images)):
                _fail('invalid_record')
            _timestamp(record['created_at']); _timestamp(record['updated_at'])
            record.update(identity=identity, images=images, schema_version=SCHEMA_VERSION)
        except (HermesAttachmentError, ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            _fail('invalid_record')
        if chat_id is not None and record['chat_id'] != chat_id:
            _fail('attachment_conflict')
        return record

    def read(self, chat_id, client_id):
        chat_id, client_id = _uuid(chat_id), _uuid(client_id)
        with self._db() as db:
            return self._row(db, client_id, chat_id=chat_id)

    def _active(self, db, chat_id):
        rows = db.execute('SELECT client_id FROM hermes_attachment_transactions '
            "WHERE workspace_id=? AND chat_id=? AND phase NOT IN ('consumed','discarded') LIMIT 2",
            (self.workspace_id, chat_id)).fetchall()
        if len(rows) > 1:
            _fail('invalid_record')
        return self._row(db, rows[0]['client_id'], chat_id=chat_id) if rows else None

    def active(self, chat_id):
        with self._db() as db:
            return self._active(db, _uuid(chat_id))

    def known_images(self, chat_id, paths):
        """Resolve exact owned image references using metadata only.

        A native text-routed image can remain an explicit path in its history.
        Only a prompt-started/consumed transaction in this workspace and chat
        proves that path's source. This does not prove prompt acceptance, file
        availability or bytes; the separate image reader must verify those.
        """
        chat_id = _uuid(chat_id)
        if type(paths) is not list or not 1 <= len(paths) <= MAX_IMAGES:
            _fail('invalid_request')
        paths = [_path(path) for path in paths]
        result = []
        with self._db() as db:
            for path in paths:
                try:
                    match = db.execute('SELECT t.client_id FROM hermes_attachment_transactions t '
                        "WHERE t.workspace_id=? AND t.chat_id=? AND t.phase IN ('prompt_started','consumed') "
                        'AND EXISTS(SELECT 1 FROM json_each(t.images) image '
                        "WHERE json_extract(image.value,'$.path')=?) "
                        'ORDER BY t.created_at DESC,t.client_id DESC LIMIT 1',
                        (self.workspace_id, chat_id, path)).fetchone()
                except sqlite3.Error:
                    _fail('invalid_record')
                if match is None:
                    return None
                row = self._row(db, match['client_id'], chat_id=chat_id)
                if row is None or row['phase'] not in ('prompt_started', 'consumed'):
                    _fail('invalid_record')
                images = [image for image in row['images'] if image['path'] == path]
                if not images or any(image != images[0] for image in images):
                    _fail('invalid_record')
                result.append(dict(images[0]))
        return result

    def begin(self, chat_id, client_id, *, identity, owner_epoch, raw_request_sha256,
              request_sha256, images):
        chat_id, client_id = _uuid(chat_id), _uuid(client_id)
        identity, owner_epoch = _identity(identity), _hash(owner_epoch, _EPOCH)
        raw_request_sha256, request_sha256 = _hash(raw_request_sha256), _hash(request_sha256)
        images = _images(images, identity=identity, workspace_id=self.workspace_id, chat_id=chat_id)
        expected = {'identity': identity, 'owner_epoch': owner_epoch,
            'raw_request_sha256': raw_request_sha256, 'request_sha256': request_sha256, 'images': images}
        with self._db(write=True) as db:
            old = self._row(db, client_id, chat_id=chat_id)
            if old is not None:
                if any(old[key] != value for key, value in expected.items()):
                    _fail('attachment_conflict')
                return dict(old, created=False)
            if self._active(db, chat_id) is not None:
                _fail('attachment_busy')
            stamp = _stamp()
            db.execute('INSERT INTO hermes_attachment_transactions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (self.workspace_id, client_id, chat_id, 1, _json(identity), owner_epoch,
                 raw_request_sha256, request_sha256, _json(images), 'prepared', 0, stamp, stamp))
            return dict(self._row(db, client_id, chat_id=chat_id), created=True)

    @staticmethod
    def _check_revision(old, revision):
        if old is None:
            _fail('attachment_not_found')
        if old['revision'] != revision:
            _fail('attachment_conflict')
        if old['phase'] in TERMINAL_PHASES:
            _fail('attachment_terminal')
        if revision == MAX_REVISION:
            _fail('attachment_conflict')

    def _write(self, db, old, phase, count=None):
        db.execute('UPDATE hermes_attachment_transactions '
            'SET revision=?,phase=?,confirmed_count=?,updated_at=? '
            'WHERE workspace_id=? AND client_id=? AND revision=?',
            (old['revision'] + 1, phase, old['confirmed_count'] if count is None else count,
             _stamp(), self.workspace_id, old['client_id'], old['revision']))
        return self._row(db, old['client_id'], chat_id=old['chat_id'])

    def _advance(self, chat_id, client_id, revision, sources, target, *, index=None):
        chat_id, client_id, revision = _uuid(chat_id), _uuid(client_id), _revision(revision)
        if index is not None and (type(index) is not int or not 0 <= index < MAX_IMAGES):
            _fail('invalid_request')
        with self._db(write=True) as db:
            old = self._row(db, client_id, chat_id=chat_id)
            self._check_revision(old, revision)
            if old['phase'] not in sources or index is not None and old['confirmed_count'] != index:
                _fail('attachment_transition')
            count = old['confirmed_count'] + 1 if target == 'confirm_attach' else old['confirmed_count']
            phase = ('staged' if count == len(old['images']) else 'prepared') if target == 'confirm_attach' else target
            return self._write(db, old, phase, count)

    def mark_attach_started(self, chat_id, client_id, revision, index):
        if type(index) is not int or not 0 <= index < MAX_IMAGES:
            _fail('invalid_request')
        return self._advance(chat_id, client_id, revision, ('prepared',), 'attaching', index=index)

    def confirm_attach(self, chat_id, client_id, revision, index):
        if type(index) is not int or not 0 <= index < MAX_IMAGES:
            _fail('invalid_request')
        return self._advance(chat_id, client_id, revision, ('attaching',), 'confirm_attach', index=index)

    def mark_prompt_started(self, chat_id, client_id, revision):
        return self._advance(chat_id, client_id, revision, ('staged',), 'prompt_started')

    def mark_prompt_rejected(self, chat_id, client_id, revision):
        return self._advance(chat_id, client_id, revision, ('prompt_started',), 'prompt_rejected')

    def mark_consumed(self, chat_id, client_id, revision):
        return self._advance(chat_id, client_id, revision, ('prompt_started',), 'consumed')

    def begin_discard(self, chat_id, client_id, revision, owner_epoch):
        chat_id, client_id = _uuid(chat_id), _uuid(client_id)
        revision, owner_epoch = _revision(revision), _hash(owner_epoch, _EPOCH)
        with self._db(write=True) as db:
            old = self._row(db, client_id, chat_id=chat_id)
            if old is None:
                _fail('attachment_not_found')
            if old['owner_epoch'] != owner_epoch:
                _fail('attachment_owner')
            # A repeated close attempt may read the current fence, never replay
            # image RPCs or replace the original process owner.
            if old['phase'] == 'discarding':
                if revision > old['revision']:
                    _fail('attachment_conflict')
                return old
            self._check_revision(old, revision)
            if old['phase'] not in ('prepared', 'attaching', 'staged', 'prompt_rejected'):
                _fail('attachment_transition')
            return self._write(db, old, 'discarding')

    def finish_discard(self, chat_id, client_id, revision, owner_epoch, proof):
        chat_id, client_id = _uuid(chat_id), _uuid(client_id)
        revision, owner_epoch = _revision(revision), _hash(owner_epoch, _EPOCH)
        fields = {'process_exited', 'io_threads_stopped', 'owned_group_empty', 'reader_stopped'}
        if type(proof) is not dict or set(proof) != fields or any(proof[key] is not True for key in fields):
            _fail('cleanup_unverified')
        with self._db(write=True) as db:
            old = self._row(db, client_id, chat_id=chat_id)
            self._check_revision(old, revision)
            if old['owner_epoch'] != owner_epoch:
                _fail('attachment_owner')
            if old['phase'] != 'discarding':
                _fail('attachment_transition')
            return self._write(db, old, 'discarded')
