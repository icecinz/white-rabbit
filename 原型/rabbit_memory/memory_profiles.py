"""Private memory configuration; no bank reads, provisioning, or inference.

Each profile owns a server-generated ``rabbit-{uuid}`` namespace. Creating or
copying reserves a fresh namespace locally; the caller must provision/check the
remote bank before declaring it available. Renaming never changes its binding.
Connection parameters are references from a fixed allowlist, never URLs/keys.
Only this module's tables are initialized in the caller's private chat database.
Frozen snapshots are pure scalar intent and do not assert runtime availability.
Optional creation IDs preserve the first create/copy snapshot transactionally.
Their SHA256 is canonical UTF8 JSON without creation_id: create hashes all six
normalized fields (including defaults); copy hashes the source ID/revision and
only explicit changes. A replay never reads the source's later revision.
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


PROGRAM_CONNECTIONS = {'hindsight': 'local/hindsight',
                       'hermes_files': 'local/hermes-files', 'mem0': 'local/mem0'}
PROGRAMS = frozenset(PROGRAM_CONNECTIONS)
CONNECTION_IDS = frozenset(PROGRAM_CONNECTIONS.values())
MODES = frozenset(('read_only', 'read_write'))
MAX_PROFILES = 128
MAX_REVISION = 2 ** 53 - 1
UNSET = object()
MUTABLE_FIELDS = ('name', 'description', 'habits', 'mode')
SNAPSHOT_FIELDS = ('id', 'revision', 'name', 'description', 'habits', 'program',
                   'connection_id', 'mode', 'bank_ref')
PROFILE_COLUMNS = SNAPSHOT_FIELDS[:-1] + ('bank_id', 'created_at', 'updated_at')
CREATION_RECEIPTS_DDL = '''CREATE TABLE IF NOT EXISTS memory_profile_creation_receipts (
    creation_id TEXT PRIMARY KEY, action TEXT NOT NULL CHECK(action IN ('create','copy')),
    request_sha256 TEXT NOT NULL, result_id TEXT NOT NULL REFERENCES memory_profiles(id),
    profile_snapshot TEXT NOT NULL)'''


LEGACY_PROFILE_SQL = '''CREATE TABLE memory_profiles (
        id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>0),
        name TEXT NOT NULL, description TEXT NOT NULL, habits TEXT NOT NULL,
        program TEXT NOT NULL CHECK(program='hindsight'),
        connection_id TEXT NOT NULL CHECK(connection_id='local/hindsight'),
        mode TEXT NOT NULL CHECK(mode IN ('read_only','read_write')),
        bank_id TEXT NOT NULL UNIQUE CHECK(bank_id='rabbit-' || id),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)'''
PROFILE_SQL = LEGACY_PROFILE_SQL.replace("CHECK(program='hindsight')",
    "CHECK(program IN ('hindsight','hermes_files','mem0'))").replace(
    "CHECK(connection_id='local/hindsight')",
    "CHECK((program='hindsight' AND connection_id='local/hindsight') OR "
    "(program='hermes_files' AND connection_id='local/hermes-files') OR "
    "(program='mem0' AND connection_id='local/mem0'))").replace(
    "CHECK(mode IN ('read_only','read_write'))",
    "CHECK(mode IN ('read_only','read_write') AND (program='hindsight' OR mode='read_only'))")


def initialize_schema(db):
    """Create absent tables in the caller transaction; upgrades use an owned connection."""
    db.execute(PROFILE_SQL.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1))
    db.execute(CREATION_RECEIPTS_DDL)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def validate_memory_profile_id(profile_id, *, allow_none=False):
    if allow_none and profile_id == 'none':
        return profile_id
    if not isinstance(profile_id, str) or len(profile_id) != 36:
        raise ValueError('记忆配置编号需要标准 UUID。')
    try:
        canonical = str(uuid.UUID(profile_id))
    except ValueError:
        raise ValueError('记忆配置编号需要标准 UUID。') from None
    if canonical != profile_id:
        raise ValueError('记忆配置编号需要标准小写 UUID。')
    return profile_id


def validate_memory_profile_revision(revision):
    if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
        raise ValueError('记忆配置版本需要有效整数。')
    return revision


def _text(value, maximum, label, *, multiline=False, nonempty=False):
    allowed = (9, 10, 13) if multiline else ()
    if (not isinstance(value, str) or len(value) > maximum
            or nonempty and not value.strip()
            or any((ord(char) < 32 and ord(char) not in allowed) or ord(char) == 127
                   or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ValueError(f'{label}最多 {maximum} 个字符，不能包含无效控制字符。')
    return value.strip() if nonempty else value


def validate_memory_profile_fields(name, description='', habits='', program='hindsight',
                                   connection_id='local/hindsight', mode='read_only'):
    """Syntax/binding allowlist only, without probing a service or bank."""
    fields = dict(name=_text(name, 40, '记忆配置名称', nonempty=True),
                  description=_text(description, 200, '用途说明', multiline=True),
                  habits=_text(habits, 4000, '身份与习惯', multiline=True))
    if not isinstance(program, str) or program not in PROGRAMS:
        raise ValueError('请选择已接入的记忆程序。')
    if not isinstance(connection_id, str) or connection_id != PROGRAM_CONNECTIONS[program]:
        raise ValueError('记忆程序与本机连接不一致。')
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError('记忆使用方式需要只读取或读取并记住。')
    if program != 'hindsight' and mode != 'read_only':
        raise ValueError('这个记忆程序当前只支持只读注入；手动编辑另行明确保存。')
    return dict(fields, program=program, connection_id=connection_id, mode=mode)


def validate_memory_snapshot(snapshot):
    """Accept explicit none or exactly nine scalar fields, including revision.

    None is not an implicit none policy. The legacy absence of a whole request
    snapshot remains the caller's concern. No keys, paths, URLs, availability,
    recalled text, or write receipts can enter this configuration snapshot.
    """
    if isinstance(snapshot, dict) and snapshot == {'id': 'none'}:
        return {'id': 'none'}
    if not isinstance(snapshot, dict) or set(snapshot) != set(SNAPSHOT_FIELDS):
        raise ValueError('记忆配置快照不完整。')
    profile_id = validate_memory_profile_id(snapshot['id'])
    revision = validate_memory_profile_revision(snapshot['revision'])
    fields = validate_memory_profile_fields(**{key: snapshot[key]
        for key in ('name', 'description', 'habits', 'program', 'connection_id', 'mode')})
    bank_ref = 'mem_' + uuid.UUID(profile_id).hex
    if not isinstance(snapshot['bank_ref'], str) or snapshot['bank_ref'] != bank_ref:
        raise ValueError('记忆范围引用与配置编号不一致。')
    return dict(id=profile_id, revision=revision, **fields, bank_ref=bank_ref)


def make_memory_snapshot(profile):
    """Copy immutable intent from a stored row; omit timestamps/status extras."""
    if isinstance(profile, dict) and profile == {'id': 'none'}:
        return {'id': 'none'}
    if not isinstance(profile, dict) or any(key not in profile for key in SNAPSHOT_FIELDS[:-1]):
        raise ValueError('记忆配置记录不完整。')
    profile_id = validate_memory_profile_id(profile['id'])
    if 'bank_id' in profile and profile['bank_id'] != 'rabbit-' + profile_id:
        raise ValueError('私有记忆库编号与配置编号不一致。')
    expected_ref = 'mem_' + uuid.UUID(profile_id).hex
    if 'bank_ref' in profile and profile['bank_ref'] != expected_ref:
        raise ValueError('记忆范围引用与配置编号不一致。')
    return validate_memory_snapshot(dict({key: profile[key] for key in SNAPSHOT_FIELDS[:-1]},
                                         bank_ref=expected_ref))


class MemoryProfileConflict(ValueError):
    def __init__(self, profile):
        super().__init__('记忆配置已在另一入口更新，请先查看新版本。')
        self.profile = dict(profile)


class MemoryProfileCreationConflict(ValueError):
    def __init__(self, creation_id):
        super().__init__('这次记忆配置新建标识已有不同请求，请核对原回执；没有再新建配置。')
        self.creation_id = creation_id


def _creation_sha256(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError('记忆配置新建请求无效。') from None
    return hashlib.sha256(raw).hexdigest()


class MemoryProfiles:
    """Independent CAS profiles in the same private SQLite file as chats."""
    def __init__(self, path):
        self.path = Path(os.path.abspath(os.path.expanduser(str(path))))
        self._check_path()
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._upgrade_legacy_schema()
        with self._open(create=True) as db:
            db.execute('BEGIN IMMEDIATE')
            initialize_schema(db)
        os.chmod(self.path, 0o600)

    def _upgrade_legacy_schema(self):
        """Owned startup migration; never toggle FK enforcement in a caller transaction."""
        if not self.path.exists():
            return
        normalize = lambda sql: ''.join(sql.lower().split()).replace('ifnotexists', '').replace('"memory_profiles"', 'memory_profiles')
        with self._open() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            previous_alter = db.execute('PRAGMA legacy_alter_table').fetchone()[0]
            try:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_profiles'").fetchone()
                if row is None or normalize(row[0]) == normalize(PROFILE_SQL):
                    db.commit()
                    return
                if normalize(row[0]) != normalize(LEGACY_PROFILE_SQL):
                    raise ValueError('记忆配置原表结构无法核验，没有尝试修复。')
                if db.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('记忆配置原关联无法核验，没有升级。')
                before = list(db.execute('SELECT * FROM memory_profiles ORDER BY id'))
                schema = [tuple(r) for r in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_stat%' ORDER BY type,name")]
                children = {name: [tuple(r) for r in db.execute('SELECT * FROM ' + name + ' ORDER BY 1')]
                    for name in ('memory_profile_creation_receipts', 'native_memory_bindings')
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()}
                objects = [(kind, name, sql) for kind, name, table, sql in schema
                           if table == 'memory_profiles' and kind in ('index', 'trigger') and sql]
                db.execute(PROFILE_SQL.replace('memory_profiles', '_rabbit_memory_profiles_upgrade', 1))
                db.execute('INSERT INTO _rabbit_memory_profiles_upgrade SELECT * FROM memory_profiles')
                db.execute('DROP TABLE memory_profiles')
                # Other native guard triggers still refer to the final table name.
                # Do not rewrite those references to the temporary replacement.
                db.execute('PRAGMA legacy_alter_table=ON')
                db.execute('ALTER TABLE _rabbit_memory_profiles_upgrade RENAME TO memory_profiles')
                for _, _, sql in objects:
                    db.execute(sql)
                if [tuple(r) for r in db.execute('SELECT * FROM memory_profiles ORDER BY id')] != [tuple(r) for r in before]:
                    raise ValueError('记忆配置升级前后数据不一致。')
                after_schema = [tuple(r) for r in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_stat%' ORDER BY type,name")]
                unchanged = lambda rows: [r for r in rows if not (r[0] == 'table' and r[1] == 'memory_profiles')]
                if unchanged(schema) != unchanged(after_schema):
                    raise ValueError('记忆配置升级改变了原关联结构。')
                for name, rows in children.items():
                    if [tuple(r) for r in db.execute('SELECT * FROM ' + name + ' ORDER BY 1')] != rows:
                        raise ValueError('记忆配置升级改变了原回执或绑定。')
                if db.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('记忆配置升级关联核验未通过。')
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.execute('PRAGMA legacy_alter_table=' + str(previous_alter))
                db.execute('PRAGMA foreign_keys=ON')

    def _check_path(self):
        cloud = Path.home() / 'Library' / 'Mobile Documents'
        if self.path == cloud or cloud in self.path.parents:
            raise ValueError('记忆配置需要保存在本机聊天库，不能放进 iCloud 项目目录。')
        if '..' in self.path.parts or self.path.is_symlink():
            raise ValueError('记忆配置数据库路径无效。')
        if any(parent.is_symlink() for parent in self.path.parents):
            raise ValueError('记忆配置数据库目录不能是符号链接。')
        if self.path.exists() and not self.path.is_file():
            raise ValueError('记忆配置数据库需要普通文件。')

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
    def get_in(db, profile_id):
        """Read through the caller's sqlite3.Row connection and transaction.

        The caller supplies a validated ID and owns the current snapshot.
        This method opens no connection and never commits or rolls back.
        """
        row = db.execute('SELECT ' + ','.join(PROFILE_COLUMNS) +
                         ' FROM memory_profiles WHERE id=?', (profile_id,)).fetchone()
        if row is None:
            raise KeyError('找不到这份记忆配置。')
        profile = dict(row)
        make_memory_snapshot(profile)  # Validate stored binding; never rewrite it.
        return profile

    def get(self, profile_id):
        validate_memory_profile_id(profile_id)
        with self.lock, self._open(readonly=True) as db:
            return self.get_in(db, profile_id)

    def list(self):
        with self.lock, self._open(readonly=True) as db:
            rows = list(db.execute('SELECT ' + ','.join(PROFILE_COLUMNS) +
                ' FROM memory_profiles ORDER BY updated_at DESC,created_at DESC,id'))
            profiles = [dict(row) for row in rows]
            for profile in profiles:
                make_memory_snapshot(profile)
            return {'profiles': profiles}

    @staticmethod
    def _creation_in(db, creation_id):
        row = db.execute('SELECT * FROM memory_profile_creation_receipts WHERE creation_id=?',
                         (creation_id,)).fetchone()
        if row is None:
            return None
        try:
            profile = json.loads(row['profile_snapshot'])
            if (row['action'] not in ('create', 'copy') or type(profile) is not dict
                    or set(profile) != set(PROFILE_COLUMNS) or profile['id'] != row['result_id']
                    or type(row['request_sha256']) is not str or len(row['request_sha256']) != 64
                    or any(c not in '0123456789abcdef' for c in row['request_sha256'])):
                raise ValueError
            snapshot = make_memory_snapshot(profile)
            if snapshot['revision'] != 1 or any(snapshot[key] != profile[key] for key in SNAPSHOT_FIELDS[:-1]):
                raise ValueError
            if not all(type(profile[key]) is str for key in ('created_at', 'updated_at')):
                raise ValueError
            _creation_sha256(profile)  # Complete UTF8/finite-JSON validation.
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ValueError('记忆配置新建回执记录无效。') from None
        return {'creation_id': creation_id, 'action': row['action'],
                'request_sha256': row['request_sha256'], 'profile': profile}

    def get_creation_receipt(self, creation_id):
        validate_memory_profile_id(creation_id)
        with self.lock, self._open(readonly=True) as db:
            return self._creation_in(db, creation_id)

    @staticmethod
    def _replay_creation_in(db, creation_id, action, request_sha256):
        if creation_id is None:
            return None
        receipt = MemoryProfiles._creation_in(db, creation_id)
        if receipt is not None:
            if receipt['action'] != action or receipt['request_sha256'] != request_sha256:
                raise MemoryProfileCreationConflict(creation_id)
            return receipt['profile']
        return None

    @staticmethod
    def _record_creation_in(db, creation_id, action, request_sha256, profile):
        if creation_id is not None:
            snapshot = {key: profile[key] for key in PROFILE_COLUMNS}
            db.execute('INSERT INTO memory_profile_creation_receipts VALUES(?,?,?,?,?)',
                       (creation_id, action, request_sha256, profile['id'],
                        json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'), allow_nan=False)))

    @staticmethod
    def _insert_in(db, fields):
        if db.execute('SELECT COUNT(*) FROM memory_profiles').fetchone()[0] >= MAX_PROFILES:
            raise ValueError('记忆配置已达到 128 份上限；原配置和记忆库仍保留。')
        profile_id, stamp = str(uuid.uuid4()), _now()
        db.execute('''INSERT INTO memory_profiles(id,revision,name,description,habits,program,
            connection_id,mode,bank_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (profile_id, 1) + tuple(fields[key] for key in
                ('name', 'description', 'habits', 'program', 'connection_id', 'mode')) +
            ('rabbit-' + profile_id, stamp, stamp))
        return MemoryProfiles.get_in(db, profile_id)

    def create(self, name, description='', habits='', program='hindsight',
               connection_id='local/hindsight', mode='read_only', *, creation_id=None):
        fields = validate_memory_profile_fields(name, description, habits, program, connection_id, mode)
        if creation_id is not None:
            validate_memory_profile_id(creation_id)
        request_sha = _creation_sha256({'action': 'create', 'profile': fields}) if creation_id is not None else None
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            replay = self._replay_creation_in(db, creation_id, 'create', request_sha)
            if replay is not None:
                return replay
            profile = self._insert_in(db, fields)
            self._record_creation_in(db, creation_id, 'create', request_sha, profile)
            return profile

    def update(self, profile_id, revision, *, name=UNSET, description=UNSET,
               habits=UNSET, mode=UNSET):
        """Edit rules/display fields; binding parameters are not accepted."""
        validate_memory_profile_id(profile_id)
        validate_memory_profile_revision(revision)
        changes = dict(name=name, description=description, habits=habits, mode=mode)
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self.get_in(db, profile_id)
            if old['revision'] != revision:
                raise MemoryProfileConflict(old)
            fields = validate_memory_profile_fields(
                **{key: old[key] if value is UNSET else value for key, value in changes.items()},
                program=old['program'], connection_id=old['connection_id'])
            if all(fields[key] == old[key] for key in MUTABLE_FIELDS):
                return old
            if revision == MAX_REVISION:
                raise ValueError('这份记忆配置已达到版本上限。')
            db.execute('''UPDATE memory_profiles SET revision=revision+1,name=?,description=?,
                habits=?,mode=?,updated_at=? WHERE id=? AND revision=?''',
                tuple(fields[key] for key in MUTABLE_FIELDS) + (_now(), profile_id, revision))
            return self.get_in(db, profile_id)

    def copy(self, profile_id, revision, *, name=None, description=UNSET,
             habits=UNSET, mode=UNSET, creation_id=None):
        """Copy parameters into a NEW ID/namespace; no remote data is copied."""
        validate_memory_profile_id(profile_id)
        validate_memory_profile_revision(revision)
        changes = dict(description=description, habits=habits, mode=mode)
        if creation_id is not None:
            validate_memory_profile_id(creation_id)
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
                raise MemoryProfileConflict(old)
            fields = validate_memory_profile_fields(
                name=old['name'][:37] + ' 副本' if name is None else name,
                **{key: old[key] if value is UNSET else value for key, value in changes.items()},
                program=old['program'], connection_id=old['connection_id'])
            profile = self._insert_in(db, fields)
            self._record_creation_in(db, creation_id, 'copy', request_sha, profile)
            return profile
