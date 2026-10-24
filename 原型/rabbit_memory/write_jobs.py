"""Private transactional memory-write outbox, without network or inference.

Enqueue inside the same transaction that completes a run. Only that run's new
user text is retained; assistant replies, imported history, images and recalled
facts never enter the payload. The operation, payload, bank binding and
connection fingerprint remain frozen for the lifetime of the job.

An unknown acknowledgement is not a new submission opportunity: unknown jobs
can only be claimed for polling their original operation. Recovery releases a
claim into unknown, never back into a queue that blindly resends retained text.
Only the explicit public_state projection may be returned to a browser.
"""
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import quote
import uuid

from .hindsight_memory import build_retain_payload, payload_digest, MAX_REQUEST_BYTES
from .memory_profiles import make_memory_snapshot, validate_memory_snapshot, MAX_REVISION


STATES = frozenset(('queued', 'submitting', 'processing', 'completed', 'failed', 'unknown'))
TERMINAL_STATES = frozenset(('completed', 'failed'))
MAX_SNAPSHOT_BYTES = 64 * 1024
MAX_PROFILE_BYTES = 64 * 1024
ERROR_CODES = frozenset((
    'memory_service_unavailable', 'memory_service_rejected', 'memory_not_found',
    'memory_response_invalid', 'memory_write_unknown', 'memory_payload_changed',
    'memory_connection_changed', 'memory_write_failed', 'memory_write_interrupted',
    'memory_job_claim_lost', 'memory_bank_identity_changed',
    'memory_write_protocol_unverified', 'memory_read_only', 'memory_invalid_input',
    'memory_capacity',
))


def initialize_schema(db):
    """Add only the outbox table; the caller owns commit/rollback."""
    db.execute('''CREATE TABLE IF NOT EXISTS memory_write_jobs (
        run_id TEXT PRIMARY KEY REFERENCES runs(id),
        operation_id TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL UNIQUE,
        profile_json TEXT NOT NULL, payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL, connection_fingerprint TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('queued','submitting','processing','completed','failed','unknown')),
        revision INTEGER NOT NULL CHECK(revision>0),
        claim_token TEXT, claim_action TEXT CHECK(claim_action IN ('submit','poll')),
        error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK((claim_token IS NULL AND claim_action IS NULL) OR
              (claim_token IS NOT NULL AND claim_action IS NOT NULL)))''')


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError('记忆任务编号需要标准 UUID。')
    try:
        canonical = str(uuid.UUID(value))
    except ValueError:
        raise ValueError('记忆任务编号需要标准 UUID。') from None
    if canonical != value:
        raise ValueError('记忆任务编号需要标准小写 UUID。')
    return value


def _revision(value):
    if type(value) is not int or not 1 <= value <= MAX_REVISION:
        raise ValueError('记忆任务版本需要有效整数。')
    return value


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('记忆任务的连接或内容指纹无效。')
    return value


def _error_code(value):
    if value is not None and (not isinstance(value, str) or value not in ERROR_CODES):
        raise ValueError('记忆任务错误需要受限错误码。')
    return value


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _bounded_object(raw, maximum, label):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > maximum:
        raise ValueError(label + '无效或超过容量。')
    try:
        value = json.loads(raw)
    except ValueError:
        raise ValueError(label + '无法核验。') from None
    if not isinstance(value, dict):
        raise ValueError(label + '结构无法核验。')
    return value


def _one(db, sql, parameters=()):
    """Work with both the caller's sqlite.Row and a minimal tuple fixture."""
    cursor = db.execute(sql, parameters)
    row = cursor.fetchone()
    return None if row is None else dict(zip((column[0] for column in cursor.description), row))


def _private_profile(snapshot):
    memory = validate_memory_snapshot(snapshot)
    if memory['id'] == 'none':
        return memory
    return dict(memory, bank_id='rabbit-' + memory['id'])


def _job(row):
    if row is None:
        return None
    value = dict(row)
    _uuid(value['run_id'])
    _uuid(value['operation_id'])
    _revision(value['revision'])
    _digest(value['connection_fingerprint'])
    _digest(value['payload_sha256'])
    if value['document_id'] != 'rabbit-turn-' + value['run_id'] or value['state'] not in STATES:
        raise ValueError('记忆任务的固定身份或状态无效。')
    _error_code(value['error_code'])
    if value['claim_token'] is not None:
        _uuid(value['claim_token'])
        if value['claim_action'] not in ('submit', 'poll') or value['state'] in TERMINAL_STATES:
            raise ValueError('记忆任务的领取状态无效。')
    elif value['claim_action'] is not None:
        raise ValueError('记忆任务的领取状态无效。')
    profile = _bounded_object(value.pop('profile_json'), MAX_PROFILE_BYTES, '记忆任务配置')
    snapshot = make_memory_snapshot(profile)
    if snapshot['id'] == 'none' or snapshot['mode'] != 'read_write' or profile != _private_profile(snapshot):
        raise ValueError('记忆任务不能更换固定保存范围。')
    payload = _bounded_object(value.pop('payload_json'), MAX_REQUEST_BYTES, '记忆保存内容')
    try:
        content = payload['items'][0]['content']
    except (KeyError, IndexError, TypeError):
        raise ValueError('记忆保存内容结构无效。') from None
    canonical = build_retain_payload(profile, operation_id=value['operation_id'],
                                     document_id=value['document_id'], content=content)
    if payload != canonical or payload_digest(payload) != value['payload_sha256']:
        raise ValueError('记忆保存内容与原任务指纹不一致，没有重写原任务。')
    value['profile'], value['payload'] = profile, payload
    return value


def public_state(job):
    """No raw text, profile, namespace, endpoint, fingerprint or claim token."""
    if job is None:
        return None
    if (not isinstance(job, dict) or not isinstance(job.get('state'), str)
            or job['state'] not in STATES):
        raise ValueError('记忆任务公开状态无效。')
    return {'state': job['state'], 'operation_id': _uuid(job.get('operation_id')),
            'error_code': _error_code(job.get('error_code'))}


def enqueue_completed_in(db, run_id, *, connection_fingerprint, request_snapshot_validator):
    """Freeze one eligible new-user payload within completion's transaction.

    A persisted job is the receipt: return it before reconsidering a later run,
    profile, source text, or connection change. This function never initializes
    schema, commits the caller transaction, or contacts a service.
    """
    _uuid(run_id)
    if not db.in_transaction:
        raise ValueError('记忆任务必须与回答完成一起保存到事务中。')
    previous = _one(db, 'SELECT * FROM memory_write_jobs WHERE run_id=?', (run_id,))
    if previous is not None:
        return _job(previous)
    run = _one(db, 'SELECT * FROM runs WHERE id=?', (run_id,))
    if run is None:
        raise KeyError('找不到这次回答。')
    if run['status'] != 'completed' or run.get('request_snapshot') is None:
        return None
    snapshot = request_snapshot_validator(
        _bounded_object(run['request_snapshot'], MAX_SNAPSHOT_BYTES, '本轮配置快照'))
    if snapshot['schema_version'] != 2:
        return None  # Legacy/non-memory requests must not produce new writes.
    profile = _private_profile(snapshot['memory_profile'])
    if profile['id'] == 'none' or profile['mode'] != 'read_write' or run.get('user_message_id') is None:
        return None
    if not isinstance(run['user_message_id'], str) or not run['user_message_id']:
        raise ValueError('本轮新用户消息的固定身份无效。')
    user = _one(db, 'SELECT chat_id,role,status,content FROM messages WHERE id=?', (run['user_message_id'],))
    if user is None or user['chat_id'] != run['chat_id'] or user['role'] != 'user' or user['status'] != 'completed':
        raise ValueError('记忆保存只能使用本轮同一聊天中已完成的新用户消息。')
    if isinstance(user['content'], str) and not user['content'].strip():
        return None  # No image/assistant/history surrogate for an empty user turn.
    fingerprint = _digest(connection_fingerprint)
    operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'small-rabbit-memory-write:' + run_id))
    document_id = 'rabbit-turn-' + run_id
    payload = build_retain_payload(profile, operation_id=operation_id,
                                   document_id=document_id, content=user['content'])
    payload_json, profile_json = _canonical_json(payload), _canonical_json(profile)
    if len(payload_json.encode('utf-8')) > MAX_REQUEST_BYTES or len(profile_json.encode('utf-8')) > MAX_PROFILE_BYTES:
        raise ValueError('记忆保存任务超过容量，没有入队。')
    stamp = _now()
    db.execute('''INSERT INTO memory_write_jobs(run_id,operation_id,document_id,profile_json,payload_json,
        payload_sha256,connection_fingerprint,state,revision,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,'queued',1,?,?)''',
        (run_id, operation_id, document_id, profile_json, payload_json, payload_digest(payload), fingerprint, stamp, stamp))
    return _job(_one(db, 'SELECT * FROM memory_write_jobs WHERE run_id=?', (run_id,)))


class MemoryJobConflict(ValueError):
    def __init__(self, job):
        super().__init__('记忆任务已由另一处理步骤更新，原任务和内容保留。')
        self.job_state = public_state(job)


class MemoryWriteJobs:
    public_state = staticmethod(public_state)

    def __init__(self, path):
        self.path = Path(os.path.abspath(os.path.expanduser(str(path))))
        self._check_path()
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._open(create=True) as db:
            db.execute('BEGIN IMMEDIATE')
            initialize_schema(db)
        os.chmod(self.path, 0o600)

    def _check_path(self):
        cloud = Path.home() / 'Library' / 'Mobile Documents'
        if self.path == cloud or cloud in self.path.parents:
            raise ValueError('记忆任务需要保存在本机聊天库，不能放进 iCloud 项目目录。')
        if '..' in self.path.parts or self.path.is_symlink():
            raise ValueError('记忆任务数据库路径无效。')
        if any(parent.is_symlink() for parent in self.path.parents):
            raise ValueError('记忆任务数据库目录不能是符号链接。')
        if self.path.exists() and not self.path.is_file():
            raise ValueError('记忆任务数据库需要普通文件。')

    @contextmanager
    def _open(self, *, readonly=False, create=False):
        self._check_path()
        mode = 'ro' if readonly else ('rwc' if create else 'rw')
        db = sqlite3.connect('file:' + quote(str(self.path), safe='/') + '?mode=' + mode,
                             uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            if readonly:
                db.execute('PRAGMA query_only=ON')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _get_in(db, run_id):
        value = _one(db, 'SELECT * FROM memory_write_jobs WHERE run_id=?', (run_id,))
        if value is None:
            raise KeyError('找不到这项记忆保存任务。')
        return _job(value)

    def get(self, run_id):
        _uuid(run_id)
        with self.lock, self._open(readonly=True) as db:
            return self._get_in(db, run_id)

    def list(self):
        """Private nonterminal jobs only; unknown remains visible for polling."""
        with self.lock, self._open(readonly=True) as db:
            rows = db.execute("SELECT * FROM memory_write_jobs WHERE state NOT IN ('completed','failed') "
                              'ORDER BY created_at,run_id')
            return {'jobs': [_job(dict(row)) for row in rows]}

    def claim(self, run_id, revision, *, for_poll=False):
        _uuid(run_id)
        _revision(revision)
        if type(for_poll) is not bool:
            raise ValueError('记忆任务领取方式需要明确选择。')
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._get_in(db, run_id)
            allowed = ('processing', 'unknown') if for_poll else ('queued',)
            if job['revision'] != revision or job['claim_token'] is not None or job['state'] not in allowed:
                return None
            if revision == MAX_REVISION:
                raise ValueError('这项记忆任务已达到版本上限。')
            token, action = str(uuid.uuid4()), 'poll' if for_poll else 'submit'
            state = job['state'] if for_poll else 'submitting'
            db.execute('''UPDATE memory_write_jobs SET revision=revision+1,claim_token=?,claim_action=?,
                state=?,updated_at=? WHERE run_id=? AND revision=? AND claim_token IS NULL''',
                (token, action, state, _now(), run_id, revision))
            return self._get_in(db, run_id)

    def update(self, run_id, token, *, state, error_code=None):
        _uuid(run_id)
        _uuid(token)
        _error_code(error_code)
        if not isinstance(state, str) or state not in ('processing', 'completed', 'failed', 'unknown'):
            raise ValueError('记忆任务只能记录已核验的处理状态，不能盲目重发。')
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._get_in(db, run_id)
            if job['claim_token'] != token or job['state'] not in ('submitting', 'processing', 'unknown'):
                raise MemoryJobConflict(job)
            if job['revision'] == MAX_REVISION:
                raise ValueError('这项记忆任务已达到版本上限。')
            db.execute('''UPDATE memory_write_jobs SET revision=revision+1,state=?,error_code=?,
                claim_token=NULL,claim_action=NULL,updated_at=? WHERE run_id=? AND claim_token=?''',
                (state, error_code, _now(), run_id, token))
            return self._get_in(db, run_id)

    def recover_claim(self, run_id, revision, token, *, error_code='memory_write_interrupted'):
        """Explicit dead-worker recovery; caller must establish worker absence."""
        _uuid(run_id)
        _revision(revision)
        _uuid(token)
        _error_code(error_code)
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._get_in(db, run_id)
            if job['revision'] != revision or job['claim_token'] != token:
                raise MemoryJobConflict(job)
            if job['state'] not in ('submitting', 'processing', 'unknown'):
                raise MemoryJobConflict(job)
            if revision == MAX_REVISION:
                raise ValueError('这项记忆任务已达到版本上限。')
            db.execute('''UPDATE memory_write_jobs SET revision=revision+1,state='unknown',error_code=?,
                claim_token=NULL,claim_action=NULL,updated_at=? WHERE run_id=? AND revision=? AND claim_token=?''',
                (error_code, _now(), run_id, revision, token))
            return self._get_in(db, run_id)

    def revision_for_chat(self, chat_id):
        with self.lock, self._open(readonly=True) as db:
            rows = [tuple(row) for row in db.execute('''SELECT j.run_id,j.state,j.revision FROM memory_write_jobs j
                JOIN runs r ON r.id=j.run_id WHERE r.chat_id=? ORDER BY j.run_id''', (chat_id,))]
        return hashlib.sha256(json.dumps(rows).encode()).hexdigest()
