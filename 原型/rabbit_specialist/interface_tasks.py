"""Small reported workflow ledger for one desktop task; no executor or retry.

Native Codex receipts remain authoritative for Codex submission. These rows
describe reported application steps, not native transcripts or proof of an
external send. Broad CUA tools do not enforce these checkpoints or a target
permission. Request policy is frozen data, not observed model telemetry.
``human_confirmed`` is a trusted caller assertion: this ledger cannot identify
a human. AI-facing MCP must not expose ``close_unknown``. ``not_sent`` also needs
independent backend-held human confirmation; an AI source label is not proof.
Closing a task preserves its unknown outcome. It proves neither native/tool
shutdown nor permission to execute on the desktop; a coordinator must separately
check unfinished native operations, approvals and uncertain background work.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
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

from rabbit_runtime.native_json import canonical_metadata, NativeJSONError
from rabbit_preparation.material_references import (validate_material_refs as validate_refs,
    MaterialReferenceError as NativeMaterialError)


MAX_REQUEST_BYTES = 256 * 1024
MAX_MESSAGE_BYTES = 128 * 1024
MAX_RESULT_BYTES = 256 * 1024
MAX_ANCHOR_BYTES = 8 * 1024
MAX_SUMMARY_BYTES = 32 * 1024
MAX_TASKS = 128
MAX_OPERATIONS = 4096
MAX_REVISION = 2**53 - 1
PHASES = frozenset(('prepared', 'dispatch_started', 'send_started', 'sent',
    'needs_review', 'completed', 'not_sent', 'closed_unknown'))
TERMINAL = frozenset(('completed', 'not_sent', 'closed_unknown'))
SEMANTICS = {'workflow': 'reported', 'cua_checkpoints_enforced': False,
    'target_is_permission': False, 'policy_is_model_telemetry': False}
CLOSURE_SEMANTICS = {'outcome_verified': False, 'native_stop_proven': False,
    'desktop_execution_authorized': False}
_SHA = re.compile(r'[a-f0-9]{64}\Z')
_DESKTOP = re.compile(r'[A-Za-z0-9_.:/-]{1,160}\Z')
_RECEIPT_STATUSES = frozenset(('submitted_unknown', 'accepted', 'completed', 'failed', 'interrupted', 'rejected'))
_DDL = (
    '''CREATE TABLE interface_tasks (
        workspace_id TEXT NOT NULL, task_id TEXT NOT NULL, desktop_id TEXT NOT NULL,
        request_sha256 TEXT NOT NULL, request_json TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>0), phase TEXT NOT NULL,
        dispatch_started INTEGER NOT NULL CHECK(dispatch_started IN (0,1)),
        send_started INTEGER NOT NULL CHECK(send_started IN (0,1)),
        native_client_id TEXT NOT NULL, state_json TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(workspace_id,task_id))''',
    '''CREATE TABLE interface_task_operations (
        workspace_id TEXT NOT NULL, operation_id TEXT NOT NULL,
        operation_sha256 TEXT NOT NULL, task_id TEXT NOT NULL, ack_json TEXT NOT NULL,
        PRIMARY KEY(workspace_id,operation_id))''',
    '''CREATE UNIQUE INDEX interface_one_active_desktop ON interface_tasks(desktop_id)
        WHERE phase NOT IN ('completed','not_sent','closed_unknown')''',
)


class InterfaceTaskError(ValueError):
    def __init__(self, code):
        self.code = code if code in ('invalid_arguments', 'not_found', 'operation_conflict',
            'revision_conflict', 'state_conflict', 'desktop_busy', 'capacity',
            'invalid_record', 'scope_changed', 'closed') else 'invalid_record'
        super().__init__('Interface task: ' + self.code)


def _fail(code):
    raise InterfaceTaskError(code)


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            _fail('invalid_arguments')
    except (ValueError, AttributeError):
        _fail('invalid_arguments')
    return value


def _sha(value):
    if type(value) is not str or _SHA.fullmatch(value) is None:
        _fail('invalid_arguments')
    return value


def _text(value, limit):
    try:
        if type(value) is not str or not value.strip() or len(value.encode('utf-8', 'strict')) > limit:
            _fail('invalid_arguments')
    except UnicodeError:
        _fail('invalid_arguments')
    return value


def _opaque(value):
    _text(value, 1024)
    if len(value) > 256 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        _fail('invalid_arguments')
    return value


def _json(value, limit):
    try:
        meta = canonical_metadata(value, max_bytes=limit, max_depth=32, max_nodes=10000, safe_integers=True)
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
        return raw, meta['sha256']
    except (NativeJSONError, UnicodeError, ValueError, TypeError, RecursionError):
        _fail('invalid_arguments')


def _object(value, limit=MAX_ANCHOR_BYTES):
    if type(value) is not dict or not value:
        _fail('invalid_arguments')
    return json.loads(_json(value, limit)[0])


def _fields(value, required, optional=()):
    if type(value) is not dict or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        _fail('invalid_arguments')
    return value


def _human_source(value):
    _fields(value, ('kind', 'note'))
    if value['kind'] != 'human_confirmed': _fail('state_conflict')
    return {'kind': 'human_confirmed', 'note': _text(value['note'], 1024)}


def _timestamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo != timezone.utc or stamp.isoformat(timespec='microseconds') != value: _fail('invalid_record')


def _request(workspace, task, value):
    _fields(value, ('desktop_id', 'target', 'message', 'material_refs', 'policy'))
    if type(value['desktop_id']) is not str or _DESKTOP.fullmatch(value['desktop_id']) is None:
        _fail('invalid_arguments')
    try:
        refs = validate_refs(value['material_refs'])
    except NativeMaterialError:
        _fail('invalid_arguments')
    result = {'schema_version': 1, 'workspace_id': workspace, 'task_id': task,
        'desktop_id': value['desktop_id'], 'target': _object(value['target']),
        'message': _text(value['message'], MAX_MESSAGE_BYTES), 'material_refs': refs,
        'policy': _object(value['policy'])}
    raw, digest = _json(result, MAX_REQUEST_BYTES)
    return json.loads(raw), raw, digest


def _initial_state():
    return dict.fromkeys(('native_receipt', 'native_intent_sha256', 'checkpoint', 'sent_anchor', 'review_reason', 'not_sent_source', 'result', 'closure'))


def _loaded(raw, limit):
    if type(raw) is not str or len(raw.encode('utf-8', 'strict')) > limit: _fail('invalid_record')
    value = json.loads(raw)
    if _json(value, limit)[0] != raw: _fail('invalid_record')
    return value


def _receipt(value, client):
    _fields(value, ('native_client_id', 'thread_id', 'turn_id', 'request_sha256', 'status'))
    if value['native_client_id'] != client or type(value['status']) is not str or value['status'] not in _RECEIPT_STATUSES:
        _fail('invalid_arguments')
    _sha(value['request_sha256'])
    for key in ('thread_id', 'turn_id'):
        if value[key] is not None:
            _opaque(value[key])
    if (value['status'] in ('accepted', 'completed', 'failed', 'interrupted')
            and (value['thread_id'] is None or value['turn_id'] is None)
            or value['status'] in ('submitted_unknown', 'rejected') and value['turn_id'] is not None):
        _fail('invalid_arguments')
    return deepcopy(value)


def _result(value):
    _fields(value, ('text', 'source_anchor'), ('summary',))
    text = _text(value['text'], MAX_RESULT_BYTES)
    result = {'text': text, 'source_anchor': _object(value['source_anchor']),
        'provenance': 'assistant_reported', 'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
    if 'summary' in value:
        result['summary'] = _text(value['summary'], MAX_SUMMARY_BYTES)
    return result


def _schema(db):
    return tuple(tuple(row) for row in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE tbl_name IN ('interface_tasks','interface_task_operations') ORDER BY type,name"))


def _expected_schema():
    db = sqlite3.connect(':memory:')
    try:
        for sql in _DDL: db.execute(sql)
        return _schema(db)
    finally: db.close()


_EXPECTED_SCHEMA = _expected_schema()


class InterfaceTasks:
    """Two local tables, atomic writes, and replayable small acknowledgements.

    Only a newly created file receives DDL. Existing files are verified without
    repairs, including when create_if_missing keeps its default True value.
    """
    def __init__(self, path, *, workspace_id, create_if_missing=True):
        if type(create_if_missing) is not bool: _fail('invalid_arguments')
        self.workspace_id = _sha(workspace_id)
        self.path = Path(path)
        self.lock, self._closed = threading.RLock(), False
        if (not self.path.is_absolute() or str(self.path) != os.path.abspath(self.path)
                or not self.path.parent.is_dir() or any(part.is_symlink() for part in (*self.path.parents, self.path))):
            _fail('invalid_arguments')
        created = False
        try:
            info = self.path.stat(follow_symlinks=False)
        except FileNotFoundError:
            if not create_if_missing: _fail('not_found')
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try: info = os.fstat(fd)
            finally: os.close(fd)
            created = True
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _fail('scope_changed')
        self._identity = info.st_dev, info.st_ino
        parent = self.path.parent.stat(); self._parent_identity = parent.st_dev, parent.st_ino
        if not created:
            # A read-only WAL connection must not manufacture missing shm or
            # recover sidecars while merely discovering old unfinished work.
            from rabbit_runtime.maintenance import sidecar_identities as _sidecars
            try: _sidecars(self.path)
            except (ValueError, OSError): _fail('scope_changed')
        with self._db(write=created, initial=True) as db:
            if created:
                for sql in _DDL: db.execute(sql)
            if _schema(db) != _EXPECTED_SCHEMA: _fail('invalid_record')

    def _check(self):
        if self._closed: _fail('closed')
        try:
            info = self.path.stat(follow_symlinks=False); parent = self.path.parent.stat(follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or any(parent.is_symlink() for parent in self.path.parents)
                    or (info.st_dev, info.st_ino) != self._identity
                    or (parent.st_dev, parent.st_ino) != self._parent_identity):
                _fail('scope_changed')
        except OSError:
            _fail('scope_changed')

    @contextmanager
    def _db(self, *, write=False, initial=False):
        with self.lock:
            self._check()
            db = sqlite3.connect('file:' + quote(str(self.path), safe='/') + ('?mode=rw' if write else '?mode=ro'),
                uri=True, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                db.execute('PRAGMA trusted_schema=OFF')
                if not write: db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                self._check()
                if not initial and _schema(db) != _EXPECTED_SCHEMA: _fail('invalid_record')
                yield db
                self._check(); db.commit()
            except BaseException:
                db.rollback(); raise
            finally: db.close()

    def _row(self, row):
        if row is None: return None
        try:
            task = _uuid(row['task_id'])
            request = _loaded(row['request_json'], MAX_REQUEST_BYTES)
            body = {key: request[key] for key in ('desktop_id', 'target', 'message', 'material_refs', 'policy')}
            checked, raw, digest = _request(self.workspace_id, task, body)
            if (request != checked or raw != row['request_json'] or digest != row['request_sha256']
                    or row['workspace_id'] != self.workspace_id or row['desktop_id'] != request['desktop_id']
                    or type(row['revision']) is not int or not 1 <= row['revision'] <= MAX_REVISION
                    or row['phase'] not in PHASES or row['dispatch_started'] not in (0, 1)
                    or row['send_started'] not in (0, 1) or row['send_started'] > row['dispatch_started']
                    or row['native_client_id'] != 'ui-task/' + task):
                _fail('invalid_record')
            state = _loaded(row['state_json'], MAX_RESULT_BYTES + MAX_SUMMARY_BYTES + 65536); _fields(state, _initial_state())
            if state['native_intent_sha256'] is not None:
                _sha(state['native_intent_sha256'])
                if not row['dispatch_started']: _fail('invalid_record')
            if state['native_receipt'] is not None:
                _receipt(state['native_receipt'], row['native_client_id'])
                if state['native_intent_sha256'] is not None and state['native_receipt']['request_sha256'] != state['native_intent_sha256']:
                    _fail('invalid_record')
            for key in ('checkpoint', 'sent_anchor'):
                if state[key] is not None: _object(state[key])
            if state['review_reason'] is not None: _text(state['review_reason'], 1024)
            if state['not_sent_source'] is not None:
                _human_source(state['not_sent_source'])
            if state['closure'] is not None:
                closure = _fields(state['closure'], ('source', 'closed_at', 'semantics'))
                _human_source(closure['source']); _timestamp(closure['closed_at'])
                if _json(closure['semantics'], 1024)[0] != _json(CLOSURE_SEMANTICS, 1024)[0]: _fail('invalid_record')
            if state['result'] is not None:
                result = state['result']; supplied = {key: result[key] for key in ('text', 'source_anchor', 'summary') if key in result}
                if _result(supplied) != result: _fail('invalid_record')
            if (row['phase'] == 'prepared' and row['dispatch_started']
                    or row['phase'] in ('dispatch_started', 'send_started', 'sent', 'completed', 'needs_review', 'closed_unknown') and not row['dispatch_started']
                    or row['phase'] in ('send_started', 'sent', 'completed') and not row['send_started']
                    or row['phase'] == 'not_sent' and row['send_started']
                    or state['native_receipt'] is not None and not row['dispatch_started']
                    or state['sent_anchor'] is not None and not row['send_started']
                    or bool(row['send_started']) != (state['checkpoint'] is not None)
                    or row['phase'] in ('sent', 'completed') and state['sent_anchor'] is None
                    or (row['phase'] == 'completed') != (state['result'] is not None)
                    or (row['phase'] == 'closed_unknown') != (state['closure'] is not None)
                    or (row['phase'] == 'not_sent') != (state['not_sent_source'] is not None)):
                _fail('invalid_record')
            for key in ('created_at', 'updated_at'):
                _timestamp(row[key])
            return {'schema_version': 1, 'workspace_id': self.workspace_id, 'task_id': task,
                'desktop_id': row['desktop_id'], 'request': checked, 'request_sha256': digest,
                'revision': row['revision'], 'phase': row['phase'], 'dispatch_started': bool(row['dispatch_started']),
                'send_started': bool(row['send_started']), 'native_client_id': row['native_client_id'],
                **state, 'created_at': row['created_at'], 'updated_at': row['updated_at'], 'semantics': deepcopy(SEMANTICS)}
        except (InterfaceTaskError, KeyError, ValueError, TypeError, RecursionError):
            _fail('invalid_record')

    def _get(self, db, task):
        return self._row(db.execute('SELECT * FROM interface_tasks WHERE workspace_id=? AND task_id=?',
            (self.workspace_id, task)).fetchone())

    def get(self, task_id):
        with self._db() as db: return self._get(db, _uuid(task_id))

    def active(self, desktop_id):
        if type(desktop_id) is not str or _DESKTOP.fullmatch(desktop_id) is None: _fail('invalid_arguments')
        with self._db() as db:
            return self._row(db.execute("SELECT * FROM interface_tasks WHERE workspace_id=? AND desktop_id=? "
                "AND phase NOT IN ('completed','not_sent','closed_unknown')", (self.workspace_id, desktop_id)).fetchone())

    def recovery_candidates(self):
        """List metadata for explicit sync; never resume or fill a send gap.

        Dispatched terminal rows remain candidates because workflow closure
        does not establish native stop. The caller explicitly gets the full
        task before checking its original native operation.
        """
        with self._db() as db:
            rows = db.execute('SELECT task_id,desktop_id,phase,revision,dispatch_started,send_started,native_client_id '
                'FROM interface_tasks WHERE workspace_id=? ORDER BY task_id LIMIT ?',
                (self.workspace_id, MAX_TASKS + 1)).fetchall()
            if len(rows) > MAX_TASKS: _fail('invalid_record')
            result = []
            for row in rows:
                try:
                    task = _uuid(row['task_id']); phase = row['phase']
                    if (type(row['desktop_id']) is not str or _DESKTOP.fullmatch(row['desktop_id']) is None
                            or type(phase) is not str or phase not in PHASES
                            or type(row['revision']) is not int or not 1 <= row['revision'] <= MAX_REVISION
                            or any(type(row[key]) is not int or row[key] not in (0, 1) for key in ('dispatch_started', 'send_started'))
                            or row['send_started'] > row['dispatch_started']
                            or row['native_client_id'] != 'ui-task/' + task
                            or phase == 'prepared' and row['dispatch_started']
                            or phase in ('dispatch_started','send_started','sent','completed','needs_review','closed_unknown') and not row['dispatch_started']
                            or phase in ('send_started','sent','completed') and not row['send_started']
                            or phase in ('dispatch_started','not_sent') and row['send_started']):
                        _fail('invalid_record')
                    if phase not in TERMINAL or row['dispatch_started']:
                        result.append({key: bool(row[key]) if key in ('dispatch_started','send_started') else row[key]
                            for key in row.keys()})
                except (InterfaceTaskError, KeyError, ValueError, TypeError):
                    _fail('invalid_record')
            return result

    def maintenance_state(self):
        """Nonblocking global counts only; incomplete never means idle."""
        from rabbit_runtime.maintenance import count_rows
        return count_rows(self.path, {'active_tasks': ('interface_tasks',
            "phase NOT IN ('completed','not_sent','closed_unknown') OR phase IS NULL", ())},
            lock=self.lock, expected_identity=self._identity, check_path=self._check)

    def operation(self, operation_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM interface_task_operations WHERE workspace_id=? AND operation_id=?',
                (self.workspace_id, _uuid(operation_id))).fetchone()
            return self._operation(row, db) if row is not None else None

    def _operation(self, row, db):
        try:
            _uuid(row['operation_id']); _uuid(row['task_id']); _sha(row['operation_sha256'])
            ack = _loaded(row['ack_json'], 4096)
            _fields(ack, ('schema_version', 'workspace_id', 'task_id', 'operation_id', 'operation_sha256',
                'request_sha256', 'revision', 'phase', 'native_client_id', 'semantics'), ('native_intent_sha256',))
            task = self._get(db, row['task_id'])
            if (row['workspace_id'] != self.workspace_id or ack['task_id'] != row['task_id']
                    or type(ack['schema_version']) is not int or ack['schema_version'] != 1
                    or ack['workspace_id'] != self.workspace_id or ack['operation_id'] != row['operation_id']
                    or ack['operation_sha256'] != row['operation_sha256']
                    or _json(ack['semantics'], 1024)[0] != _json(SEMANTICS, 1024)[0]
                    or type(ack['revision']) is not int or not 1 <= ack['revision'] <= MAX_REVISION
                    or ack['phase'] not in PHASES or ack['native_client_id'] != 'ui-task/' + row['task_id']
                    or task is None or ack['revision'] > task['revision'] or ack['request_sha256'] != task['request_sha256']):
                _fail('invalid_record')
            _sha(ack['request_sha256'])
            if 'native_intent_sha256' in ack:
                if _sha(ack['native_intent_sha256']) != task['native_intent_sha256']: _fail('invalid_record')
            elif task['native_intent_sha256'] is not None and ack['phase'] != 'prepared':
                _fail('invalid_record')
            return ack
        except (KeyError, ValueError, TypeError): _fail('invalid_record')

    def create(self, task_id, operation_id, *, expected_revision=0, request):
        task = _uuid(task_id); checked, raw, digest = _request(self.workspace_id, task, request)
        return self._write(task, operation_id, expected_revision, 'create', checked, create=(raw, digest))

    def transition(self, task_id, operation_id, expected_revision, action, payload):
        if type(action) is not str or action not in ('dispatch', 'attach_native_receipt', 'checkpoint_send',
                'report_sent', 'needs_review', 'complete', 'not_sent', 'close_unknown'): _fail('invalid_arguments')
        return self._write(_uuid(task_id), operation_id, expected_revision, action,
            json.loads(_json(payload, MAX_RESULT_BYTES + MAX_ANCHOR_BYTES + MAX_SUMMARY_BYTES + 4096)[0]))

    def claim_dispatch(self, task_id, operation_id, expected_revision, *, native_intent_sha256=None, existing_only=False):
        """Atomically distinguish this new dispatch intent from an old ack.

        Only ``created=True`` authorizes the caller's single send attempt. A
        recovered/replayed claim never authorizes filling a crash gap. This
        transient flag is not stored inside, or substituted for, the old ack.
        ``existing_only`` verifies a replay atomically, returning None if absent;
        it never creates an intent after admission or rendering has failed. An
        omitted intent in that mode is recovered from the original ack, not a
        new render. An explicitly different intent always conflicts.
        """
        if type(existing_only) is not bool: _fail('invalid_arguments')
        task = _uuid(task_id)
        payload = {'native_client_id': 'ui-task/' + task}
        if native_intent_sha256 is not None: payload['native_intent_sha256'] = _sha(native_intent_sha256)
        return self._write(task, operation_id, expected_revision, 'dispatch',
            payload, return_created=True, existing_only=existing_only)

    def _write(self, task, operation_id, revision, action, payload, *, create=None, return_created=False, existing_only=False):
        operation = _uuid(operation_id)
        if type(revision) is not int or not 0 <= revision < MAX_REVISION: _fail('invalid_arguments')
        with self._db(write=True) as db:
            old = db.execute('SELECT * FROM interface_task_operations WHERE workspace_id=? AND operation_id=?',
                (self.workspace_id, operation)).fetchone()
            old_ack = self._operation(old, db) if old is not None else None
            if (existing_only and old_ack is not None and action == 'dispatch'
                    and 'native_intent_sha256' not in payload and 'native_intent_sha256' in old_ack):
                payload = dict(payload, native_intent_sha256=old_ack['native_intent_sha256'])
            _, op_sha = _json({'workspace_id': self.workspace_id, 'task_id': task, 'operation_id': operation,
                'expected_revision': revision, 'action': action, 'payload': payload}, MAX_REQUEST_BYTES + MAX_RESULT_BYTES + 16384)
            if old is not None:
                if old['task_id'] != task or old['operation_sha256'] != op_sha: _fail('operation_conflict')
                return {'ack': old_ack, 'created': False} if return_created else old_ack
            if existing_only: return None
            current = self._get(db, task)
            if (current is None and revision != 0 or current is not None and current['revision'] != revision):
                _fail('revision_conflict')
            stamp = datetime.now(timezone.utc).isoformat(timespec='microseconds')
            if create is not None:
                if current is not None: _fail('revision_conflict')
                if db.execute('SELECT COUNT(*) FROM interface_tasks').fetchone()[0] >= MAX_TASKS: _fail('capacity')
                current = {'desktop_id': payload['desktop_id'], 'request_sha256': create[1], 'revision': 1,
                    'phase': 'prepared', 'dispatch_started': False, 'send_started': False,
                    'native_client_id': 'ui-task/' + task, **_initial_state()}
                try:
                    db.execute('INSERT INTO interface_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (self.workspace_id, task, payload['desktop_id'], create[1], create[0], 1, 'prepared', 0, 0,
                         current['native_client_id'], _json(_initial_state(), MAX_REQUEST_BYTES)[0], stamp, stamp))
                except sqlite3.IntegrityError: _fail('desktop_busy')
            else:
                if current is None: _fail('not_found')
                self._advance(current, action, payload, stamp=stamp)
                current['revision'] += 1
                state = {key: current[key] for key in _initial_state()}
                db.execute('UPDATE interface_tasks SET revision=?,phase=?,dispatch_started=?,send_started=?,state_json=?,updated_at=? '
                    'WHERE workspace_id=? AND task_id=?', (current['revision'], current['phase'], int(current['dispatch_started']),
                    int(current['send_started']), _json(state, MAX_RESULT_BYTES + MAX_SUMMARY_BYTES + 65536)[0], stamp, self.workspace_id, task))
            if db.execute('SELECT COUNT(*) FROM interface_task_operations').fetchone()[0] >= MAX_OPERATIONS: _fail('capacity')
            ack = {'schema_version': 1, 'workspace_id': self.workspace_id, 'task_id': task,
                'operation_id': operation, 'operation_sha256': op_sha, 'request_sha256': current['request_sha256'],
                'revision': current['revision'], 'phase': current['phase'], 'native_client_id': current['native_client_id'],
                'semantics': deepcopy(SEMANTICS)}
            if current['native_intent_sha256'] is not None: ack['native_intent_sha256'] = current['native_intent_sha256']
            db.execute('INSERT INTO interface_task_operations VALUES(?,?,?,?,?)',
                (self.workspace_id, operation, op_sha, task, _json(ack, 4096)[0]))
            ack = json.loads(_json(ack, 4096)[0])
            return {'ack': ack, 'created': True} if return_created else ack

    @staticmethod
    def _advance(task, action, value, *, stamp):
        phase = task['phase']
        if action == 'attach_native_receipt':
            if not task['dispatch_started']: _fail('state_conflict')
            value = _receipt(value, task['native_client_id']); previous = task['native_receipt']
            if task['native_intent_sha256'] is not None and task['native_intent_sha256'] != value['request_sha256']:
                _fail('state_conflict')
            if previous is not None:
                if any(previous[key] is not None and previous[key] != value[key] for key in
                        ('native_client_id', 'thread_id', 'turn_id', 'request_sha256')): _fail('state_conflict')
                if (previous['status'] in ('completed', 'failed', 'interrupted', 'rejected') and previous['status'] != value['status']
                        or previous['status'] == 'accepted' and value['status'] in ('submitted_unknown', 'rejected')):
                    _fail('state_conflict')
            task['native_receipt'] = value; return
        if phase in TERMINAL: _fail('state_conflict')
        if action == 'dispatch':
            _fields(value, ('native_client_id',), ('native_intent_sha256',))
            if phase != 'prepared' or value['native_client_id'] != task['native_client_id']: _fail('state_conflict')
            if 'native_intent_sha256' in value: task['native_intent_sha256'] = _sha(value['native_intent_sha256'])
            task.update(phase='dispatch_started', dispatch_started=True)
        elif action == 'checkpoint_send':
            _fields(value, ('source_anchor',))
            if phase != 'dispatch_started': _fail('state_conflict')
            task.update(phase='send_started', send_started=True, checkpoint=_object(value['source_anchor']))
        elif action == 'report_sent':
            _fields(value, ('source_anchor',))
            if phase not in ('send_started', 'needs_review') or not task['send_started']: _fail('state_conflict')
            anchor = _object(value['source_anchor'])
            if task['sent_anchor'] is not None and task['sent_anchor'] != anchor: _fail('state_conflict')
            task.update(phase='sent', sent_anchor=anchor)
        elif action == 'needs_review':
            _fields(value, ('reason',))
            if phase == 'prepared': _fail('state_conflict')
            task.update(phase='needs_review', review_reason=_text(value['reason'], 1024))
        elif action == 'complete':
            if phase != 'sent': _fail('state_conflict')
            task.update(phase='completed', result=_result(value))
        elif action == 'close_unknown':
            _fields(value, ('source',))
            if not task['dispatch_started']: _fail('state_conflict')
            task.update(phase='closed_unknown', closure={'source': _human_source(value['source']),
                'closed_at': stamp, 'semantics': deepcopy(CLOSURE_SEMANTICS)})
        else:
            _fields(value, ('source',))
            if task['send_started']: _fail('state_conflict')
            task.update(phase='not_sent', not_sent_source=_human_source(value['source']))

    def close(self):
        with self.lock: self._closed = True
