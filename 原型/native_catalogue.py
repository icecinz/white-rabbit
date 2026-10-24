"""Application chat names only, independent of every Agent protocol.

All source tables live in the existing application chat.sqlite. Catalogue
creation reserves metadata/workspace through a trusted callback; never starts
an Agent, imports its runtime, copies its body or selects a native tool policy.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading
from urllib.parse import quote
import uuid

from native_http_common import SpecialistHTTPError

MAX_CHATS = 128
CATALOGUE_TABLES = {
    'codex': 'native_chat_catalogue', 'pi': 'pi_native_chat_catalogue',
    'hermes': 'hermes_native_chat_catalogue', 'claude': 'claude_native_chat_catalogue',
    'dsh': 'dsh_native_chat_catalogue',
}
DISPLAY_NAMES_DDL = '''CREATE TABLE IF NOT EXISTS native_chat_display_names (
    agent TEXT NOT NULL, chat_id TEXT NOT NULL, title TEXT NOT NULL,
    PRIMARY KEY(agent,chat_id))'''


def _error(status, code, message, *, unsent=False):
    return SpecialistHTTPError(status, code, message, accepted=False if unsent else None)


def chat_id(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise _error(400, 'native_chat_invalid_id', '原生对话编号无效。', unsent=True) from None
    return value


def _title(value):
    if (type(value) is not str or not 1 <= len(value.strip()) <= 80
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise _error(400, 'native_chat_invalid_title', '对话名称需要1–80个字符。', unsent=True)
    return value.strip()


class NativeChatNameConflict(SpecialistHTTPError):
    def __init__(self, current_chat):
        super().__init__(409, 'native_chat_name_conflict',
                         '对话名称已经变化，请核对当前名称后再修改。', accepted=False)
        self.current_chat = current_chat

    def payload(self):
        return dict(super().payload(), current_chat=self.current_chat)


class NativeChatCatalogue:
    """Only this module's metadata table; no messages, runs or native bodies."""
    def __init__(self, path, *, agent='codex'):
        if agent not in CATALOGUE_TABLES:
            raise ValueError('原生目录只接受已接入的Agent。')
        self.agent = agent
        self.table = CATALOGUE_TABLES[agent]
        self.path = Path(path)
        self.lock = threading.RLock()
        # The Service already owns and validates this exact ChatStore path.
        if not self.path.is_absolute() or self.path.is_symlink() or not self.path.is_file():
            raise ValueError('原生对话目录需要现有的本机聊天库。')
        with self._db(write=True) as db:
            db.execute(f'''CREATE TABLE IF NOT EXISTS {self.table} (
                id TEXT PRIMARY KEY, creation_id TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL, created_at TEXT NOT NULL)''')
            # Keep the original creation intent immutable for create retries.
            # Names are app metadata, shared by the five catalogue namespaces.
            db.execute(DISPLAY_NAMES_DDL)

    @contextmanager
    def _db(self, *, write=False):
        with self.lock:
            db = sqlite3.connect('file:' + quote(str(self.path), safe='/') +
                                 ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=15)
            db.row_factory = sqlite3.Row
            try:
                db.execute('PRAGMA trusted_schema=OFF')
                if write:
                    db.execute('BEGIN IMMEDIATE')
                else:
                    db.execute('PRAGMA query_only=ON')
                with db:
                    yield db
            finally:
                db.close()

    def _public(self, row):
        title = row['display_title'] if 'display_title' in row.keys() else row['title']
        return {'id': chat_id(row['id']), 'title': _title(title),
                'agent': self.agent, 'role': 'chat', 'created_at': row['created_at']}

    def _display_select(self):
        return (f'SELECT c.*,COALESCE(n.title,c.title) AS display_title FROM {self.table} c '
                'LEFT JOIN native_chat_display_names n ON n.agent=? AND n.chat_id=c.id')

    def list(self):
        with self._db() as db:
            rows = db.execute(self._display_select() + ' ORDER BY c.created_at DESC,c.id',
                              (self.agent,)).fetchall()
        return [self._public(row) for row in rows]

    def get(self, identity):
        identity = chat_id(identity)
        with self._db() as db:
            row = db.execute(self._display_select() + ' WHERE c.id=?', (self.agent, identity)).fetchone()
        if row is None:
            raise _error(404, 'native_chat_not_found', '找不到这段原生对话。', unsent=True)
        return self._public(row)

    def rename(self, identity, expected_title, title):
        """Compare the current name and rename in one existing DB transaction.

        This is current-value CAS, without a monotonic revision or a receipt.
        A no-op confirms only this transaction's observed current name.
        """
        identity = chat_id(identity)
        _title(expected_title)  # Validate, but compare the exact supplied value.
        title = _title(title)
        with self._db(write=True) as db:
            row = db.execute(self._display_select() + ' WHERE c.id=?', (self.agent, identity)).fetchone()
            if row is None:
                raise _error(404, 'native_chat_not_found', '找不到这段原生对话。', unsent=True)
            current = self._public(row)
            if row['display_title'] != expected_title:
                raise NativeChatNameConflict(current)
            changed = row['display_title'] != title
            if changed:
                if title == row['title']:
                    db.execute('DELETE FROM native_chat_display_names WHERE agent=? AND chat_id=?',
                               (self.agent, identity))
                else:
                    db.execute('''INSERT INTO native_chat_display_names VALUES (?,?,?)
                        ON CONFLICT(agent,chat_id) DO UPDATE SET title=excluded.title''',
                               (self.agent, identity, title))
            return {'chat': dict(current, title=title), 'previous_title': row['display_title'], 'changed': changed}

    def create(self, creation_id, title, prepare, *, fixed_id=None):
        creation_id, title = chat_id(creation_id), _title(title)
        fixed_id = chat_id(fixed_id) if fixed_id is not None else None
        with self._db(write=True) as db:
            row = db.execute(f'SELECT * FROM {self.table} WHERE creation_id=?', (creation_id,)).fetchone()
            if row is not None:
                if row['title'] != title or fixed_id is not None and row['id'] != fixed_id:
                    raise _error(409, 'native_chat_creation_conflict', '这次新建已有不同名称，请先刷新列表。', unsent=True)
                result = self._public(row)
            else:
                if fixed_id is not None and db.execute(f'SELECT 1 FROM {self.table} WHERE id=?',
                                                       (fixed_id,)).fetchone() is not None:
                    raise _error(409, 'native_chat_creation_conflict', '这次新建的固定身份已有其他创建归属。', unsent=True)
                if db.execute(f'SELECT COUNT(*) FROM {self.table}').fetchone()[0] >= MAX_CHATS:
                    raise _error(409, 'native_chat_limit', '原生对话目录已达到当前上限。', unsent=True)
                result = {'id': fixed_id or str(uuid.uuid4()), 'title': title, 'agent': self.agent, 'role': 'chat',
                          'created_at': datetime.now(timezone.utc).isoformat()}
                # Native identity is reserved before publication, without RPC.
                # A trusted fixed ID lets a failed publication retry the same
                # prepared identity. Default creation retains its old contract.
                prepare(result['id'])
                db.execute(f'INSERT INTO {self.table} VALUES (?,?,?,?)',
                           (result['id'], creation_id, title, result['created_at']))
        return result

