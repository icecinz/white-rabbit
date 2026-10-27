"""Saved next-turn shared-memory references for ordinary native chats.

Only explicit CAS writes create this module's table in the existing chat.sqlite.
MemoryProfiles remains the parameter owner. Reads and guards use one caller
snapshot, never a memory service, native owner, transcript or browser selection.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from native_catalogue import CATALOGUE_TABLES, chat_id as validate_chat_id
from native_http_common import SpecialistHTTPError
from rabbit_memory.memory_profiles import (MemoryProfiles, MAX_REVISION,
    validate_memory_profile_id, validate_memory_profile_revision)
from rabbit_memory.memory_management import saved_memory_profile_in


AGENTS = frozenset(('codex', 'pi', 'hermes', 'claude', 'dsh'))
TABLE = 'native_chat_memory_selections'
_WORKSPACE = re.compile(r'[0-9a-f]{64}\Z')
_COLUMNS = ('workspace_id', 'agent', 'chat_id', 'revision', 'memory_profile_id', 'memory_profile_revision')


class NativeMemorySelectionCorrupt(ValueError):
    """Existing persisted selection or public configuration failed validation."""


def _scope(workspace_id, agent, chat_id):
    if type(workspace_id) is not str or not _WORKSPACE.fullmatch(workspace_id):
        raise ValueError('原生记忆选择的工作区无效。')
    if type(agent) is not str or agent not in AGENTS:
        raise ValueError('这条原生入口尚未支持保存记忆选择。')
    try:
        validate_chat_id(chat_id)
    except SpecialistHTTPError:
        raise ValueError('原生记忆选择的对话编号无效。') from None


def _revision(value, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value <= MAX_REVISION:
        raise ValueError('原生记忆选择版本需要有效整数。')
    return value


def _pair(profile_id, profile_revision):
    if profile_id is None and profile_revision is None:
        return
    if profile_id is None or profile_revision is None:
        raise ValueError('记忆配置编号与版本需要一起选择或清空。')
    validate_memory_profile_id(profile_id)
    validate_memory_profile_revision(profile_revision)


def initialize_schema(db):
    """Only the explicit selection writer owns this lazy schema transaction."""
    db.execute('''CREATE TABLE IF NOT EXISTS native_chat_memory_selections (
        workspace_id TEXT NOT NULL, agent TEXT NOT NULL, chat_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>0),
        memory_profile_id TEXT, memory_profile_revision INTEGER,
        CHECK((memory_profile_id IS NULL AND memory_profile_revision IS NULL) OR
              (memory_profile_id IS NOT NULL AND memory_profile_revision IS NOT NULL)),
        PRIMARY KEY(workspace_id,agent,chat_id))''')


def _table_exists(db):
    row = db.execute('SELECT type FROM sqlite_master WHERE name=?', (TABLE,)).fetchone()
    if row is None:
        return False
    expected = [(name, 'INTEGER' if index in (3, 5) else 'TEXT',
                 1 if index < 4 else 0, None, index + 1 if index < 3 else 0)
                for index, name in enumerate(_COLUMNS)]
    if row[0] != 'table' or [tuple(item[1:6]) for item in
            db.execute('PRAGMA table_info(' + TABLE + ')')] != expected:
        raise NativeMemorySelectionCorrupt('原生记忆选择表结构无法核验。')
    return True


def _selection_in(db, workspace_id, agent, chat_id):
    if not _table_exists(db):
        return {'revision': 0, 'memory_profile_id': None, 'memory_profile_revision': None}
    rows = db.execute('''SELECT revision,memory_profile_id,memory_profile_revision
        FROM native_chat_memory_selections WHERE workspace_id=? AND agent=? AND chat_id=? LIMIT 2''',
        (workspace_id, agent, chat_id)).fetchall()
    if not rows:
        return {'revision': 0, 'memory_profile_id': None, 'memory_profile_revision': None}
    if len(rows) != 1:
        raise NativeMemorySelectionCorrupt('原生记忆选择记录不唯一。')
    value = dict(zip(('revision', 'memory_profile_id', 'memory_profile_revision'), rows[0]))
    try:
        _revision(value['revision'], positive=True)
        _pair(value['memory_profile_id'], value['memory_profile_revision'])
    except ValueError:
        raise NativeMemorySelectionCorrupt('原生记忆选择记录无法核验。') from None
    return value


def _catalogue_in(db, agent, chat_id):
    table = CATALOGUE_TABLES[agent]
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
        raise KeyError('找不到这段原生对话。')
    if db.execute(f'SELECT 1 FROM {table} WHERE id=?', (chat_id,)).fetchone() is None:
        raise KeyError('找不到这段原生对话。')


def _profile_in(db, selection, *, requested=False):
    identity, revision = selection['memory_profile_id'], selection['memory_profile_revision']
    if identity is None:
        return None, True
    try:
        row = MemoryProfiles.get_in(db, identity)
    except KeyError:
        return None, False
    except (ValueError, TypeError, UnicodeError, sqlite3.Error):
        raise NativeMemorySelectionCorrupt('已保存的记忆配置记录无法核验。') from None
    if row['revision'] != revision:
        return None, False
    if row['mode'] != 'read_only':
        error = ValueError if requested else NativeMemorySelectionCorrupt
        raise error('原生对话的附加记忆只能使用只读配置。')
    try:
        return saved_memory_profile_in(db, row), True
    except (ValueError, TypeError, KeyError, UnicodeError, sqlite3.Error):
        raise NativeMemorySelectionCorrupt('记忆配置的只读来源无法核验。') from None


def _public_in(db, workspace_id, agent, chat_id, selection=None):
    selected = _selection_in(db, workspace_id, agent, chat_id) if selection is None else dict(selection)
    profile, current = _profile_in(db, selected)
    return {'workspace_id': workspace_id, 'agent': agent, 'chat_id': chat_id,
            'selection': selected, 'profile': profile, 'profile_current': current}


class NativeMemorySelectionConflict(ValueError):
    def __init__(self, current, message='这段原生对话的下轮记忆选择已经变化，请重新核对。'):
        super().__init__(message)
        self.current = dict(current, selection=dict(current['selection']),
                            profile=dict(current['profile']) if current['profile'] is not None else None)
        self.current_selection = dict(current['selection'])


def assert_memory_selection_in(db, workspace_id, agent, chat_id, memory_id, revision):
    """Guard a fresh freeze in its existing transaction, after immutable replay.

    Callers normalize the raw request's absent/``none`` choice to (None,None).
    Missing table/row keeps legacy behavior. This function opens no connection.
    """
    _scope(workspace_id, agent, chat_id)
    _pair(memory_id, revision)
    selected = _selection_in(db, workspace_id, agent, chat_id)
    if selected['revision'] == 0:
        return
    _catalogue_in(db, agent, chat_id)
    current = _public_in(db, workspace_id, agent, chat_id, selected)
    if (selected['memory_profile_id'], selected['memory_profile_revision']) != (memory_id, revision) or not current['profile_current']:
        raise NativeMemorySelectionConflict(current)


class NativeMemorySelections:
    """Independent next-turn memory references and revision/CAS ownership."""
    def __init__(self, path, *, workspace_id):
        if type(workspace_id) is not str or not _WORKSPACE.fullmatch(workspace_id):
            raise ValueError('原生记忆选择的工作区无效。')
        self.path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
        cloud = Path.home() / 'Library' / 'Mobile Documents'
        if (not self.path.is_file() or self.path.is_symlink() or cloud in self.path.parents
                or any(parent.is_symlink() for parent in self.path.parents)):
            raise ValueError('原生记忆选择需要现有本机聊天库。')
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

    @contextmanager
    def none_fence(self, agent, chat_id):
        """Serialize a fresh plain-text reservation with selection writes.

        While holding this context, first inspect the old native receipt. Only
        if absent, call the yielded guard and the existing local store.begin.
        No recall, material read, native startup or RPC belongs in this scope.
        This is a selection fence, not an atomic transaction across databases.
        """
        _scope(self.workspace_id, agent, chat_id)
        with self._db(write=True) as db:
            active = True

            def assert_none():
                if not active:
                    raise RuntimeError('原生记忆选择核对已经结束。')
                assert_memory_selection_in(db, self.workspace_id, agent, chat_id, None, None)

            try:
                yield assert_none
            finally:
                active = False

    def select(self, agent, chat_id, expected_revision, memory_profile_id, memory_profile_revision):
        _scope(self.workspace_id, agent, chat_id)
        _revision(expected_revision)
        _pair(memory_profile_id, memory_profile_revision)
        with self._db(write=True) as db:
            _catalogue_in(db, agent, chat_id)
            if not _table_exists(db):
                initialize_schema(db)
            old = _selection_in(db, self.workspace_id, agent, chat_id)
            if old['revision'] != expected_revision:
                raise NativeMemorySelectionConflict(_public_in(db, self.workspace_id, agent, chat_id, old))
            chosen = {'revision': old['revision'], 'memory_profile_id': memory_profile_id,
                      'memory_profile_revision': memory_profile_revision}
            _profile, current = _profile_in(db, chosen, requested=True)
            if not current:
                raise NativeMemorySelectionConflict(_public_in(db, self.workspace_id, agent, chat_id, old),
                    '所选记忆配置已更新或删除，请读取当前版本后再选择。')
            if old['revision'] and chosen == old:
                return _public_in(db, self.workspace_id, agent, chat_id, old)
            if old['revision'] == MAX_REVISION:
                raise ValueError('这段原生对话的记忆选择已达到版本上限。')
            chosen['revision'] += 1
            db.execute('''INSERT INTO native_chat_memory_selections
                (workspace_id,agent,chat_id,revision,memory_profile_id,memory_profile_revision) VALUES(?,?,?,?,?,?)
                ON CONFLICT(workspace_id,agent,chat_id) DO UPDATE SET
                    revision=excluded.revision,memory_profile_id=excluded.memory_profile_id,
                    memory_profile_revision=excluded.memory_profile_revision''',
                (self.workspace_id, agent, chat_id, chosen['revision'], memory_profile_id, memory_profile_revision))
            return _public_in(db, self.workspace_id, agent, chat_id, chosen)
