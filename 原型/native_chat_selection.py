"""Next-turn saved-profile references for native application chats.

This metadata belongs to chat.sqlite. Named parameters remain in ai_profiles;
an explicit local save adds one owned Codex configuration per target. Reads and
construction never create tables. No owner, Agent, transcript or RPC is involved.

Native intent freezing checks the reference in its existing read and final
write transactions. Existing frozen intents replay before this check. A clear
selection is a durable managed row, not a return to legacy browser authority.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from native_catalogue import CATALOGUE_TABLES, chat_id as validate_chat_id
from native_http_common import SpecialistHTTPError
from profiles import (AIProfiles, MAX_REVISION, PROFILE_COLUMNS, PROFILE_FIELDS,
                      LOCAL_CONFIGURATION_TABLE, allocate_profile_id_in,
                      make_profile_snapshot, validate_profile_fields,
                      validate_profile_id, validate_profile_revision)


AGENTS = frozenset(('codex', 'pi', 'hermes', 'claude'))
TABLE = 'native_chat_profile_selections'
_WORKSPACE = re.compile(r'[0-9a-f]{64}\Z')
_LOCAL_COLUMNS = ('workspace_id', 'agent', 'chat_id') + PROFILE_COLUMNS + (
    'operation_id', 'request_sha256', 'selection_revision')
_LOCAL_NAME = '本对话设置'


class NativeChatSelectionCorrupt(ValueError):
    """Existing persisted metadata failed its bounded decoder."""


class NativeChatLocalScopeError(ValueError):
    """A local parameter identity was requested outside its owning target."""


def _scope(workspace_id, agent, chat_id):
    if type(workspace_id) is not str or not _WORKSPACE.fullmatch(workspace_id):
        raise ValueError('原生配置选择的工作区无效。')
    if type(agent) is not str or agent not in AGENTS:
        raise ValueError('这条原生入口尚未支持保存配置选择。')
    try:
        validate_chat_id(chat_id)
    except SpecialistHTTPError:
        # The catalogue codec raises an HTTP error; this storage interface is
        # deliberately independent of the owning HTTP response mapping.
        raise ValueError('原生配置选择的对话编号无效。') from None


def _revision(value, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value <= MAX_REVISION:
        raise ValueError('原生配置选择版本需要有效整数。')
    return value


def _pair(profile_id, profile_revision):
    if profile_id is None and profile_revision is None:
        return
    if profile_id is None or profile_revision is None:
        raise ValueError('AI 配置编号与版本需要一起选择或清空。')
    validate_profile_id(profile_id)
    validate_profile_revision(profile_revision)


def initialize_schema(db):
    """Participate in the explicit caller-owned selection transaction only."""
    db.execute('''CREATE TABLE IF NOT EXISTS native_chat_profile_selections (
        workspace_id TEXT NOT NULL, agent TEXT NOT NULL, chat_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>0),
        ai_profile_id TEXT, ai_profile_revision INTEGER,
        CHECK((ai_profile_id IS NULL AND ai_profile_revision IS NULL) OR
              (ai_profile_id IS NOT NULL AND ai_profile_revision IS NOT NULL)),
        PRIMARY KEY(workspace_id,agent,chat_id))''')


def _table_exists(db):
    row = db.execute('SELECT type FROM sqlite_master WHERE name=?', (TABLE,)).fetchone()
    if row is None:
        return False
    if row[0] != 'table':
        raise NativeChatSelectionCorrupt('原生配置选择表无法核验。')
    columns = [item[1] for item in db.execute('PRAGMA table_info(' + TABLE + ')')]
    if columns != ['workspace_id', 'agent', 'chat_id', 'revision', 'ai_profile_id', 'ai_profile_revision']:
        raise NativeChatSelectionCorrupt('原生配置选择表结构无法核验。')
    return True


def _selection_in(db, workspace_id, agent, chat_id):
    if not _table_exists(db):
        return {'revision': 0, 'ai_profile_id': None, 'ai_profile_revision': None}
    rows = db.execute('''SELECT revision,ai_profile_id,ai_profile_revision
        FROM native_chat_profile_selections WHERE workspace_id=? AND agent=? AND chat_id=? LIMIT 2''',
        (workspace_id, agent, chat_id)).fetchall()
    if not rows:
        return {'revision': 0, 'ai_profile_id': None, 'ai_profile_revision': None}
    if len(rows) != 1:
        raise NativeChatSelectionCorrupt('原生配置选择记录不唯一。')
    value = dict(zip(('revision', 'ai_profile_id', 'ai_profile_revision'), rows[0]))
    try:
        _revision(value['revision'], positive=True)
        _pair(value['ai_profile_id'], value['ai_profile_revision'])
    except ValueError:
        raise NativeChatSelectionCorrupt('原生配置选择记录无法核验。') from None
    return value


def _catalogue_in(db, agent, chat_id):
    # Identifiers come only from this constant mapping, never caller SQL.
    table = CATALOGUE_TABLES[agent]
    if db.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone() is None:
        raise KeyError('找不到这段原生对话。')
    if db.execute(f'SELECT 1 FROM {table} WHERE id=?', (chat_id,)).fetchone() is None:
        raise KeyError('找不到这段原生对话。')


def _local_table_exists(db):
    row = db.execute('SELECT type FROM sqlite_master WHERE name=?',
                     (LOCAL_CONFIGURATION_TABLE,)).fetchone()
    if row is None:
        return False
    if row[0] != 'table' or tuple(item[1] for item in db.execute(
            'PRAGMA table_info(native_chat_local_configurations)')) != _LOCAL_COLUMNS:
        raise NativeChatSelectionCorrupt('本对话配置表结构无法核验。')
    return True


def _initialize_local_schema(db):
    if _local_table_exists(db):
        return
    db.execute('''CREATE TABLE native_chat_local_configurations (
        workspace_id TEXT NOT NULL, agent TEXT NOT NULL CHECK(agent='codex'), chat_id TEXT NOT NULL,
        id TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL CHECK(revision>0),
        name TEXT NOT NULL, agent_instance_id TEXT NOT NULL, device_id TEXT NOT NULL,
        model_id TEXT NOT NULL, reasoning_effort TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        operation_id TEXT NOT NULL, request_sha256 TEXT NOT NULL,
        selection_revision INTEGER NOT NULL CHECK(selection_revision>0),
        PRIMARY KEY(workspace_id,agent,chat_id))''')


def _decode_local(row):
    try:
        value = dict(zip(_LOCAL_COLUMNS, row))
        _scope(value['workspace_id'], value['agent'], value['chat_id'])
        if (value['agent'] != 'codex' or value['agent_instance_id'] != 'local/codex'
                or value['device_id'] != 'local' or value['name'] != _LOCAL_NAME):
            raise ValueError
        validate_profile_id(value['id']); validate_profile_revision(value['revision'])
        validate_profile_id(value['operation_id']); _revision(value['selection_revision'], positive=True)
        fields = {key: value[key] for key in PROFILE_FIELDS}
        if validate_profile_fields(**fields) != fields:
            raise ValueError
        if type(value['request_sha256']) is not str or not _WORKSPACE.fullmatch(value['request_sha256']):
            raise ValueError
        if any(type(value[key]) is not str or not 1 <= len(value[key]) <= 64
               for key in ('created_at', 'updated_at')):
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise NativeChatSelectionCorrupt('本对话配置记录无法核验。') from None


def _local_in(db, *, identity=None, target=None):
    if not _local_table_exists(db):
        return None
    predicate, values = ('id=?', (identity,)) if target is None else (
        'workspace_id=? AND agent=? AND chat_id=?', target)
    rows = db.execute('SELECT ' + ','.join(_LOCAL_COLUMNS) +
        ' FROM native_chat_local_configurations WHERE ' + predicate + ' LIMIT 2', values).fetchall()
    if len(rows) > 1:
        raise NativeChatSelectionCorrupt('本对话配置记录不唯一。')
    return _decode_local(tuple(rows[0])) if rows else None


def resolve_profile_in(db, workspace_id, agent, chat_id, profile_id):
    """Read named or target-owned parameters in the caller's transaction only.

    Named records preserve legacy scope validation at their callers. A local
    record requires this exact Codex target, even when its selection is absent.
    Neither missing nor out-of-scope identities fall back to native defaults.
    """
    _scope(workspace_id, agent, chat_id)
    validate_profile_id(profile_id)
    local = _local_in(db, identity=profile_id)
    try:
        named = AIProfiles.get_in(db, profile_id)
    except KeyError:
        named = None
    if local is not None and named is not None:
        raise NativeChatSelectionCorrupt('命名配置与本对话配置的编号重复。')
    if local is not None:
        if agent != 'codex' or (local['workspace_id'], local['agent'], local['chat_id']) != (
                workspace_id, agent, chat_id):
            raise NativeChatLocalScopeError('这份本对话配置不属于指定的原生对话。')
        _catalogue_in(db, agent, chat_id)
        return {key: local[key] for key in PROFILE_COLUMNS}
    if named is None:
        raise KeyError('找不到这份 AI 配置。')
    return named


def _profile_in(db, workspace_id, agent, chat_id, selection, *, requested=False):
    identity, revision = selection['ai_profile_id'], selection['ai_profile_revision']
    if identity is None:
        return None, True
    try:
        row = resolve_profile_in(db, workspace_id, agent, chat_id, identity)
    except KeyError:
        return None, False
    except NativeChatLocalScopeError:
        error = ValueError if requested else NativeChatSelectionCorrupt
        raise error('本对话配置与目标范围不一致。') from None
    if row['revision'] != revision:
        return None, False
    try:
        profile = make_profile_snapshot(row)['ai_profile']
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise NativeChatSelectionCorrupt('已保存的 AI 配置记录无法核验。') from None
    if profile['agent_instance_id'] != 'local/' + agent or profile['device_id'] != 'local':
        error = ValueError if requested else NativeChatSelectionCorrupt
        raise error('原生配置选择与 Agent 范围不一致。')
    return profile, True


def _public_in(db, workspace_id, agent, chat_id, selection=None):
    selection = (_selection_in(db, workspace_id, agent, chat_id)
                 if selection is None else dict(selection))
    profile, current = _profile_in(db, workspace_id, agent, chat_id, selection)
    return {'workspace_id': workspace_id, 'agent': agent, 'chat_id': chat_id,
            'selection': selection, 'profile': profile, 'profile_current': current}


class NativeChatSelectionConflict(ValueError):
    def __init__(self, current, message='这段原生对话的下轮配置已经变化，请重新核对。'):
        super().__init__(message)
        self.current = dict(current, selection=dict(current['selection']),
                            profile=dict(current['profile']) if current['profile'] is not None else None)
        self.current_selection = dict(current['selection'])


def _local_operation_result(value, operation_id, digest, *, replayed):
    return dict(value, operation={'id': operation_id, 'request_sha256': digest,
                                  'replayed': replayed})


def _replay_local_in(db, workspace_id, agent, chat_id, local, operation_id, digest):
    if local is None or local['operation_id'] != operation_id:
        return None
    current = _public_in(db, workspace_id, agent, chat_id)
    if local['request_sha256'] != digest:
        raise NativeChatSelectionConflict(current, '这次调整编号已用于不同内容；没有覆盖原设置。')
    expected = {'revision': local['selection_revision'], 'ai_profile_id': local['id'],
                'ai_profile_revision': local['revision']}
    if current['selection'] != expected or not current['profile_current']:
        raise NativeChatSelectionConflict(current, '这次调整已被后续选择取代；请重新核对当前设置。')
    return _local_operation_result(current, operation_id, digest, replayed=True)


def assert_selection_in(db, workspace_id, agent, chat_id, profile_id, profile_revision):
    """Check the managed choice in the caller's existing SQLite transaction.

    Missing table/row retains legacy behavior. Do not call this before replaying
    an existing immutable intent, and do not open a second DB connection here.
    """
    _scope(workspace_id, agent, chat_id)
    _pair(profile_id, profile_revision)
    selected = _selection_in(db, workspace_id, agent, chat_id)
    if selected['revision'] == 0:
        return
    _catalogue_in(db, agent, chat_id)
    if (selected['ai_profile_id'], selected['ai_profile_revision']) != (profile_id, profile_revision):
        raise NativeChatSelectionConflict(_public_in(db, workspace_id, agent, chat_id, selected))


class NativeChatSelections:
    """Target selections and optional owned parameters, with explicit CAS writes."""
    def __init__(self, path, *, workspace_id):
        if type(workspace_id) is not str or not _WORKSPACE.fullmatch(workspace_id):
            raise ValueError('原生配置选择的工作区无效。')
        self.path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
        cloud = Path.home() / 'Library' / 'Mobile Documents'
        if (not self.path.is_file() or self.path.is_symlink() or cloud in self.path.parents
                or any(parent.is_symlink() for parent in self.path.parents)):
            raise ValueError('原生配置选择需要现有本机聊天库。')
        self.workspace_id = workspace_id

    @contextmanager
    def _db(self, *, write=False):
        db = sqlite3.connect('file:' + quote(str(self.path), safe='/') +
                             ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA trusted_schema=OFF')
            if not write:
                db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            with db:
                yield db
        finally:
            db.close()

    def read(self, agent, chat_id):
        _scope(self.workspace_id, agent, chat_id)
        with self._db() as db:
            _catalogue_in(db, agent, chat_id)
            return _public_in(db, self.workspace_id, agent, chat_id)

    def select(self, agent, chat_id, expected_revision, ai_profile_id, ai_profile_revision):
        _scope(self.workspace_id, agent, chat_id)
        _revision(expected_revision)
        _pair(ai_profile_id, ai_profile_revision)
        with self._db(write=True) as db:
            _catalogue_in(db, agent, chat_id)
            initialize_schema(db)
            old = _selection_in(db, self.workspace_id, agent, chat_id)
            if old['revision'] != expected_revision:
                raise NativeChatSelectionConflict(_public_in(db, self.workspace_id, agent, chat_id, old))
            chosen = {'revision': old['revision'], 'ai_profile_id': ai_profile_id,
                      'ai_profile_revision': ai_profile_revision}
            _profile, current = _profile_in(db, self.workspace_id, agent, chat_id, chosen, requested=True)
            if not current:
                raise NativeChatSelectionConflict(_public_in(db, self.workspace_id, agent, chat_id, old),
                    '所选 AI 配置已更新或删除，请读取当前版本后再选择。')
            if old['revision'] and chosen == old:
                return _public_in(db, self.workspace_id, agent, chat_id, old)
            if old['revision'] == MAX_REVISION:
                raise ValueError('这段原生对话的配置选择已达到版本上限。')
            chosen['revision'] += 1
            db.execute('''INSERT INTO native_chat_profile_selections
                (workspace_id,agent,chat_id,revision,ai_profile_id,ai_profile_revision) VALUES(?,?,?,?,?,?)
                ON CONFLICT(workspace_id,agent,chat_id) DO UPDATE SET
                    revision=excluded.revision,ai_profile_id=excluded.ai_profile_id,
                    ai_profile_revision=excluded.ai_profile_revision''',
                (self.workspace_id, agent, chat_id, chosen['revision'], ai_profile_id, ai_profile_revision))
            return _public_in(db, self.workspace_id, agent, chat_id, chosen)

    def _local_base_in(self, db, agent, chat_id, expected_revision, profile_id, profile_revision):
        current = _public_in(db, self.workspace_id, agent, chat_id)
        selected = current['selection']
        if (selected['revision'] != expected_revision or selected['revision'] == 0
                or (selected['ai_profile_id'], selected['ai_profile_revision']) != (profile_id, profile_revision)
                or not current['profile_current'] or current['profile'] is None):
            raise NativeChatSelectionConflict(current,
                '请从本对话当前已选的有效配置调整；原生默认或已变化的配置不能作为基值。')
        return current['profile']

    def save_local_configuration(self, agent, chat_id, expected_revision,
                                 base_profile_id, base_profile_revision,
                                 parameters, operation_id, *, validator):
        """Atomically replace this target's own parameters and next-turn choice.

        Only model/effort are editable here. The already-selected base supplies
        execution identity; a named selection exits any previous local values.
        Validation runs without a database transaction. Distinct operations
        advance both revisions; the latest exact replay never revalidates or
        writes. Once superseded, its old CAS cannot restore the old setting.
        """
        _scope(self.workspace_id, agent, chat_id)
        if agent != 'codex':
            raise ValueError('当前仅支持普通 Codex 对话的局部设置。')
        _revision(expected_revision)
        validate_profile_id(base_profile_id); validate_profile_revision(base_profile_revision)
        validate_profile_id(operation_id)
        if type(parameters) is not dict or set(parameters) != {'model_id', 'reasoning_effort'}:
            raise ValueError('本对话设置只接受完整的模型和强度参数。')
        if not callable(validator):
            raise ValueError('本对话设置需要原生模型能力核验。')
        # Detach the caller's parameters before the external validator can run.
        fields = validate_profile_fields(_LOCAL_NAME, 'local/codex', 'local',
                                          parameters['model_id'], parameters['reasoning_effort'])
        request = {'agent': agent, 'chat_id': chat_id,
                   'expected_revision': expected_revision, 'base_profile_id': base_profile_id,
                   'base_profile_revision': base_profile_revision,
                   'parameters': {key: fields[key] for key in ('model_id', 'reasoning_effort')},
                   'operation_id': operation_id}
        digest = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
        target = (self.workspace_id, agent, chat_id)
        with self._db() as db:
            _catalogue_in(db, agent, chat_id)
            local = _local_in(db, target=target)
            replay = _replay_local_in(db, *target, local, operation_id, digest)
            if replay is not None:
                return replay
            base = self._local_base_in(db, agent, chat_id, expected_revision,
                                       base_profile_id, base_profile_revision)
            fields = validate_profile_fields(_LOCAL_NAME, base['agent_instance_id'], base['device_id'],
                                              fields['model_id'], fields['reasoning_effort'])
        outcome = validator(dict(fields))
        if outcome is False:
            raise ValueError('所选原生模型和强度组合不受支持；原设置保留。')
        if outcome is not None and outcome is not True:
            raise ValueError('原生能力核验需要明确通过或拒绝。')
        with self._db(write=True) as db:
            _catalogue_in(db, agent, chat_id)
            now_local = _local_in(db, target=target)
            replay = _replay_local_in(db, *target, now_local, operation_id, digest)
            if replay is not None:
                return replay
            current_base = self._local_base_in(db, agent, chat_id, expected_revision,
                                               base_profile_id, base_profile_revision)
            if current_base != base or now_local != local:
                raise NativeChatSelectionConflict(_public_in(db, *target),
                    '核验期间配置参数已经变化；原设置保留，请重新读取。')
            if expected_revision == MAX_REVISION or local is not None and local['revision'] == MAX_REVISION:
                raise ValueError('本对话配置已达到版本上限；原设置保留。')
            _initialize_local_schema(db)
            identity = allocate_profile_id_in(db) if local is None else local['id']
            # A former local row may coexist with a selected named base. Check
            # its ID as well before reusing it, not only the selected base ID.
            if local is not None:
                resolve_profile_in(db, *target, identity)
            stamp = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
            revision = 1 if local is None else local['revision'] + 1
            row = dict(zip(('workspace_id', 'agent', 'chat_id'), target), id=identity,
                       revision=revision, **fields, created_at=local['created_at'] if local else stamp,
                       updated_at=stamp, operation_id=operation_id, request_sha256=digest,
                       selection_revision=expected_revision + 1)
            changes = [key for key in _LOCAL_COLUMNS if key not in (
                'workspace_id', 'agent', 'chat_id', 'id', 'created_at')]
            db.execute('INSERT INTO native_chat_local_configurations (' + ','.join(_LOCAL_COLUMNS) +
                ') VALUES(' + ','.join('?' for _ in _LOCAL_COLUMNS) +
                ') ON CONFLICT(workspace_id,agent,chat_id) DO UPDATE SET ' +
                ','.join(key + '=excluded.' + key for key in changes),
                tuple(row[key] for key in _LOCAL_COLUMNS))
            chosen = {'revision': expected_revision + 1, 'ai_profile_id': identity,
                      'ai_profile_revision': revision}
            changed = db.execute('''UPDATE native_chat_profile_selections SET revision=?,ai_profile_id=?,
                ai_profile_revision=? WHERE workspace_id=? AND agent=? AND chat_id=? AND revision=?''',
                (chosen['revision'], identity, revision) + target + (expected_revision,)).rowcount
            if changed != 1:
                raise NativeChatSelectionCorrupt('本对话选择未能原子更新；原设置保留。')
            return _local_operation_result(_public_in(db, *target, chosen), operation_id, digest, replayed=False)
