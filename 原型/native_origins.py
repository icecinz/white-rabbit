"""One immutable native continuation source per target chat; metadata only.

The service verifies catalogue ownership. This store keeps explicit references
in the existing application SQLite file, never messages or native Agent state.
"""
import json
import re
import sqlite3
import uuid

from rabbit_preparation.material_references import validate_material_refs as validate_refs


PATH = '/api/management/native-chat-origin'
SCOPE = 'native_continuation_source'
AGENTS = {'codex': 'native_chats', 'hermes': 'hermes_native_chats'}
FIELDS = ('target_agent', 'target_chat_id', 'creation_id', 'source_view',
          'source_revision', 'selected_message_ids', 'material_refs', 'snapshot_sha256')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_MESSAGES = {
    'invalid': (400, '接续来源的范围或版本无效。'),
    'not_found': (404, '找不到指定的原生对话。'),
    'creation_mismatch': (409, '接续目标与原创建编号不一致，没有保存来源。'),
    'conflict': (409, '这段对话已经保存了不同的接续来源，没有覆盖。'),
    'corrupt': (409, '已保存的接续来源需要核对，没有替换。'),
    'unavailable': (503, '所选原生对话入口暂时不可用。'),
    'storage_unconfirmed': (503, '接续来源的保存尚未确认，请按原目标读取核对。'),
}


class NativeOriginError(ValueError):
    def __init__(self, kind='invalid'):
        self.status, message = _MESSAGES[kind]
        self.code = 'native_chat_origin_' + kind
        self.accepted = None if kind == 'storage_unconfirmed' else False
        super().__init__(message)


def _text(value, maximum):
    if type(value) is not str or not 0 < len(value) <= maximum or '\0' in value:
        raise NativeOriginError()
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise NativeOriginError() from None
    return value


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise NativeOriginError() from None
    return value


def target(agent, chat_id):
    if type(agent) is not str or agent not in AGENTS:
        raise NativeOriginError()
    return agent, _uuid(chat_id)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def record(value, workspace_id):
    """Copy the exact bounded declaration, without verifying its body claims."""
    try:
        if type(value) is not dict or set(value) != set(FIELDS):
            raise NativeOriginError()
        target(value['target_agent'], value['target_chat_id']); _uuid(value['creation_id'])
        source = value['source_view']
        if (type(source) is not dict or set(source) != {'workspace_id', 'agent', 'chat_id', 'title'}
                or source['workspace_id'] != workspace_id):
            raise NativeOriginError()
        target(source['agent'], source['chat_id']); _text(source['title'], 16384)
        for key in ('source_revision', 'snapshot_sha256'):
            if type(value[key]) is not str or _SHA.fullmatch(value[key]) is None:
                raise NativeOriginError()
        selected = value['selected_message_ids']
        if type(selected) is not list or len(selected) > 50:
            raise NativeOriginError()
        for identity in selected:
            _text(identity, 16384)
        if len(set(selected)) != len(selected):
            raise NativeOriginError()
        refs = validate_refs(value['material_refs'])
        if not selected and not refs:
            raise NativeOriginError()
        raw = _json(dict(value, material_refs=refs))
        if len(raw.encode('utf-8')) > 8192:
            raise NativeOriginError()
        return json.loads(raw)
    except NativeOriginError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise NativeOriginError() from None


class NativeOrigins:
    def __init__(self, store, workspace_id):
        if type(workspace_id) is not str or _SHA.fullmatch(workspace_id) is None:
            raise NativeOriginError()
        self.store, self.workspace_id = store, workspace_id
        with store.lock, store._open() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS native_continuation_sources (
                workspace_id TEXT NOT NULL, target_agent TEXT NOT NULL,
                target_chat_id TEXT NOT NULL, record_json TEXT NOT NULL,
                PRIMARY KEY(workspace_id,target_agent,target_chat_id))''')

    def _read_in(self, db, agent, chat_id):
        row = db.execute('SELECT record_json FROM native_continuation_sources '
            'WHERE workspace_id=? AND target_agent=? AND target_chat_id=?',
            (self.workspace_id, agent, chat_id)).fetchone()
        if row is None:
            return None
        try:
            raw = row['record_json']
            value = record(json.loads(raw), self.workspace_id)
            if (value['target_agent'] != agent or value['target_chat_id'] != chat_id
                    or _json(value) != raw):
                raise NativeOriginError()
            return value
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise NativeOriginError('corrupt') from None

    def read(self, agent, chat_id):
        target(agent, chat_id)
        try:
            with self.store.lock, self.store._open(readonly=True) as db:
                return self._read_in(db, agent, chat_id)
        except (sqlite3.Error, OSError):
            raise NativeOriginError('storage_unconfirmed') from None

    def save(self, value):
        value = record(value, self.workspace_id)
        agent, chat_id = value['target_agent'], value['target_chat_id']
        try:
            with self.store.lock, self.store._open() as db:
                db.execute('BEGIN IMMEDIATE')
                previous = self._read_in(db, agent, chat_id)
                if previous is not None:
                    if previous != value:
                        raise NativeOriginError('conflict')
                    return previous
                db.execute('INSERT INTO native_continuation_sources VALUES(?,?,?,?)',
                    (self.workspace_id, agent, chat_id, _json(value)))
            return value
        except (sqlite3.Error, OSError):
            raise NativeOriginError('storage_unconfirmed') from None
