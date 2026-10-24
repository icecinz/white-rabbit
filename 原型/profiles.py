"""Private saved AI parameters, independent of chat selection and run snapshots.

AIProfiles adds profile, migration-map and default-setting tables to the caller's
private SQLite database, without migrating chat or run records. Reads
use mode=ro/query_only and never revalidate capabilities or write a migration.
Writes require CAS for an existing profile, and copy always gets a new UUID.
Optional creation IDs atomically preserve the first created profile snapshot.
Their SHA256 input is UTF8 JSON (ensure_ascii=False, sort_keys=True,
separators=(',', ':'), allow_nan=False), without creation_id:
create: {'action':'create','profile': all five normalized fields};
copy: {'action':'copy','profile_id':source_id,'revision':source_revision,
       'changes': explicit fields only}. name=None/UNSET are omitted; an
explicit reasoning_effort=None is retained. Replay never rechecks capabilities.

An optional capability_validator receives a COPY of the five normalized fields
and must return None/True, or reject with ValueError/False. Explicit model or
effort choices cannot be saved without that exact capability check. Enumeration
membership is only syntax validation, never proof that a model supports effort.
The callback should not mutate this database or make inference requests.

Empty model_id and reasoning_effort=None retain the existing connector-default
semantics. This data layer makes no claim that a saved connection is online.
Selection, frozen run snapshots and actual route resolution are caller work.
No credentials, arbitrary device IDs or raw route objects are stored here.
"""
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from urllib.parse import quote
import uuid

from preferences import validate_model_id
from rabbit_memory.memory_profiles import make_memory_snapshot, validate_memory_snapshot
from agent_registry import INSTANCES as AGENT_INSTANCES


DEVICE_IDS = frozenset(('local',))
REASONING_EFFORTS = frozenset(('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'))
PROFILE_FIELDS = ('name', 'agent_instance_id', 'device_id', 'model_id', 'reasoning_effort')
PROFILE_COLUMNS = ('id', 'revision') + PROFILE_FIELDS + ('created_at', 'updated_at')
MAX_REVISION = 2 ** 53 - 1
MAX_PROFILES = 128
UNSET = object()
SNAPSHOT_PROFILE_FIELDS = ('id', 'revision') + PROFILE_FIELDS


def initialize_schema(db):
    """Add this module's tables using an already-open caller transaction."""
    db.execute('''CREATE TABLE IF NOT EXISTS ai_profiles (
        id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>0),
        name TEXT NOT NULL, agent_instance_id TEXT NOT NULL, device_id TEXT NOT NULL,
        model_id TEXT NOT NULL, reasoning_effort TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS ai_profile_migrations (
        legacy_key TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES ai_profiles(id))''')
    db.execute('CREATE TABLE IF NOT EXISTS profile_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    db.execute('''CREATE TABLE IF NOT EXISTS ai_profile_creation_receipts (
        creation_id TEXT PRIMARY KEY, action TEXT NOT NULL CHECK(action IN ('create','copy')),
        request_sha256 TEXT NOT NULL, result_id TEXT NOT NULL REFERENCES ai_profiles(id),
        profile_snapshot TEXT NOT NULL)''')


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def validate_profile_id(profile_id):
    if not isinstance(profile_id, str) or len(profile_id) != 36:
        raise ValueError('AI 配置编号需要标准 UUID。')
    try:
        canonical = str(uuid.UUID(profile_id))
    except ValueError:
        raise ValueError('AI 配置编号需要标准 UUID。') from None
    if canonical != profile_id:
        raise ValueError('AI 配置编号需要标准小写 UUID。')
    return profile_id


def validate_profile_revision(revision):
    if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
        raise ValueError('AI 配置版本需要有效的整数。')
    return revision


def validate_profile_fields(name, agent_instance_id, device_id='local', model_id='', reasoning_effort=None):
    """Normalize scalar fields; do not declare capabilities from these enums."""
    if (not isinstance(name, str) or len(name) > 40 or not name.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError('AI 配置名称需要 1–40 个字符，不能包含控制字符。')
    if not isinstance(agent_instance_id, str) or agent_instance_id not in AGENT_INSTANCES:
        raise ValueError('目前只能选择已接入的本机连接。')
    if not isinstance(device_id, str) or device_id not in DEVICE_IDS:
        raise ValueError('目前只能选择已接入的本机设备。')
    backend = agent_instance_id.split('/', 1)[1]
    validate_model_id(model_id, backend)
    if reasoning_effort is not None and (not isinstance(reasoning_effort, str)
                                         or reasoning_effort not in REASONING_EFFORTS):
        raise ValueError('思考强度需要已知选项或 Agent 默认。')
    return dict(name=name.strip(), agent_instance_id=agent_instance_id, device_id=device_id,
                model_id=model_id, reasoning_effort=reasoning_effort)


def validate_request_snapshot(snapshot):
    """Bounded intent only; resolved route/actual model remain separate facts.

    None is the legacy absent snapshot, never a fabricated historical profile.
    Version 1 preserves explicit none. Version 2 carries a validated memory
    selection. Retrieved facts live separately in the accepted run, never in
    shared/browser intent. This pure codec does not prove service availability.
    """
    if snapshot is None:
        return None
    if (not isinstance(snapshot, dict)
            or set(snapshot) != {'schema_version', 'ai_profile', 'memory_profile', 'used_memory_references'}
            or type(snapshot.get('schema_version')) is not int or snapshot['schema_version'] not in (1, 2)
            or not isinstance(snapshot.get('ai_profile'), dict)
            or set(snapshot['ai_profile']) != set(SNAPSHOT_PROFILE_FIELDS)
            or not isinstance(snapshot.get('used_memory_references'), list)
            or snapshot['used_memory_references']):
        raise ValueError('本轮配置选择快照无效。')
    memory = validate_memory_snapshot(snapshot.get('memory_profile'))
    if snapshot['schema_version'] == 1 and memory != {'id': 'none'}:
        raise ValueError('旧版配置快照不包含独立记忆库。')
    profile = snapshot['ai_profile']
    fields = validate_profile_fields(**{key: profile[key] for key in PROFILE_FIELDS})
    return {'schema_version': snapshot['schema_version'], 'ai_profile': dict(id=validate_profile_id(profile['id']),
        revision=validate_profile_revision(profile['revision']), **fields),
        'memory_profile': memory, 'used_memory_references': []}


def make_profile_snapshot(profile, memory_profile=None, *, schema_version=None):
    if not isinstance(profile, dict) or any(key not in profile for key in SNAPSHOT_PROFILE_FIELDS):
        raise ValueError('AI 配置记录不完整。')
    memory = make_memory_snapshot(memory_profile) if memory_profile is not None else {'id': 'none'}
    version = schema_version if schema_version is not None else (1 if memory == {'id': 'none'} else 2)
    return validate_request_snapshot({'schema_version': version,
        'ai_profile': {key: profile[key] for key in SNAPSHOT_PROFILE_FIELDS},
        'memory_profile': memory, 'used_memory_references': []})


class ProfileConflict(ValueError):
    def __init__(self, profile):
        super().__init__('AI 配置已在另一入口更新，请先查看新版本。')
        self.profile = dict(profile)


class ProfileCreationConflict(ValueError):
    def __init__(self, creation_id):
        super().__init__('这次新建标识已有不同请求，请核对原回执；没有再新建配置。')
        self.creation_id = creation_id


class DefaultProfileConflict(ValueError):
    def __init__(self, default_state):
        super().__init__('默认 AI 配置已在另一入口更新，请先查看当前选择。')
        self.default_state = dict(default_state)


def _creation_sha256(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError('配置新建请求无效。') from None
    return hashlib.sha256(raw).hexdigest()


def _default_revision(value):
    if type(value) is not int or not 0 <= value <= MAX_REVISION:
        raise ValueError('默认 AI 配置版本需要有效的整数。')
    return value


class AIProfiles:
    """Saved profiles in the same private DB, with independent integer CAS."""
    def __init__(self, path, *, capability_validator=None):
        self.path = Path(os.path.abspath(os.path.expanduser(str(path))))
        cloud = Path.home() / 'Library' / 'Mobile Documents'
        if self.path == cloud or cloud in self.path.parents:
            raise ValueError('AI 配置需要保存在本机聊天库，不能放进 iCloud 项目目录。')
        if '..' in self.path.parts or self.path.is_symlink():
            raise ValueError('AI 配置数据库路径无效。')
        for parent in self.path.parents:
            if parent.is_symlink():
                raise ValueError('AI 配置数据库目录不能是符号链接。')
        if self.path.exists() and not self.path.is_file():
            raise ValueError('AI 配置数据库需要普通文件。')
        if capability_validator is not None and not callable(capability_validator):
            raise ValueError('AI 能力校验器无效。')
        self.capability_validator = capability_validator
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._open(create=True) as db:
            db.execute('BEGIN IMMEDIATE')
            initialize_schema(db)
        # Same private database as ChatStore; no existing table is migrated.
        os.chmod(self.path, 0o600)

    @contextmanager
    def _open(self, *, readonly=False, create=False):
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
    def get_in(db, profile_id):
        """Read one AI profile through the caller's current SQLite snapshot.

        The connection must use sqlite3.Row and the complete ai_profiles schema.
        The caller validates the ID and owns the transaction. This method opens
        no connection, performs no capability check, and never commits or rolls back.
        """
        row = db.execute('SELECT ' + ','.join(PROFILE_COLUMNS) + ' FROM ai_profiles WHERE id=?', (profile_id,)).fetchone()
        if row is None:
            raise KeyError('找不到这份 AI 配置。')
        return dict(row)

    def get(self, profile_id):
        validate_profile_id(profile_id)
        with self.lock, self._open(readonly=True) as db:
            return self.get_in(db, profile_id)

    def list(self):
        with self.lock, self._open(readonly=True) as db:
            return {'profiles': self._profiles_in(db)}

    @staticmethod
    def _profiles_in(db):
        rows = db.execute('SELECT ' + ','.join(PROFILE_COLUMNS) +
                          ' FROM ai_profiles ORDER BY updated_at DESC,created_at DESC,id')
        return [dict(row) for row in rows]

    @staticmethod
    def _default_state_in(db, *, validate=True):
        rows = dict(db.execute("SELECT key,value FROM profile_settings WHERE key IN "
                               "('default_ai_profile_id','default_ai_profile_revision')").fetchall())
        value = rows.get('default_ai_profile_revision', '0')
        try:
            revision = _default_revision(int(value))
            if str(revision) != value:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise ValueError('默认 AI 配置版本记录无效。') from None
        identity = rows.get('default_ai_profile_id')
        if validate and identity is not None:
            validate_profile_id(identity)
            AIProfiles.get_in(db, identity)
        return {'ai_profile_id': identity, 'revision': revision}

    def get_default_state(self):
        with self.lock, self._open(readonly=True) as db:
            db.execute('BEGIN')
            return self._default_state_in(db)

    @staticmethod
    def _readable_default_in(db, state):
        try:
            if state['ai_profile_id'] is not None:
                validate_profile_id(state['ai_profile_id'])
                AIProfiles.get_in(db, state['ai_profile_id'])
        except (ValueError, KeyError):
            return dict(state, ai_profile_id=None)
        return dict(state)

    def saved_state(self):
        """One readonly snapshot; an invalid default cannot hide valid profiles.

        Strict default reads still reject damaged references. The listing uses
        an unresolved selection and a private fingerprint, never the bad value,
        so observers detect changes without repairing or disclosing the row.
        The actual revision remains strict for subsequent CAS writes.
        """
        with self.lock, self._open(readonly=True) as db:
            db.execute('BEGIN')
            profiles = self._profiles_in(db)
            state = self._default_state_in(db, validate=False)
            readable = self._readable_default_in(db, state)
            saved = {'profiles': profiles, 'default_state': readable}
            if readable != state:
                rows = db.execute("SELECT key,typeof(value),hex(value) FROM profile_settings WHERE key IN "
                    "('default_ai_profile_id','default_ai_profile_revision') ORDER BY key").fetchall()
                saved.update(default_unavailable=True,
                    default_fingerprint=hashlib.sha256(json.dumps([tuple(row) for row in rows],
                        ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest())
            return saved

    def get_default(self):
        return self.get_default_state()['ai_profile_id']

    def set_default(self, profile_id, *, expected_revision=UNSET):
        return self.set_default_state(profile_id, expected_revision=expected_revision)['ai_profile_id']

    def set_default_state(self, profile_id, *, expected_revision=UNSET):
        """Return this transaction's acknowledgement, never a later reread."""
        validate_profile_id(profile_id)
        if expected_revision is not UNSET:
            _default_revision(expected_revision)
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            # An explicit legacy choice may repair a dangling old default ID.
            state = self._default_state_in(db, validate=False)
            if expected_revision is not UNSET and expected_revision != state['revision']:
                raise DefaultProfileConflict(self._readable_default_in(db, state))
            self.get_in(db, profile_id)
            if state['ai_profile_id'] != profile_id:
                if state['revision'] == MAX_REVISION:
                    raise ValueError('默认 AI 配置已达到版本上限。')
                db.executemany('INSERT INTO profile_settings(key,value) VALUES(?,?) '
                               'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                               [('default_ai_profile_id', profile_id),
                                ('default_ai_profile_revision', str(state['revision']+1))])
                state = {'ai_profile_id': profile_id, 'revision': state['revision']+1}
            return state

    @staticmethod
    def _creation_in(db, creation_id):
        row = db.execute('SELECT * FROM ai_profile_creation_receipts WHERE creation_id=?', (creation_id,)).fetchone()
        if row is None:
            return None
        try:
            profile = json.loads(row['profile_snapshot'])
            if (row['action'] not in ('create', 'copy') or type(profile) is not dict
                    or set(profile) != set(PROFILE_COLUMNS) or profile['id'] != row['result_id']
                    or type(row['request_sha256']) is not str or len(row['request_sha256']) != 64
                    or any(c not in '0123456789abcdef' for c in row['request_sha256'])):
                raise ValueError
            validate_profile_id(profile['id']); validate_profile_revision(profile['revision'])
            fields = validate_profile_fields(**{key: profile[key] for key in PROFILE_FIELDS})
            if fields != {key: profile[key] for key in PROFILE_FIELDS}:
                raise ValueError
            if not all(type(profile[key]) is str for key in ('created_at', 'updated_at')):
                raise ValueError
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ValueError('配置新建回执记录无效。') from None
        return {'creation_id': creation_id, 'action': row['action'],
                'request_sha256': row['request_sha256'], 'profile': profile}

    def get_creation_receipt(self, creation_id):
        validate_profile_id(creation_id)
        with self.lock, self._open(readonly=True) as db:
            return self._creation_in(db, creation_id)

    @staticmethod
    def _replay_creation_in(db, creation_id, action, request_sha256):
        if creation_id is None:
            return None
        receipt = AIProfiles._creation_in(db, creation_id)
        if receipt is not None:
            if receipt['action'] != action or receipt['request_sha256'] != request_sha256:
                raise ProfileCreationConflict(creation_id)
            return receipt['profile']
        return None

    @staticmethod
    def _record_creation_in(db, creation_id, action, request_sha256, profile):
        if creation_id is not None:
            snapshot = {key: profile[key] for key in PROFILE_COLUMNS}
            db.execute('INSERT INTO ai_profile_creation_receipts VALUES(?,?,?,?,?)',
                       (creation_id, action, request_sha256, profile['id'],
                        json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'), allow_nan=False)))

    def _capabilities(self, fields, validator):
        check = self.capability_validator if validator is None else validator
        if check is None:
            if fields['model_id'] or fields['reasoning_effort'] is not None:
                raise ValueError('尚未核对这条连接的精确模型和强度，不能保存显式选择。')
            return
        if not callable(check):
            raise ValueError('AI 能力校验器无效。')
        outcome = check(dict(fields))
        if outcome is False:
            raise ValueError('所选 Agent、模型或强度组合当前不受支持。')
        if outcome is not None and outcome is not True:
            raise ValueError('AI 能力校验器需要明确通过或拒绝。')

    @staticmethod
    def _insert_in(db, fields):
        if db.execute('SELECT COUNT(*) FROM ai_profiles').fetchone()[0] >= MAX_PROFILES:
            raise ValueError('AI 配置已达到 128 份上限；原配置和旧数据仍保留。')
        profile_id, stamp = str(uuid.uuid4()), _now()
        db.execute('''INSERT INTO ai_profiles(id,revision,name,agent_instance_id,device_id,model_id,
            reasoning_effort,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)''',
            (profile_id, 1) + tuple(fields[key] for key in PROFILE_FIELDS) + (stamp, stamp))
        return AIProfiles.get_in(db, profile_id)

    def import_legacy(self, legacy_key, name, agent_instance_id, device_id='local', model_id='', reasoning_effort=None):
        """Server-only preservation; a saved legacy choice is not proof of support.

        A key is imported once. Later preferences or names never rewrite the
        profile to which it was mapped. This method must not be exposed by HTTP.
        """
        if (not isinstance(legacy_key, str) or not 1 <= len(legacy_key) <= 256
                or any(ord(char) < 32 or ord(char) == 127 for char in legacy_key)):
            raise ValueError('旧配置迁移标识无效。')
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT profile_id FROM ai_profile_migrations WHERE legacy_key=?', (legacy_key,)).fetchone()
            if previous is not None:
                return self.get_in(db, previous['profile_id'])
            fields = validate_profile_fields(name, agent_instance_id, device_id, model_id, reasoning_effort)
            profile = self._insert_in(db, fields)
            db.execute('INSERT INTO ai_profile_migrations(legacy_key,profile_id) VALUES(?,?)', (legacy_key, profile['id']))
            return profile

    def create(self, name, agent_instance_id, device_id='local', model_id='', reasoning_effort=None, *, validator=None, creation_id=None):
        fields = validate_profile_fields(name, agent_instance_id, device_id, model_id, reasoning_effort)
        if creation_id is not None:
            validate_profile_id(creation_id)
        request_sha = _creation_sha256({'action': 'create', 'profile': fields}) if creation_id is not None else None
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            replay = self._replay_creation_in(db, creation_id, 'create', request_sha)
            if replay is not None:
                return replay
            self._capabilities(fields, validator)
            profile = self._insert_in(db, fields)
            self._record_creation_in(db, creation_id, 'create', request_sha, profile)
            return profile

    def update(self, profile_id, revision, *, name=UNSET, agent_instance_id=UNSET,
               device_id=UNSET, model_id=UNSET, reasoning_effort=UNSET, validator=None):
        validate_profile_id(profile_id)
        validate_profile_revision(revision)
        changes = dict(name=name, agent_instance_id=agent_instance_id, device_id=device_id,
                       model_id=model_id, reasoning_effort=reasoning_effort)
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self.get_in(db, profile_id)
            if old['revision'] != revision:
                raise ProfileConflict(old)
            fields = validate_profile_fields(**{key: old[key] if value is UNSET else value
                                                for key, value in changes.items()})
            self._capabilities(fields, validator)
            if all(old[key] == fields[key] for key in PROFILE_FIELDS):
                return old
            if revision == MAX_REVISION:
                raise ValueError('这份 AI 配置已达到版本上限。')
            db.execute('''UPDATE ai_profiles SET revision=revision+1,name=?,agent_instance_id=?,device_id=?,
                model_id=?,reasoning_effort=?,updated_at=? WHERE id=? AND revision=?''',
                tuple(fields[key] for key in PROFILE_FIELDS) + (_now(), profile_id, revision))
            return self.get_in(db, profile_id)

    def copy(self, profile_id, revision, *, name=None, agent_instance_id=UNSET,
             device_id=UNSET, model_id=UNSET, reasoning_effort=UNSET, validator=None, creation_id=None):
        """Copy reviewed parameters into a NEW ID; the original is never edited."""
        validate_profile_id(profile_id)
        validate_profile_revision(revision)
        changes = dict(agent_instance_id=agent_instance_id, device_id=device_id,
                       model_id=model_id, reasoning_effort=reasoning_effort)
        if creation_id is not None:
            validate_profile_id(creation_id)
        explicit = {key: value for key, value in changes.items() if value is not UNSET}
        if name is not None:
            explicit['name'] = name.strip() if isinstance(name, str) else name
        request_sha = _creation_sha256({'action': 'copy', 'profile_id': profile_id,
            'revision': revision, 'changes': explicit}) if creation_id is not None else None
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            replay = self._replay_creation_in(db, creation_id, 'copy', request_sha)
            if replay is not None:
                return replay
            old = self.get_in(db, profile_id)
            if old['revision'] != revision:
                raise ProfileConflict(old)
            fields = {key: old[key] if value is UNSET else value for key, value in changes.items()}
            fields['name'] = old['name'][:37] + ' 副本' if name is None else name
            fields = validate_profile_fields(**fields)
            self._capabilities(fields, validator)
            profile = self._insert_in(db, fields)
            self._record_creation_in(db, creation_id, 'copy', request_sha, profile)
            return profile
