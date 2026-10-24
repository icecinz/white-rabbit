"""Next-turn saved-profile references for native application chats.

This metadata belongs to chat.sqlite; profiles remain the sole parameter store.
Construction and reads never create a table. Only an explicit selection write
adds this module's one table. No owner, Agent, transcript or RPC is involved.

Native intent freezing checks the reference in its existing read and final
write transactions. Existing frozen intents replay before this check. A clear
selection is a durable managed row, not a return to legacy browser authority.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from native_catalogue import CATALOGUE_TABLES, chat_id as validate_chat_id
from native_http_common import SpecialistHTTPError
from profiles import (AIProfiles, MAX_REVISION, make_profile_snapshot,
                      validate_profile_id, validate_profile_revision)


AGENTS = frozenset(('codex', 'pi', 'hermes'))
TABLE = 'native_chat_profile_selections'
_WORKSPACE = re.compile(r'[0-9a-f]{64}\Z')


class NativeChatSelectionCorrupt(ValueError):
    """Existing persisted metadata failed its bounded decoder."""


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


def _profile_in(db, agent, selection, *, requested=False):
    identity, revision = selection['ai_profile_id'], selection['ai_profile_revision']
    if identity is None:
        return None, True
    try:
        row = AIProfiles.get_in(db, identity)
    except KeyError:
        return None, False
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
    profile, current = _profile_in(db, agent, selection)
    return {'workspace_id': workspace_id, 'agent': agent, 'chat_id': chat_id,
            'selection': selection, 'profile': profile, 'profile_current': current}


class NativeChatSelectionConflict(ValueError):
    def __init__(self, current, message='这段原生对话的下轮配置已经变化，请重新核对。'):
        super().__init__(message)
        self.current = dict(current, selection=dict(current['selection']),
                            profile=dict(current['profile']) if current['profile'] is not None else None)
        self.current_selection = dict(current['selection'])


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
    """One app-local table, explicit CAS writes and read-only observations."""
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
            _profile, current = _profile_in(db, agent, chosen, requested=True)
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
