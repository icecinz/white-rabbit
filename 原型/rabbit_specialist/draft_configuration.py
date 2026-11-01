"""One durable metadata receipt per specialist command, never a chat or task queue.

The host supplies named-profile reads, capability validation and the active-turn
guard. Browser ownership is separate from model-visible command identity.
"""
from contextlib import contextmanager
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time
import uuid

PATH = '/api/management/draft-configuration'
SCOPE = 'home_configuration_proposal'
TABLE = 'specialist_draft_configurations'
MAX_RECORDS = 4096
MAX_REVISION = 2 ** 53 - 1
REASONS = frozenset(('target_changed', 'configuration_changed', 'handoff_started',
                     'storage_unavailable', 'unsupported', 'cancelled'))
EFFORTS = frozenset(('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'))
PROFILE_KEYS = frozenset(('id', 'revision', 'name', 'agent_instance_id', 'device_id',
                          'model_id', 'reasoning_effort'))
IDENTITY = frozenset(('client_id', 'draft_id', 'configuration_revision'))
PUBLIC_KEYS = IDENTITY | {'workspace_id', 'scope', 'base_profile', 'parameters',
                         'proposal', 'state', 'application', 'native_turn'}
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_COLUMNS = ('workspace_id', 'client_id', 'owner_sha256', 'record_json')
_DDL = '''CREATE TABLE specialist_draft_configurations (
    workspace_id TEXT NOT NULL, client_id TEXT NOT NULL, owner_sha256 TEXT NOT NULL,
    record_json TEXT NOT NULL, PRIMARY KEY (workspace_id, client_id))'''


class DraftConfigurationError(ValueError):
    def __init__(self, reason='invalid'):
        self.code = 'draft_configuration_' + reason
        self.status = (404 if reason == 'not_found' else 403 if reason == 'owner_mismatch'
                       else 400 if reason == 'invalid' else 503 if reason == 'storage_unconfirmed' else 409)
        self.accepted = None if reason == 'storage_unconfirmed' else False
        super().__init__({'not_found': '没有这次首页配置登记。',
            'active_turn_unconfirmed': '无法核实这是专员正在处理的原指令。',
            'conflict': '这次首页配置记录已变化，请核对原记录。',
            'owner_mismatch': '请在原浏览器处理这次设置建议。',
            'capacity': '首页配置记录已达上限，未新增登记。',
            'corrupt': '首页配置记录需要核对，原数据保留。',
            'storage_unconfirmed': '首页配置保存结果尚未核实，请读取原记录。'
        }.get(reason, '首页配置请求无效。'))


def _fail(reason='invalid'):
    raise DraftConfigurationError(reason)


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            _fail()
    except (ValueError, AttributeError):
        _fail()
    return value


def _revision(value):
    if type(value) is not int or not 1 <= value < MAX_REVISION:
        _fail()
    return value


def _client(value):
    if type(value) is not str or not value.startswith('specialist/ui/'):
        _fail()
    _uuid(value[len('specialist/ui/'):])
    return value


def _text(value, maximum, *, empty=False):
    if (type(value) is not str or len(value) > maximum or (not empty and not value.strip())
            or any(ord(c) < 32 or ord(c) == 127 or 0xd800 <= ord(c) <= 0xdfff for c in value)):
        _fail()
    return value


def _hash(value):
    if type(value) is not str or not _HEX.fullmatch(value):
        _fail()
    return value


def parameters(value):
    if type(value) is not dict or set(value) != {'model_id', 'reasoning_effort'}:
        _fail()
    model = _text(value['model_id'], 1536, empty=True)
    if model and not model.startswith('local/codex/'):
        _fail()
    effort = value['reasoning_effort']
    if effort is not None and (type(effort) is not str or effort not in EFFORTS):
        _fail()
    return {'model_id': model, 'reasoning_effort': effort}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def proposal_sha256(value):
    return hashlib.sha256(_json(parameters(value)).encode('utf-8')).hexdigest()


def _profile(value):
    if type(value) is not dict or set(value) != PROFILE_KEYS:
        _fail()
    _uuid(value['id']); _revision(value['revision']); _text(value['name'], 40)
    if value['agent_instance_id'] != 'local/codex' or value['device_id'] != 'local':
        _fail()
    parameters({key: value[key] for key in ('model_id', 'reasoning_effort')})
    return dict(value)


def request(value):
    """Strict fixed body contract; CSRF is stripped only by the host boundary."""
    if type(value) is not dict or type(value.get('operation')) is not str:
        _fail()
    action = value['operation']
    extra = {'register': {'owner_secret', 'base_profile_id', 'base_profile_revision', 'parameters'},
             'read': {'owner_secret'}, 'inspect': set(), 'propose': {'parameters'},
             'settle': {'owner_secret', 'proposal_sha256', 'result', 'applied_revision', 'reason'},
             'resume': {'owner_secret', 'proposal_sha256'}}.get(action)
    if extra is None:
        _fail()
    required = IDENTITY | {'operation'} | extra
    if action == 'inspect' and 'client_id' not in value:
        required = required - {'client_id'}
    if set(value) != required:
        _fail()
    if 'client_id' in value:
        _client(value['client_id'])
    _uuid(value['draft_id']); _revision(value['configuration_revision'])
    if 'owner_secret' in value:
        _hash(value['owner_secret'])
    if 'parameters' in value:
        parameters(value['parameters'])
    if action == 'register':
        _uuid(value['base_profile_id']); _revision(value['base_profile_revision'])
    if action in ('settle', 'resume'):
        _hash(value['proposal_sha256'])
    if action == 'settle':
        _application({k: value[k] for k in ('result', 'applied_revision', 'reason')},
                     value['configuration_revision'])
    return json.loads(_json(value))


def _application(value, revision):
    if type(value) is not dict or set(value) != {'result', 'applied_revision', 'reason'}:
        _fail()
    if value['result'] == 'applied':
        if type(value['applied_revision']) is not int or value['applied_revision'] != revision + 1 or value['reason'] is not None:
            _fail()
    elif value['result'] == 'rejected':
        if (value['applied_revision'] is not None or type(value['reason']) is not str
                or value['reason'] not in REASONS):
            _fail()
    else:
        _fail()


def _public(value, workspace):
    if type(value) is not dict or set(value) != PUBLIC_KEYS or value['workspace_id'] != workspace or value['scope'] != SCOPE:
        _fail()
    _client(value['client_id']); _uuid(value['draft_id']); _revision(value['configuration_revision'])
    _profile(value['base_profile']); parameters(value['parameters'])
    proposal, application, native = value['proposal'], value['application'], value['native_turn']
    if value['state'] == 'registered':
        if any(item is not None for item in (proposal, application, native)):
            _fail()
    else:
        if (type(proposal) is not dict or set(proposal) != {'parameters', 'sha256'}
                or proposal_sha256(proposal['parameters']) != proposal['sha256']
                or type(native) is not dict or set(native) != {'thread_id', 'turn_id'}):
            _fail()
        for item in native.values():
            _text(item, 256)
        if value['state'] == 'pending':
            if application is not None:
                _fail()
        elif value['state'] in ('applied', 'rejected'):
            _application(application, value['configuration_revision'])
            if application['result'] != value['state']:
                _fail()
        else:
            _fail()
    return value


class DraftConfiguration:
    def __init__(self, store, workspace_id):
        _hash(workspace_id)
        self.store, self.workspace_id = store, workspace_id
        self._changed = threading.Condition()

    @contextmanager
    def _db(self, *, write=False):
        try:
            with self.store.lock, self.store._open(readonly=not write) as db:
                if write:
                    db.execute('BEGIN IMMEDIATE')
                exists = db.execute('SELECT type FROM sqlite_master WHERE name=?', (TABLE,)).fetchone()
                if exists is None and write:
                    db.execute(_DDL)
                elif exists is not None and exists[0] != 'table':
                    _fail('corrupt')
                if exists is not None or write:
                    if tuple(row['name'] for row in db.execute('PRAGMA table_info(' + TABLE + ')')) != _COLUMNS:
                        _fail('corrupt')
                yield db, exists is not None or write
        except sqlite3.Error:
            _fail('storage_unconfirmed')

    def _row(self, db, exists, fields):
        row = db.execute('SELECT * FROM ' + TABLE + ' WHERE workspace_id=? AND client_id=?',
                         (self.workspace_id, fields['client_id'])).fetchone() if exists else None
        if row is None:
            _fail('not_found')
        try:
            if type(row['record_json']) is not str or len(row['record_json'].encode('utf-8')) > 16384:
                _fail()
            value = _public(json.loads(row['record_json']), self.workspace_id)
            _hash(row['owner_sha256'])
            if value['client_id'] != row['client_id'] or _json(value) != row['record_json']:
                _fail()
        except (ValueError, TypeError, KeyError, UnicodeError):
            _fail('corrupt')
        if any(value[key] != fields[key] for key in IDENTITY):
            _fail('conflict')
        return value, row['owner_sha256']

    @staticmethod
    def _owner(fields, owner):
        digest = hashlib.sha256(fields['owner_secret'].encode('ascii')).hexdigest()
        if not hmac.compare_digest(digest, owner):
            _fail('owner_mismatch')

    def _save(self, db, value, owner):
        _public(value, self.workspace_id)
        db.execute('UPDATE ' + TABLE + ' SET owner_sha256=?,record_json=? WHERE workspace_id=? AND client_id=?',
                   (owner, _json(value), self.workspace_id, value['client_id']))

    def read(self, fields, *, owner=False):
        with self._db() as (db, exists):
            value, secret = self._row(db, exists, fields)
            if owner:
                self._owner(fields, secret)
            return value

    def register(self, fields, read_base):
        with self._db(write=True) as (db, exists):
            base = _profile(read_base(db, fields['base_profile_id'], fields['base_profile_revision']))
            value = dict(workspace_id=self.workspace_id, scope=SCOPE,
                **{key: fields[key] for key in IDENTITY}, base_profile=base,
                parameters=parameters(fields['parameters']), proposal=None,
                state='registered', application=None, native_turn=None)
            try:
                old, owner = self._row(db, exists, fields)
            except DraftConfigurationError as error:
                if error.code != 'draft_configuration_not_found':
                    raise
            else:
                self._owner(fields, owner)
                if any(old[key] != value[key] for key in ('base_profile', 'parameters')):
                    _fail('conflict')
                return old
            if db.execute('SELECT COUNT(*) FROM ' + TABLE).fetchone()[0] >= MAX_RECORDS:
                _fail('capacity')
            owner = hashlib.sha256(fields['owner_secret'].encode('ascii')).hexdigest()
            db.execute('INSERT INTO ' + TABLE + ' VALUES (?,?,?,?)',
                       (self.workspace_id, fields['client_id'], owner, _json(value)))
            return value

    def propose(self, fields, read_base, validate, active_guard):
        observed = self.read(fields)
        proposed = parameters(fields['parameters'])
        # Capability discovery/validation must never execute under the turn lock.
        validate(dict(observed['base_profile'], **proposed))
        with self._db(write=True) as (db, exists):
            value, owner = self._row(db, exists, fields)
            base = value['base_profile']
            if value != observed or _profile(read_base(db, base['id'], base['revision'])) != base:
                _fail('conflict')
            proposal = {'parameters': proposed, 'sha256': proposal_sha256(proposed)}
            def commit(client_id, native_turn):
                if client_id != fields['client_id']:
                    _fail('active_turn_unconfirmed')
                if value['proposal'] is not None:
                    if value['proposal'] != proposal or value['native_turn'] != native_turn:
                        _fail('conflict')
                else:
                    value.update(proposal=proposal, native_turn=dict(native_turn), state='pending')
                    self._save(db, value, owner)
                # Only the already-locked CAS and commit occur inside the guard.
                db.commit()
                return value
            return active_guard(fields['client_id'], commit)

    def settle(self, fields):
        with self._db(write=True) as (db, exists):
            value, owner = self._row(db, exists, fields)
            self._owner(fields, owner)
            if value['proposal'] is None or value['proposal']['sha256'] != fields['proposal_sha256']:
                _fail('conflict')
            application = {key: fields[key] for key in ('result', 'applied_revision', 'reason')}
            if value['application'] is not None and value['application'] != application:
                _fail('conflict')
            value.update(state=application['result'], application=application)
            self._save(db, value, owner)
        with self._changed:
            self._changed.notify_all()
        return value

    def resume(self, fields, terminal_check):
        observed = self.read(fields)
        if observed['proposal'] is None or observed['proposal']['sha256'] != fields['proposal_sha256']:
            _fail('conflict')
        terminal_check(fields['client_id'], observed['native_turn'])
        with self._db(write=True) as (db, exists):
            value, _ = self._row(db, exists, fields)
            if value != observed:
                _fail('conflict')
            owner = hashlib.sha256(fields['owner_secret'].encode('ascii')).hexdigest()
            self._save(db, value, owner)
            return value

    def wait(self, fields, timeout=10):
        """Bounded wait for the browser, without holder/SQLite locks or polling."""
        deadline = time.monotonic() + min(10, max(0, timeout))
        with self._changed:
            while True:
                value = self.read(fields)
                remaining = deadline - time.monotonic()
                if value['state'] != 'pending' or remaining <= 0:
                    return value
                self._changed.wait(remaining)
