"""Private, durable shared conversations, independent of native agent sessions."""
import datetime as dt
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import quote
import uuid

from preferences import validate_model_id
from model_catalog import valid_model
from rabbit_content.file_intent import validate_intent, validate_inputs
from profiles import (AIProfiles, ProfileConflict, UNSET, MAX_REVISION, make_profile_snapshot,
                      validate_request_snapshot, validate_profile_id, REASONING_EFFORTS,
                      initialize_schema as initialize_profile_schema)
from rabbit_memory.memory_profiles import (MemoryProfiles, MemoryProfileConflict, make_memory_snapshot,
    validate_memory_profile_id, initialize_schema as initialize_memory_profile_schema)
from memory_context import validate_memory_inputs, memory_query, public_recall_info
from rabbit_memory.write_jobs import (enqueue_completed_in, public_state as public_memory_job_state,
                        initialize_schema as initialize_memory_job_schema)


ACTIVE = ('queued', 'running')
MAX_CONTEXT_PINS = 20
from rabbit_content.attachments import (MAX_MESSAGE_IMAGES, MAX_HISTORY_IMAGES,
    MAX_NATIVE_ATTACHMENTS, NATIVE_SOURCE_FIELDS, validate_attachment_ids, validate_native_attachments)


def _native_ids(records):
    return list(dict.fromkeys(item['attachment_id'] for item in records
                              if item['material_state'] == 'ready'))


def _source_record(message, *, imported=False):
    native = validate_native_attachments(message.get('native_attachments'))
    record = {'role': message['role'], 'content': message.get('content', ''),
              'phase': message.get('phase') or '',
              'provenance': {key: (message.get('provenance') or {}).get(key) if imported else message.get(key)
                             for key in ('agent', 'session_id', 'message_id', 'timestamp', 'source_path', 'source_line')}}
    if native:
        record['native_attachments'] = [{key: item[key] for key in NATIVE_SOURCE_FIELDS} for item in native]
    else:
        ids = validate_attachment_ids(message.get('attachment_ids'), maximum=MAX_HISTORY_IMAGES)
        if ids:
            record['attachment_ids'] = ids
    return record


def _source_imported(source, messages):
    return [m for m in messages if m.get('status') == 'imported' and isinstance(m.get('provenance'), dict)
            and m['provenance'].get('agent') == source.get('agent')
            and m['provenance'].get('session_id') == source.get('session_id')]


def _version(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


class BusyError(ValueError):
    pass


class ContextSelectionConflict(BusyError):
    def __init__(self, selection):
        super().__init__('必带前文已在另一入口修改，请先查看最新选择。')
        self.context_selection = selection


class ProfileSelectionConflict(BusyError):
    def __init__(self, selection):
        super().__init__('这段聊天的 AI 或记忆选择已在另一入口修改，请先查看新版本。')
        self.profile_selection = dict(selection)


class SourceUpdateConflict(BusyError):
    """A reviewed source tail or shared-chat snapshot is no longer current."""


def source_projection(messages):
    """Only native fields preserved by importing participate in equality/hash.

    Display cleanup, index row ordinals, shared UUIDs and import dates are not
    native message identity. Content remains the complete indexed original.
    """
    return [_source_record(message) for message in messages if message.get('role') in ('user', 'assistant')]


def source_update_comparison(source, messages, indexed):
    native = source_projection(indexed)
    imported = [_source_record(message, imported=True) for message in _source_imported(source, messages)]
    version = _version(native)
    prefix_matches = len(imported) <= len(native) and imported == native[:len(imported)]
    new_count = len(native) - len(imported) if prefix_matches else 0
    return {'status': ('appendable' if new_count else 'unchanged') if prefix_matches else 'diverged',
            'source_version': version, 'indexed_messages': len(native),
            'imported_messages': len(imported), 'new_messages': new_count}


def imported_message(message, message_id, fallback_timestamp=None):
    """Project one native record identically for storage and ephemeral preview.

    A preview supplies a source-derived identifier; persisted chats supply an
    independent UUID. Neither operation replaces native message identifiers.
    """
    if message.get('role') not in ('user', 'assistant'):
        return None
    provenance = {key: message.get(key) for key in
                  ('agent', 'session_id', 'message_id', 'timestamp', 'source_path', 'source_line')}
    native = validate_native_attachments(message.get('native_attachments'))
    ids = validate_attachment_ids(message.get('attachment_ids', _native_ids(native)), maximum=MAX_HISTORY_IMAGES)
    if native and ids != _native_ids(native):
        raise ValueError('历史图片引用和原生位置不一致。')
    return {'id': message_id, 'role': message['role'], 'content': message.get('content', ''),
            'created_at': message.get('timestamp') or fallback_timestamp,
            'backend': message.get('agent'), 'status': 'imported', 'provenance': provenance,
            'phase': message.get('phase') or '',
            'attachment_ids': ids, 'native_attachments': native,
            'source_anchor': message.get('source_anchor')}


def source_image_backfill_plan(source, messages, indexed, *, chat_revision=None):
    """Review missing image positions independently of normal source-tail appends.

    Existing textual records must be a complete unchanged native text prefix.
    Stable native descriptors cannot be replaced by this operation. Materials
    can be repaired without changing their source fingerprint or old columns.
    """
    plan = {'status': 'unchanged', 'chat_revision': int(chat_revision) if chat_revision is not None else None,
            'material_updates': [], 'new_image_messages': [],
            'counts': {'backfilled_messages': 0, 'backfilled_images': 0, 'inserted_image_messages': 0},
            'notice': '已导入前文没有待补入的原生图片。'}
    imported = _source_imported(source, messages)
    native = [m for m in indexed if m.get('role') in ('user', 'assistant')]
    def text_only(record):
        value = dict(record)
        value.pop('native_attachments', None)
        value.pop('attachment_ids', None)
        return value
    old_text = [text_only(_source_record(m, imported=True)) for m in imported if (m.get('content') or '').strip()]
    new_text = [text_only(_source_record(m)) for m in native if (m.get('content') or '').strip()]
    old_ids = [(m.get('provenance') or {}).get('message_id') for m in imported]
    new_ids = [m.get('message_id') for m in native]
    malformed = (any(not isinstance(i, str) or not i or len(i) > 256 for i in old_ids + new_ids)
                 or len(set(old_ids)) != len(old_ids) or len(set(new_ids)) != len(new_ids)
                 or any(m.get('agent') != source.get('agent') or m.get('session_id') != source.get('session_id') for m in native))
    if malformed or len(old_text) > len(new_text) or old_text != new_text[:len(old_text)]:
        plan.update(status='diverged', notice='原生文字已删除、改动或重新排序，不能自动补入图片。')
    else:
        old_by_id = dict(zip(old_ids, imported))
        positions = {identity: i for i, identity in enumerate(new_ids)}
        existing_positions = [positions.get(identity) for identity in old_ids]
        if (any(i is None for i in existing_positions)
                or existing_positions != sorted(existing_positions)):
            plan.update(status='diverged', notice='原生消息身份或顺序已变化，不能自动补入图片。')
        else:
            last = max(existing_positions, default=-1)
            # A fresh ordinary tail stays a single tail operation, including
            # pictures interleaved in it. Image-only trailing gaps can be fixed.
            has_new_text_tail = any((m.get('content') or '').strip() for m in native[last + 1:])
            for position, message in enumerate(native):
                identity = message['message_id']
                old = old_by_id.get(identity)
                descriptions = validate_native_attachments(message.get('native_attachments'))
                if old is not None:
                    if text_only(_source_record(old, imported=True)) != text_only(_source_record(message)):
                        plan.update(status='diverged', notice='原生消息已有改动，不能自动覆盖已有分支。')
                        break
                    previous = validate_native_attachments(old.get('native_attachments'))
                    if previous and ([{k: i[k] for k in NATIVE_SOURCE_FIELDS} for i in previous] !=
                                     [{k: i[k] for k in NATIVE_SOURCE_FIELDS} for i in descriptions]):
                        plan.update(status='diverged', notice='已记录的原生图片已变化，不能自动覆盖已有分支。')
                        break
                    if not descriptions:
                        continue
                    if previous:
                        # A later missing source must not erase a previously
                        # verified private copy. Only explicit missing→ready
                        # repairs and unavailable diagnostics can change.
                        descriptions = [dict(before) if before['material_state'] == 'ready' else dict(after)
                                        for before, after in zip(previous, descriptions)]
                    ids = _native_ids(descriptions)
                    if descriptions != previous or ids != old.get('attachment_ids', []):
                        update = {'message_id': old['id'], 'source_message_id': identity,
                                  'attachment_ids': ids, 'native_attachments': descriptions}
                        plan['material_updates'].append(update)
                elif descriptions and not (message.get('content') or '').strip():
                    if position > last and has_new_text_tail:
                        continue
                    next_old = next((old_by_id[new_ids[i]] for i in range(position + 1, len(native))
                                     if new_ids[i] in old_by_id), None)
                    anchor = next_old['id'] if next_old else ('after:' + imported[-1]['id'] if imported else None)
                    if anchor is None:
                        # An empty old branch has no original UUID to anchor;
                        # normal source import/append handles the entire source.
                        continue
                    addition = dict(message, source_anchor=anchor,
                                    attachment_ids=_native_ids(descriptions), native_attachments=descriptions)
                    plan['new_image_messages'].append(addition)
            if plan['status'] != 'diverged':
                count_images = sum(max(0, len(update['attachment_ids']) - len(old_by_id[update['source_message_id']].get('attachment_ids', [])))
                                   for update in plan['material_updates'])
                count_images += sum(len(m['attachment_ids']) for m in plan['new_image_messages'])
                plan['counts'] = {'backfilled_messages': len(plan['material_updates']),
                                  'backfilled_images': count_images,
                                  'inserted_image_messages': len(plan['new_image_messages'])}
                if plan['material_updates'] or plan['new_image_messages']:
                    plan.update(status='backfillable', notice='仅补入已核对前文的原生图片，不改原文字和分支。')
    if plan['status'] == 'diverged':
        plan['material_updates'], plan['new_image_messages'] = [], []
    plan['plan_version'] = _version({'source': source, 'native': source_projection(native),
        'old': [{'id': m.get('id'), 'record': _source_record(m, imported=True),
                 'attachment_ids': m.get('attachment_ids', []), 'native_attachments': m.get('native_attachments', []),
                 'source_anchor': m.get('source_anchor')} for m in imported],
        'material_updates': plan['material_updates'], 'new_image_messages': plan['new_image_messages']})
    return plan


class ChatStore:
    validate_attachment_ids = staticmethod(validate_attachment_ids)

    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        cloud = Path.home() / 'Library' / 'Mobile Documents'
        if cloud == self.path or cloud in self.path.parents:
            raise ValueError('聊天数据库需要保存在本机目录，不能放进项目的 iCloud 文件夹。')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self.lock = threading.RLock()
        with self._open() as db:
            db.executescript('''
                PRAGMA journal_mode=DELETE;
                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, source TEXT, creation_key TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS messages (
                    ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT UNIQUE NOT NULL, chat_id TEXT NOT NULL REFERENCES chats(id),
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL, created_at TEXT NOT NULL, backend TEXT,
                    status TEXT NOT NULL, provenance TEXT, phase TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS chat_messages ON messages(chat_id,ordinal);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id),
                    backend TEXT NOT NULL, client_id TEXT NOT NULL, status TEXT NOT NULL,
                    message_id TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, context_info TEXT, native_session_id TEXT,
                    UNIQUE(chat_id,client_id));
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(chat_id)
                    WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                INSERT OR IGNORE INTO store_meta VALUES('revision',0);
                CREATE TABLE IF NOT EXISTS chat_revisions (
                    chat_id TEXT PRIMARY KEY REFERENCES chats(id), revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS creation_aliases (
                    creation_key TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL REFERENCES chats(id));
                CREATE TABLE IF NOT EXISTS shared_drafts (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>0),
                    text TEXT NOT NULL, backend TEXT NOT NULL, target TEXT NOT NULL,
                    updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS shared_draft_receipts (
                    draft_id TEXT NOT NULL REFERENCES shared_drafts(id),
                    revision INTEGER NOT NULL CHECK(revision>0),
                    chat_id TEXT NOT NULL REFERENCES chats(id),
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    PRIMARY KEY(draft_id,revision));
                INSERT OR IGNORE INTO store_meta VALUES('draft_revision',0);
                CREATE TABLE IF NOT EXISTS context_selection (
                    chat_id TEXT PRIMARY KEY REFERENCES chats(id),
                    revision INTEGER NOT NULL CHECK(revision>=0),
                    pinned_ids TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chat_profile_selections (
                    chat_id TEXT PRIMARY KEY REFERENCES chats(id), ai_profile_id TEXT,
                    memory_profile_id TEXT NOT NULL DEFAULT 'none', revision INTEGER NOT NULL CHECK(revision>=0));
                CREATE TABLE IF NOT EXISTS source_update_receipts (
                    chat_id TEXT NOT NULL REFERENCES chats(id), source_version TEXT NOT NULL,
                    reviewed_revision INTEGER NOT NULL, resulting_revision INTEGER NOT NULL,
                    PRIMARY KEY(chat_id,source_version,reviewed_revision));
                CREATE TABLE IF NOT EXISTS source_image_backfill_receipts (
                    chat_id TEXT NOT NULL REFERENCES chats(id), plan_version TEXT NOT NULL,
                    reviewed_revision INTEGER NOT NULL, resulting_revision INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY(chat_id,plan_version,reviewed_revision));
            ''')
            db.execute('BEGIN IMMEDIATE')
            initialize_profile_schema(db)
            initialize_memory_profile_schema(db)
            initialize_memory_job_schema(db)
            self._migrate_memory_selection_in(db)
            # Existing chats survive upgrading the local prototype.
            message_columns = {row['name'] for row in db.execute('PRAGMA table_info(messages)')}
            for field in ('model', 'provider', 'model_source'):
                if field not in message_columns:
                    db.execute('ALTER TABLE messages ADD COLUMN ' + field + ' TEXT')
            if 'attachment_ids' not in message_columns:
                db.execute("ALTER TABLE messages ADD COLUMN attachment_ids TEXT NOT NULL DEFAULT '[]'")
            if 'native_attachments' not in message_columns:
                db.execute("ALTER TABLE messages ADD COLUMN native_attachments TEXT NOT NULL DEFAULT '[]'")
            if 'source_anchor' not in message_columns:
                db.execute('ALTER TABLE messages ADD COLUMN source_anchor TEXT')
            run_columns = {row['name'] for row in db.execute('PRAGMA table_info(runs)')}
            if 'context_pins' not in run_columns:
                db.execute("ALTER TABLE runs ADD COLUMN context_pins TEXT NOT NULL DEFAULT '[]'")
            if 'route' not in run_columns:
                db.execute('ALTER TABLE runs ADD COLUMN route TEXT')
            if 'input_images' not in run_columns:
                db.execute("ALTER TABLE runs ADD COLUMN input_images TEXT NOT NULL DEFAULT '[]'")
            if 'request_snapshot' not in run_columns:
                db.execute('ALTER TABLE runs ADD COLUMN request_snapshot TEXT')
            if 'model_source' not in run_columns:
                db.execute('ALTER TABLE runs ADD COLUMN model_source TEXT')
            for field in ('user_message_id', 'memory_inputs', 'memory_connection_fingerprint', 'memory_retain_error_code', 'task_intent', 'file_inputs'):
                if field not in run_columns:
                    db.execute('ALTER TABLE runs ADD COLUMN ' + field + ' TEXT')
            draft_columns = {row['name'] for row in db.execute('PRAGMA table_info(shared_drafts)')}
            if 'model_id' not in draft_columns:
                db.execute("ALTER TABLE shared_drafts ADD COLUMN model_id TEXT NOT NULL DEFAULT ''")
            if 'attachment_ids' not in draft_columns:
                db.execute("ALTER TABLE shared_drafts ADD COLUMN attachment_ids TEXT NOT NULL DEFAULT '[]'")
            if 'profile_snapshot' not in draft_columns:
                db.execute('ALTER TABLE shared_drafts ADD COLUMN profile_snapshot TEXT')
            if 'task_intent' not in draft_columns:
                db.execute('ALTER TABLE shared_drafts ADD COLUMN task_intent TEXT')
        os.chmod(self.path, 0o600)

    @contextmanager
    def _open(self, *, readonly=False):
        if readonly:
            db = sqlite3.connect('file:' + quote(str(self.path), safe='/') + '?mode=ro', uri=True, timeout=15)
        else:
            db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        if readonly:
            db.execute('PRAGMA query_only=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _migrate_memory_selection_in(db):
        """Remove only the former none-only check, preserving every old row."""
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_profile_selections'").fetchone()
        sql = row['sql'] if row is not None else ''
        checks = re.findall(r'CHECK\s*\([^)]*\bmemory_profile_id\b[^)]*\)', sql, flags=re.I)
        if not checks:
            return
        column = r'(?:memory_profile_id|"memory_profile_id"|\[memory_profile_id\]|`memory_profile_id`)'
        legacy = r"CHECK\s*\(\s*" + column + r"\s*=\s*'none'\s*\)"
        if any(not re.fullmatch(legacy, check, flags=re.I) for check in checks):
            raise ValueError('旧记忆选择表含未知约束，原数据保留，未自动重建。')
        columns = [item['name'] for item in db.execute('PRAGMA table_info(chat_profile_selections)')]
        if columns != ['chat_id', 'ai_profile_id', 'memory_profile_id', 'revision']:
            raise ValueError('旧记忆选择表含未知字段，原数据保留，未自动重建。')
        if db.execute("SELECT 1 FROM sqlite_master WHERE tbl_name='chat_profile_selections' AND "
                      "type IN ('index','trigger') AND sql IS NOT NULL").fetchone():
            raise ValueError('旧记忆选择表含独立索引或触发器，原数据保留，未自动重建。')
        temporary = '_memory_profile_selections_upgrade'
        if db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (temporary,)).fetchone():
            raise ValueError('记忆选择升级的临时表名已存在，原数据保留。')
        before = [tuple(item) for item in db.execute('SELECT * FROM chat_profile_selections ORDER BY chat_id')]
        db.execute('''CREATE TABLE _memory_profile_selections_upgrade (
            chat_id TEXT PRIMARY KEY REFERENCES chats(id), ai_profile_id TEXT,
            memory_profile_id TEXT NOT NULL DEFAULT 'none', revision INTEGER NOT NULL CHECK(revision>=0))''')
        db.execute('''INSERT INTO _memory_profile_selections_upgrade(chat_id,ai_profile_id,memory_profile_id,revision)
            SELECT chat_id,ai_profile_id,memory_profile_id,revision FROM chat_profile_selections''')
        after = [tuple(item) for item in db.execute('SELECT * FROM _memory_profile_selections_upgrade ORDER BY chat_id')]
        if before != after:
            raise ValueError('记忆选择升级核对未通过，原数据保留。')
        db.execute('DROP TABLE chat_profile_selections')
        db.execute('ALTER TABLE _memory_profile_selections_upgrade RENAME TO chat_profile_selections')

    def create(self, title='新对话', source=None, imported=(), creation_key=None, reuse_source=False):
        with self.lock, self._open() as db:
            # Protect lookup-and-create across independently opened stores too,
            # not only threads sharing this object's RLock.
            db.execute('BEGIN IMMEDIATE')
            chat_id = self._create_in(db, title, source, imported, creation_key, reuse_source)
        return self.get(chat_id)['chat']

    def _create_in(self, db, title, source=None, imported=(), creation_key=None, reuse_source=False):
        """Use the caller's transaction so a draft can create and submit atomically."""
        if creation_key:
            old = self._creation(db, creation_key)
            if old:
                return old['id']
        if reuse_source:
            old = self._source(db, source)
            if old:
                if creation_key:
                    db.execute('INSERT INTO creation_aliases(creation_key,chat_id) VALUES(?,?)',
                               (creation_key, old['id']))
                return old['id']
        chat_id, stamp = str(uuid.uuid4()), now()
        db.execute('INSERT INTO chats(id,title,created_at,updated_at,source,creation_key) VALUES(?,?,?,?,?,?)',
                   (chat_id, title.strip()[:120] or '新对话', stamp, stamp,
                    json.dumps(source, ensure_ascii=False) if source else None, creation_key))
        for message in imported:
            record = imported_message(message, str(uuid.uuid4()), stamp)
            if record is None:
                continue
            db.execute('''INSERT INTO messages(id,chat_id,role,content,created_at,backend,status,provenance,phase,attachment_ids,native_attachments)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                       (record['id'], chat_id, record['role'], record['content'],
                        record['created_at'], record['backend'], record['status'],
                        json.dumps(record['provenance'], ensure_ascii=False), record['phase'], json.dumps(record['attachment_ids']),
                        json.dumps(record['native_attachments'], ensure_ascii=False)))
        self._bump(db, chat_id)
        return chat_id

    @staticmethod
    def _bump(db, chat_id):
        # The revision changes in the same transaction as the actual records.
        # Reads, duplicate submissions, and late terminal callbacks do not bump.
        db.execute("UPDATE store_meta SET value=value+1 WHERE key='revision'")
        revision = db.execute("SELECT value FROM store_meta WHERE key='revision'").fetchone()[0]
        db.execute('''INSERT INTO chat_revisions VALUES(?,?)
            ON CONFLICT(chat_id) DO UPDATE SET revision=excluded.revision''', (chat_id, revision))

    def changes(self, chat_id=None):
        with self.lock, self._open() as db:
            revision = db.execute("SELECT value FROM store_meta WHERE key='revision'").fetchone()[0]
            selected = None
            if chat_id:
                row = db.execute('''SELECT c.id,COALESCE(cr.revision,0) AS revision,
                    EXISTS(SELECT 1 FROM runs r WHERE r.chat_id=c.id AND r.status IN ('queued','running')) AS running
                    FROM chats c LEFT JOIN chat_revisions cr ON cr.chat_id=c.id WHERE c.id=?''', (chat_id,)).fetchone()
                if row:
                    selected = {'id': row['id'], 'revision': str(row['revision']), 'running': bool(row['running'])}
            return {'revision': str(revision), 'selected': selected}

    def list(self):
        with self.lock, self._open() as db:
            rows = db.execute('''SELECT c.*, COALESCE(cr.revision,0) AS revision,
                              (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id) AS message_count,
                              EXISTS(SELECT 1 FROM runs r WHERE r.chat_id=c.id AND r.status IN ('queued','running')) AS running
                              FROM chats c LEFT JOIN chat_revisions cr ON cr.chat_id=c.id ORDER BY c.updated_at DESC''')
            return {'chats': [self._chat(row) for row in rows]}

    @staticmethod
    def _chat(row):
        value = dict(row)
        value.pop('creation_key', None)
        value['revision'] = str(value['revision'])
        value['source'] = json.loads(value['source']) if value.get('source') else None
        if 'running' in value:
            value['running'] = bool(value['running'])
        return value

    @staticmethod
    def _run(row, *, db=None):
        if row is None:
            return None
        value = dict(row)
        value['context_info'] = json.loads(value['context_info']) if value.get('context_info') else None
        value['context_pins'] = json.loads(value.get('context_pins') or '[]')
        value['route'] = json.loads(value['route']) if value.get('route') else None
        value['input_images'] = json.loads(value.get('input_images') or '[]')
        value['task_intent'] = validate_intent(json.loads(value['task_intent']) if value.get('task_intent') else None)
        value['file_inputs'] = validate_inputs(json.loads(value.get('file_inputs') or '[]'), value['task_intent'])
        value['request_snapshot'] = validate_request_snapshot(json.loads(value['request_snapshot'])) if value.get('request_snapshot') else None
        raw_memory = value.pop('memory_inputs', None)
        value.pop('memory_connection_fingerprint', None)
        retain_error = value.pop('memory_retain_error_code', None)
        snapshot = value['request_snapshot']
        if snapshot is not None and snapshot['schema_version'] == 2:
            package = json.loads(raw_memory) if raw_memory is not None else None
            recall = public_recall_info(package, snapshot)
            memory = snapshot['memory_profile']
            retain = {'state': 'disabled' if memory['id'] == 'none' or memory['mode'] == 'read_only'
                      else 'not_applicable', 'operation_id': None, 'error_code': None}
            if db is not None:
                job = db.execute('SELECT state,operation_id,error_code FROM memory_write_jobs WHERE run_id=?',
                                 (value['id'],)).fetchone()
                if job is not None:
                    retain = public_memory_job_state(dict(job))
                elif retain_error is not None:
                    retain = {'state': 'failed', 'operation_id': None, 'error_code': 'memory_write_failed'}
            elif retain_error is not None:
                retain = {'state': 'failed', 'operation_id': None, 'error_code': 'memory_write_failed'}
            value['memory_info'] = {'recall': recall, 'retain': retain}
        return value

    @staticmethod
    def _context_selection(db, chat_id):
        row = db.execute('SELECT revision,pinned_ids FROM context_selection WHERE chat_id=?', (chat_id,)).fetchone()
        return {'revision': row['revision'], 'pinned_ids': json.loads(row['pinned_ids'])} if row else {'revision': 0, 'pinned_ids': []}

    @staticmethod
    def _profile_selection(db, chat_id):
        row = db.execute('SELECT revision,ai_profile_id,memory_profile_id FROM chat_profile_selections WHERE chat_id=?',
                         (chat_id,)).fetchone()
        return dict(row) if row else {'revision': 0, 'ai_profile_id': None, 'memory_profile_id': 'none'}

    def get_profile_selection(self, chat_id):
        with self.lock, self._open() as db:
            db.execute('BEGIN')
            if not db.execute('SELECT 1 FROM chats WHERE id=?', (chat_id,)).fetchone():
                raise KeyError('找不到这段对话。')
            return self._profile_selection(db, chat_id)

    @staticmethod
    def _validate_selection_revision(revision):
        if type(revision) is not int or not 0 <= revision <= MAX_REVISION:
            raise ValueError('聊天配置选择版本需要有效的整数。')

    @staticmethod
    def _write_profile_selection(db, chat_id, old, ai_profile_id, memory_profile_id):
        if old['ai_profile_id'] == ai_profile_id and old['memory_profile_id'] == memory_profile_id:
            return old
        if old['revision'] == MAX_REVISION:
            raise ValueError('这段聊天的配置选择已达到版本上限。')
        selected = {'revision': old['revision'] + 1, 'ai_profile_id': ai_profile_id, 'memory_profile_id': memory_profile_id}
        db.execute('''INSERT INTO chat_profile_selections(chat_id,ai_profile_id,memory_profile_id,revision) VALUES(?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET ai_profile_id=excluded.ai_profile_id,
                memory_profile_id=excluded.memory_profile_id,revision=excluded.revision''',
            (chat_id, ai_profile_id, memory_profile_id, selected['revision']))
        return selected

    def set_profile_selection(self, chat_id, ai_profile_id=UNSET, *, memory_profile_id=UNSET, revision):
        self._validate_selection_revision(revision)
        if ai_profile_id is not UNSET and ai_profile_id is not None:
            validate_profile_id(ai_profile_id)
        if memory_profile_id is not UNSET:
            validate_memory_profile_id(memory_profile_id, allow_none=True)
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM chats WHERE id=?', (chat_id,)).fetchone():
                raise KeyError('找不到这段对话。')
            old = self._profile_selection(db, chat_id)
            if old['revision'] != revision:
                raise ProfileSelectionConflict(old)
            if db.execute("SELECT 1 FROM runs WHERE chat_id=? AND status IN ('queued','running','cancelling')", (chat_id,)).fetchone():
                raise BusyError('这一段对话正在回答或停止，配置选择暂不能修改。')
            chosen_ai = old['ai_profile_id'] if ai_profile_id is UNSET else ai_profile_id
            chosen_memory = old['memory_profile_id'] if memory_profile_id is UNSET else memory_profile_id
            if chosen_ai is not None:
                AIProfiles.get_in(db, chosen_ai)
            if chosen_memory != 'none':
                MemoryProfiles.get_in(db, chosen_memory)
            selected = self._write_profile_selection(db, chat_id, old, chosen_ai, chosen_memory)
            if selected != old:
                self._bump(db, chat_id)
            return selected

    def profile_message_snapshots(self, chat_id):
        """Historical intent only; never look up a profile's current version."""
        with self.lock, self._open() as db:
            db.execute('BEGIN')
            if not db.execute('SELECT 1 FROM chats WHERE id=?', (chat_id,)).fetchone():
                raise KeyError('找不到这段对话。')
            rows = db.execute('SELECT message_id,request_snapshot FROM runs WHERE chat_id=? AND request_snapshot IS NOT NULL',
                              (chat_id,))
            result = {}
            for row in rows:
                snapshot = validate_request_snapshot(json.loads(row['request_snapshot']))
                if snapshot is not None:
                    result[row['message_id']] = snapshot
            return result

    def message_request_configurations(self, chat_id):
        """Safe frozen request parameters, not provider execution telemetry.

        Historical replies never resolve today's profile or connector defaults.
        Missing, corrupt or ambiguous evidence leaves this optional item absent.
        """
        with self.lock, self._open(readonly=True) as db:
            db.execute('BEGIN')
            if not db.execute('SELECT 1 FROM chats WHERE id=?', (chat_id,)).fetchone():
                raise KeyError('找不到这段对话。')
            rows = db.execute('''SELECT r.message_id,r.route FROM runs r
                JOIN messages m ON m.id=r.message_id AND m.chat_id=r.chat_id
                WHERE r.chat_id=? AND m.role='assistant' ''', (chat_id,))
            result, seen = {}, set()
            for row in rows:
                message_id = row['message_id']
                if message_id in seen:
                    result.pop(message_id, None)
                    continue
                seen.add(message_id)
                raw = row['route']
                if not isinstance(raw, str) or len(raw) > 64 * 1024:
                    continue
                try:
                    route = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(route, dict) or not valid_model(route.get('model')):
                    continue
                effort = route.get('reasoning_effort')
                result[message_id] = {
                    'model': route['model'],
                    'reasoning_effort': effort if isinstance(effort, str) and effort in REASONING_EFFORTS else None,
                    'source': 'requested_route',
                }
            return result

    def memory_message_info(self, chat_id):
        """Public evidence for every v2 reply, without current-profile lookup."""
        with self.lock, self._open(readonly=True) as db:
            db.execute('BEGIN')
            if not db.execute('SELECT 1 FROM chats WHERE id=?', (chat_id,)).fetchone():
                raise KeyError('找不到这段对话。')
            rows = list(db.execute('SELECT * FROM runs WHERE chat_id=? AND request_snapshot IS NOT NULL', (chat_id,)))
            result = {}
            for row in rows:
                run = self._run(row, db=db)
                if 'memory_info' in run:
                    result[run['message_id']] = run['memory_info']
            return result

    @staticmethod
    def _validate_profile_snapshot_projection(snapshot, *, backend, model_id=UNSET, route=UNSET):
        snapshot = validate_request_snapshot(snapshot)
        if snapshot is None:
            return None
        profile = snapshot['ai_profile']
        if profile['agent_instance_id'] != 'local/' + backend:
            raise ValueError('AI 配置与所选 Agent 连接不一致。')
        if model_id is not UNSET and model_id != profile['model_id']:
            raise ValueError('共享草稿模型与 AI 配置不一致。')
        if route is not UNSET:
            if (not isinstance(route, dict) or route.get('backend') != backend
                    or 'requested_model_id' not in route or route['requested_model_id'] != profile['model_id']):
                raise ValueError('本轮实际连接与 AI 配置的模型选择不一致。')
            validate_model_id(route.get('model_id'), backend)
            if not route.get('model_id'):
                raise ValueError('本轮连接没有明确的实际模型候选。')
            if profile['model_id'] and route['model_id'] != profile['model_id']:
                raise ValueError('本轮实际模型候选与显式配置不一致。')
            if profile['reasoning_effort'] is not None and route.get('reasoning_effort') != profile['reasoning_effort']:
                raise ValueError('本轮实际连接没有应用已保存的思考强度。')
        return snapshot

    @staticmethod
    def _validate_profile_snapshot_in(db, snapshot, *, backend, model_id=UNSET, route=UNSET):
        snapshot = ChatStore._validate_profile_snapshot_projection(snapshot, backend=backend,
                                                                   model_id=model_id, route=route)
        if snapshot is not None:
            current = AIProfiles.get_in(db, snapshot['ai_profile']['id'])
            if make_profile_snapshot(current)['ai_profile'] != snapshot['ai_profile']:
                raise ProfileConflict(current)
            memory = snapshot['memory_profile']
            if memory['id'] != 'none':
                current_memory = MemoryProfiles.get_in(db, memory['id'])
                if make_memory_snapshot(current_memory) != memory:
                    raise MemoryProfileConflict(current_memory)
        return snapshot

    def get_context_selection(self, chat_id):
        with self.lock, self._open() as db:
            db.execute('BEGIN')
            if not db.execute('SELECT 1 FROM chats WHERE id=?', (chat_id,)).fetchone():
                raise KeyError('找不到这段对话。')
            return self._context_selection(db, chat_id)

    def set_context_pin(self, chat_id, message_id, pinned, revision):
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 256 or '\x00' in message_id:
            raise ValueError('必带前文的消息标识无效。')
        if type(pinned) is not bool:
            raise ValueError('请选择固定或取消固定。')
        if type(revision) is not int or not 0 <= revision <= 2**53 - 1:
            raise ValueError('必带前文版本无效。')
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM chats WHERE id=?', (chat_id,)).fetchone():
                raise KeyError('找不到这段对话。')
            selection = self._context_selection(db, chat_id)
            if revision != selection['revision']:
                raise ContextSelectionConflict(selection)
            row = db.execute('SELECT role,status,content,attachment_ids,native_attachments FROM messages WHERE chat_id=? AND id=?', (chat_id, message_id)).fetchone()
            if (row is None or row['role'] not in ('user', 'assistant') or row['status'] not in ('completed', 'imported')
                    or (not row['content'].strip() and not json.loads(row['attachment_ids']) and not json.loads(row['native_attachments']))):
                raise ValueError('只能固定这段共同聊天中已完成且非空的用户消息或回答。')
            pins = selection['pinned_ids']
            if (message_id in pins) == pinned:
                return selection
            if revision == 2**53 - 1:
                raise ValueError('这段聊天的必带前文已达到版本上限。')
            if pinned:
                if len(pins) >= MAX_CONTEXT_PINS:
                    raise ValueError('最多固定 20 条必带前文，请先取消一条。')
                pins = pins + [message_id]
            else:
                pins = [value for value in pins if value != message_id]
            next_selection = {'revision': revision + 1, 'pinned_ids': pins}
            db.execute('''INSERT INTO context_selection VALUES(?,?,?,?)
                ON CONFLICT(chat_id) DO UPDATE SET revision=excluded.revision,pinned_ids=excluded.pinned_ids,updated_at=excluded.updated_at''',
                (chat_id, revision + 1, json.dumps(pins), now()))
            self._bump(db, chat_id)
            return next_selection

    @staticmethod
    def messages_in(db, chat_id, *, schema='main', include_file_task_inputs=False):
        """Project complete messages in the caller's current SQLite snapshot.

        The connection must use sqlite3.Row and complete ChatStore messages/runs
        schemas in main or the attached common database. The caller supplies the
        chat ID and owns the transaction. This method opens no connection and
        never commits or rolls back; logical ordering and filtering stay intact.
        """
        if schema not in ('main', 'common'):
            raise ValueError('共同聊天库名称无效。')
        table = 'main.messages' if schema == 'main' else 'common.messages'
        messages = [dict(m) for m in db.execute('SELECT * FROM ' + table + ' WHERE chat_id=? ORDER BY ordinal', (chat_id,))]
        for message in messages:
            message['provenance'] = json.loads(message['provenance']) if message['provenance'] else None
            message['attachment_ids'] = json.loads(message.get('attachment_ids') or '[]')
            message['native_attachments'] = json.loads(message.get('native_attachments') or '[]')
        by_id = {m['id']: m for m in messages}
        # Read-only text materials belong to the original user turn. Their
        # immutable identities live in existing run receipts, never in the
        # user's sentence or an extra material/body column. The same-transaction
        # projection is also used before accepting a continuation.
        for run in db.execute('SELECT id,message_id,status,user_message_id,task_intent,file_inputs,context_info FROM ' + schema +
                              '.runs WHERE chat_id=? AND task_intent IS NOT NULL', (chat_id,)):
            intent = validate_intent(json.loads(run['task_intent']))
            if include_file_task_inputs and intent['mode'] == 'files' and run['status'] == 'completed':
                reply = by_id.get(run['message_id'])
                if reply is not None and reply['role'] == 'assistant' and reply['status'] == 'completed':
                    reply['file_task_run_id'] = run['id']
            # File-tool inputs are not automatically replayed as ordinary
            # chat context. Explicit handoff may select their frozen versions.
            if (intent['mode'] != 'chat' and not include_file_task_inputs
                    or not intent['artifact_ids']):
                continue
            owner = by_id.get(run['user_message_id'])
            if owner is None or owner['role'] != 'user':
                continue
            owner['text_file_inputs'] = validate_inputs(json.loads(run['file_inputs'] or '[]'), intent)
            info = json.loads(run['context_info']) if run['context_info'] else {}
            if isinstance(info, dict) and isinstance(info.get('file_input_refs'), list):
                owner['text_file_input_refs'] = info['file_input_refs']
        before, after = {}, {}
        for message in messages:
            anchor = message.get('source_anchor')
            if not isinstance(anchor, str):
                continue
            target, buckets = (anchor[6:], after) if anchor.startswith('after:') else (anchor, before)
            if target in by_id and target != message['id']:
                buckets.setdefault(target, []).append(message)
        ordered, seen = [], set()
        anchored = {m['id'] for bucket in (before, after) for values in bucket.values() for m in values}
        def emit(message):
            pending = [(message, False)]
            while pending:
                current, ready = pending.pop()
                if ready:
                    ordered.append(current)
                    continue
                if current['id'] in seen:
                    continue
                seen.add(current['id'])
                pending.extend((child, False) for child in reversed(after.get(current['id'], [])))
                pending.append((current, True))
                pending.extend((child, False) for child in reversed(before.get(current['id'], [])))
        for message in messages:
            if message['id'] not in anchored:
                emit(message)
        # Never lose a preserved record if an old/corrupt anchor became orphaned.
        for message in messages:
            emit(message)
        return ordered

    def get(self, chat_id):
        with self.lock, self._open() as db:
            db.execute('BEGIN')
            return self._get_in(db, chat_id)

    def handoff_snapshot(self, chat_id):
        """Include frozen material and memory evidence for explicit selection.

        One read-only transaction keeps messages, versions and source identities
        consistent; no file bodies are read and ordinary context is unchanged.
        The latest run is used only for its activity flag: invalid optional
        memory evidence must not prevent handing off preserved message text.
        """
        with self.lock, self._open() as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            result = self._get_in(db, chat_id, include_file_task_inputs=True, include_run=False)
            self._handoff_memory_in(db, chat_id, result['messages'])
            return result

    @staticmethod
    def _handoff_memory_in(db, chat_id, messages):
        """Attach only one completed reply's immutable, safe recall evidence.

        Invalid legacy evidence cannot erase the reply's ordinary text. This
        projection neither resolves today's profile nor consults a memory bank.
        """
        replies = {message['id']: message for message in messages
                   if message['role'] == 'assistant' and message['status'] == 'completed'}
        by_reply = {}
        for row in db.execute('''SELECT message_id,status,request_snapshot,memory_inputs
                FROM runs WHERE chat_id=?''', (chat_id,)):
            if row['message_id'] in replies:
                by_reply.setdefault(row['message_id'], []).append(row)
        for identity, rows in by_reply.items():
            message = replies[identity]
            if len(rows) != 1:
                message['handoff_memory_unavailable'] = True
                continue
            row = rows[0]
            if row['status'] != 'completed':
                continue
            try:
                snapshot = validate_request_snapshot(json.loads(row['request_snapshot'])
                    if row['request_snapshot'] is not None else None)
                package = json.loads(row['memory_inputs']) if row['memory_inputs'] is not None else None
                if snapshot is None or snapshot['schema_version'] != 2:
                    if package is not None:
                        raise ValueError('旧请求不能附带长期记忆检索包。')
                    continue
                memory = snapshot['memory_profile']
                recall = public_recall_info(package, snapshot)
                if memory['id'] == 'none':
                    continue
                message['handoff_memory'] = {
                    'profile_id': memory['id'], 'profile_revision': memory['revision'],
                    'profile_name': memory['name'], 'retrieved_at': recall['retrieved_at'],
                    'references': recall['used_references'], 'habits': memory['habits']}
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                message['handoff_memory_unavailable'] = True

    def _get_in(self, db, chat_id, *, include_file_task_inputs=False, include_run=True):
        row = db.execute('''SELECT c.*,COALESCE(cr.revision,0) AS revision
            FROM chats c LEFT JOIN chat_revisions cr ON cr.chat_id=c.id WHERE c.id=?''', (chat_id,)).fetchone()
        if row is None:
            raise KeyError('找不到这段对话。')
        messages = self.messages_in(db, chat_id,
                                     include_file_task_inputs=include_file_task_inputs)
        for message in messages:
            message.pop('ordinal')
        chat = self._chat(row)
        chat['message_count'] = len(messages)
        latest = db.execute('SELECT * FROM runs WHERE chat_id=? ORDER BY created_at DESC LIMIT 1', (chat_id,)).fetchone()
        run = self._run(latest, db=db) if include_run else None
        chat['running'] = bool(latest and latest['status'] in ACTIVE)
        return {'chat': chat, 'messages': messages, 'run': run, 'revision': chat['revision'],
                'context_selection': self._context_selection(db, chat_id),
                'profile_selection': self._profile_selection(db, chat_id)}

    def markdown_snapshot(self, chat_id):
        """Read all frozen file-input identities with the same chat snapshot.

        This projection never reads input files or reconstructs missing owner
        links. Ordinary get() and its per-turn context semantics stay unchanged.
        """
        with self.lock, self._open() as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            result = self._get_in(db, chat_id)
            owners = {message['id']: message for message in result['messages'] if message['role'] == 'user'}
            seen = {}
            for row in db.execute('SELECT user_message_id,task_intent,file_inputs FROM runs WHERE chat_id=? ORDER BY created_at,id',
                                  (chat_id,)):
                raw = json.loads(row['file_inputs']) if row['file_inputs'] is not None else None
                intent = validate_intent(json.loads(row['task_intent']) if row['task_intent'] is not None else None)
                refs = validate_inputs(raw, intent)
                owner = row['user_message_id']
                if owner not in owners:
                    if refs:
                        raise ValueError('文件输入缺少原用户消息关联，不能生成完整整理快照。')
                    continue
                if owner in seen and seen[owner] != refs:
                    raise ValueError('同一用户消息有不同文件输入记录，不能生成完整整理快照。')
                seen[owner] = refs
            for identity, message in owners.items():
                message['markdown_file_inputs'] = seen.get(identity, [])
            result['markdown_projection'] = {'schema_version': 1, 'frozen_file_inputs_complete': True}
            return result

    def find_submission(self, chat_id, client_id):
        with self.lock, self._open() as db:
            return self._run(db.execute('SELECT * FROM runs WHERE chat_id=? AND client_id=?', (chat_id, client_id)).fetchone(), db=db)

    def find_creation(self, creation_key):
        with self.lock, self._open() as db:
            row = self._creation(db, creation_key)
            return self._chat(row) if row else None

    @staticmethod
    def _creation(db, creation_key):
        return db.execute('''SELECT c.*,COALESCE(cr.revision,0) AS revision
            FROM chats c LEFT JOIN chat_revisions cr ON cr.chat_id=c.id
            WHERE c.creation_key=? OR c.id=(SELECT chat_id FROM creation_aliases WHERE creation_key=?)
            ORDER BY CASE WHEN c.creation_key=? THEN 0 ELSE 1 END LIMIT 1''',
            (creation_key, creation_key, creation_key)).fetchone()

    @staticmethod
    def _source(db, source):
        if not isinstance(source, dict) or not source.get('agent') or not source.get('session_id'):
            return None
        rows = db.execute('''SELECT c.*,COALESCE(cr.revision,0) AS revision
            FROM chats c LEFT JOIN chat_revisions cr ON cr.chat_id=c.id WHERE c.source IS NOT NULL
            ORDER BY c.updated_at DESC,c.created_at DESC,c.id''')
        for row in rows:
            try:
                saved = json.loads(row['source'])
            except (ValueError, TypeError):
                continue
            if (isinstance(saved, dict) and saved.get('agent') == source['agent']
                    and saved.get('session_id') == source['session_id']):
                return row
        return None

    def find_source(self, source):
        with self.lock, self._open() as db:
            row = self._source(db, source)
            return self._chat(row) if row else None

    @staticmethod
    def validate_source_update(source_version, chat_revision):
        if (not isinstance(source_version, str) or len(source_version) != 64
                or any(char not in '0123456789abcdef' for char in source_version)):
            raise ValueError('来源版本无效，请重新查看来源更新。')
        if type(chat_revision) is not int or not 0 <= chat_revision <= 2**53 - 1:
            raise ValueError('共同聊天版本无效，请重新查看来源更新。')

    def append_source_update(self, chat_id, source, indexed, source_version, chat_revision, source_guard=None):
        """Append a fully reviewed native tail without touching prior branches.

        The write lock and receipt protect independent ChatStore instances too.
        A retry succeeds with zero additions only while the post-write chat and
        the complete native source still match that successful request.
        """
        self.validate_source_update(source_version, chat_revision)
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            chat = db.execute('''SELECT c.*,COALESCE(cr.revision,0) AS revision
                FROM chats c LEFT JOIN chat_revisions cr ON cr.chat_id=c.id WHERE c.id=?''', (chat_id,)).fetchone()
            if chat is None:
                raise KeyError('找不到这段对话。')
            saved_source = json.loads(chat['source']) if chat['source'] else None
            if (not isinstance(saved_source, dict) or not isinstance(source, dict)
                    or {key: saved_source.get(key) for key in ('agent', 'session_id')} != source):
                raise SourceUpdateConflict('这段聊天的来源已变化，请重新查看来源更新。')
            if db.execute("SELECT 1 FROM runs WHERE chat_id=? AND status IN ('queued','running','cancelling')", (chat_id,)).fetchone():
                raise BusyError('这一段对话正在回答或停止，请等它结束后重新查看来源更新。')
            if source_guard is not None:
                source_guard()
            messages = self.messages_in(db, chat_id)
            comparison = source_update_comparison(source, messages, indexed)
            if source_version != comparison['source_version']:
                raise SourceUpdateConflict('原生来源已变化，请重新查看来源更新再追加。')
            receipt = db.execute('''SELECT resulting_revision FROM source_update_receipts
                WHERE chat_id=? AND source_version=? AND reviewed_revision=?''',
                (chat_id, source_version, chat_revision)).fetchone()
            if receipt and chat['revision'] == receipt['resulting_revision']:
                return 0
            if chat_revision != chat['revision']:
                raise SourceUpdateConflict('共同聊天已变化，请重新查看来源更新再追加。')
            if comparison['status'] == 'diverged':
                raise SourceUpdateConflict('原生来源已有删除、改动或顺序变化，不能自动追加，请重新查看来源原文。')
            if comparison['status'] == 'unchanged':
                return 0
            stamp = now()
            native_messages = [message for message in indexed if message.get('role') in ('user', 'assistant')]
            for message in native_messages[comparison['imported_messages']:]:
                record = imported_message(message, str(uuid.uuid4()), stamp)
                # This is the date added to this shared branch. The original
                # timestamp remains unchanged in provenance.
                db.execute('''INSERT INTO messages(id,chat_id,role,content,created_at,backend,status,provenance,phase,attachment_ids,native_attachments)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (record['id'], chat_id, record['role'], record['content'], stamp, record['backend'],
                     record['status'], json.dumps(record['provenance'], ensure_ascii=False), record['phase'], json.dumps(record['attachment_ids']),
                     json.dumps(record['native_attachments'], ensure_ascii=False)))
            db.execute('UPDATE chats SET updated_at=? WHERE id=?', (stamp, chat_id))
            self._bump(db, chat_id)
            resulting_revision = db.execute('SELECT revision FROM chat_revisions WHERE chat_id=?', (chat_id,)).fetchone()[0]
            db.execute('INSERT INTO source_update_receipts VALUES(?,?,?,?)',
                       (chat_id, source_version, chat_revision, resulting_revision))
            if source_guard is not None:
                # A changed external index rolls this entire write back,
                # including the revision and receipt, before it is committed.
                source_guard()
            return comparison['new_messages']

    def find_source_image_backfill_receipt(self, chat_id, plan_version, chat_revision):
        """Read an original image-repair success without inspecting its source."""
        self.validate_source_update(plan_version, chat_revision)
        with self.lock, self._open() as db:
            db.execute('BEGIN')
            if not db.execute('SELECT 1 FROM chats WHERE id=?', (chat_id,)).fetchone():
                raise KeyError('找不到这段对话。')
            receipt = db.execute('''SELECT result_json FROM source_image_backfill_receipts
                WHERE chat_id=? AND plan_version=? AND reviewed_revision=?''',
                (chat_id, plan_version, chat_revision)).fetchone()
            return dict(json.loads(receipt['result_json']), recovered=True) if receipt is not None else None

    def backfill_source_images(self, chat_id, source, indexed, plan_version, chat_revision, source_guard=None):
        """Apply exactly one reviewed image repair without changing old columns."""
        self.validate_source_update(plan_version, chat_revision)
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            chat = db.execute('''SELECT c.*,COALESCE(cr.revision,0) AS revision
                FROM chats c LEFT JOIN chat_revisions cr ON cr.chat_id=c.id WHERE c.id=?''', (chat_id,)).fetchone()
            if chat is None:
                raise KeyError('找不到这段对话。')
            saved_source = json.loads(chat['source']) if chat['source'] else None
            if (not isinstance(saved_source, dict) or not isinstance(source, dict)
                    or {key: saved_source.get(key) for key in ('agent', 'session_id')} != source):
                raise SourceUpdateConflict('这段聊天的来源已变化，请重新查看图片补入。')
            receipt = db.execute('''SELECT result_json FROM source_image_backfill_receipts
                WHERE chat_id=? AND plan_version=? AND reviewed_revision=?''',
                (chat_id, plan_version, chat_revision)).fetchone()
            if receipt is not None:
                # Original success remains recoverable after later chat changes;
                # this only acknowledges it and performs no current mutation.
                return dict(json.loads(receipt['result_json']), recovered=True)
            if db.execute("SELECT 1 FROM runs WHERE chat_id=? AND status IN ('queued','running','cancelling')", (chat_id,)).fetchone():
                raise BusyError('这一段对话正在回答或停止，请等它结束后再补入图片。')
            if chat_revision != chat['revision']:
                raise SourceUpdateConflict('共同聊天已变化，请重新查看图片补入。')
            if source_guard is not None:
                source_guard()
            messages = self.messages_in(db, chat_id)
            plan = source_image_backfill_plan(source, messages, indexed, chat_revision=chat_revision)
            if plan_version != plan['plan_version']:
                raise SourceUpdateConflict('原生图片或补入计划已变化，请重新查看。')
            if plan['status'] == 'diverged':
                raise SourceUpdateConflict(plan['notice'])
            result = dict(plan['counts'], recovered=False)
            if plan['status'] == 'unchanged':
                return result
            for update in plan['material_updates']:
                db.execute('UPDATE messages SET attachment_ids=?,native_attachments=? WHERE chat_id=? AND id=?',
                    (json.dumps(update['attachment_ids']), json.dumps(update['native_attachments'], ensure_ascii=False),
                     chat_id, update['message_id']))
            stamp = now()
            for message in plan['new_image_messages']:
                record = imported_message(message, str(uuid.uuid4()), stamp)
                db.execute('''INSERT INTO messages(id,chat_id,role,content,created_at,backend,status,provenance,phase,
                    attachment_ids,native_attachments,source_anchor) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (record['id'], chat_id, record['role'], record['content'], record['created_at'], record['backend'],
                     record['status'], json.dumps(record['provenance'], ensure_ascii=False), record['phase'],
                     json.dumps(record['attachment_ids']), json.dumps(record['native_attachments'], ensure_ascii=False),
                     record['source_anchor']))
            db.execute('UPDATE chats SET updated_at=? WHERE id=?', (stamp, chat_id))
            self._bump(db, chat_id)
            resulting_revision = db.execute('SELECT revision FROM chat_revisions WHERE chat_id=?', (chat_id,)).fetchone()[0]
            db.execute('INSERT INTO source_image_backfill_receipts VALUES(?,?,?,?,?)',
                (chat_id, plan_version, chat_revision, resulting_revision, json.dumps(result)))
            if source_guard is not None:
                source_guard()
            return result

    def submit(self, chat_id, text, backend, client_id, model=None, provider=None, context_validator=None, route=None, attachment_ids=None,
               *, profile_snapshot=None, profile_selection_revision=None, memory_inputs=None, memory_connection_fingerprint=None, task_intent=None, file_inputs=None):
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            run, created = self._submit_in(db, chat_id, text, backend, client_id, model, provider,
                                           context_validator=context_validator, route=route, attachment_ids=attachment_ids,
                                           profile_snapshot=profile_snapshot, profile_selection_revision=profile_selection_revision,
                                           memory_inputs=memory_inputs, memory_connection_fingerprint=memory_connection_fingerprint,
                                           task_intent=task_intent, file_inputs=file_inputs)
        return run, created

    def _submit_in(self, db, chat_id, text, backend, client_id, model=None, provider=None, context_validator=None, route=None, attachment_ids=None,
                   *, profile_snapshot=None, profile_selection_revision=None,
                   memory_inputs=None, memory_connection_fingerprint=None, task_intent=None, file_inputs=None):
        previous = db.execute('SELECT * FROM runs WHERE chat_id=? AND client_id=?', (chat_id, client_id)).fetchone()
        if previous:
            return self._run(previous, db=db), False
        intent = validate_intent(task_intent)
        frozen_files = validate_inputs(file_inputs, intent)
        attachments = validate_attachment_ids(attachment_ids)
        if not isinstance(text, str) or (not text.strip() and not attachments):
            raise ValueError('先写消息或添加图片，再发送。')
        row = db.execute('SELECT title FROM chats WHERE id=?', (chat_id,)).fetchone()
        if row is None:
            raise KeyError('找不到这段对话。')
        if db.execute("SELECT 1 FROM runs WHERE chat_id=? AND status IN ('queued','running','cancelling')", (chat_id,)).fetchone():
            raise BusyError('这一段对话正在回答，请等它完成或先停止。')
        snapshot = self._validate_profile_snapshot_in(db, profile_snapshot, backend=backend, route=route)
        package = validate_memory_inputs(memory_inputs, snapshot, query=memory_query(text))
        chosen_memory = snapshot['memory_profile']['id'] if snapshot is not None else 'none'
        if chosen_memory == 'none':
            if memory_connection_fingerprint is not None:
                raise ValueError('不使用长期记忆的请求不能附带记忆连接指纹。')
        elif (not isinstance(memory_connection_fingerprint, str)
                or not re.fullmatch(r'[0-9a-f]{64}', memory_connection_fingerprint)):
            raise ValueError('本轮长期记忆连接指纹无效。')
        selection = self._profile_selection(db, chat_id)
        if profile_selection_revision is not None:
            self._validate_selection_revision(profile_selection_revision)
            if profile_selection_revision != selection['revision']:
                raise ProfileSelectionConflict(selection)
        if snapshot is None:
            if selection['ai_profile_id'] is not None or selection['memory_profile_id'] != 'none':
                raise ProfileSelectionConflict(selection)
        else:
            chosen_id = snapshot['ai_profile']['id']
            initial = selection == {'revision': 0, 'ai_profile_id': None, 'memory_profile_id': 'none'}
            if not initial and selection['memory_profile_id'] != chosen_memory:
                raise ProfileSelectionConflict(selection)
            if selection['ai_profile_id'] is not None:
                if selection['ai_profile_id'] != chosen_id or profile_selection_revision is None:
                    raise ProfileSelectionConflict(selection)
            elif selection['revision'] and profile_selection_revision is None:
                raise ProfileSelectionConflict(selection)
        pins = self._context_selection(db, chat_id)['pinned_ids']
        validation = None
        if context_validator is not None:
            messages = self.messages_in(db, chat_id)
            for message in messages:
                message.pop('ordinal')
            messages.append({'id': 'draft', 'role': 'user', 'status': 'completed', 'content': text,
                             'attachment_ids': attachments, 'is_draft': True})
            validation = context_validator(messages, pins)
        context_info, input_images = None, []
        if validation is not None:
            if not isinstance(validation, dict) or not isinstance(validation.get('context_info'), dict):
                raise ValueError('本轮上下文核验结果无效。')
            context_info = validation['context_info']
            input_images = validation.get('input_images', [])
            if (not isinstance(input_images, list) or len(input_images) > 8
                    or any(not isinstance(item, dict) or set(item) != {'id', 'sha256'}
                           or not isinstance(item['id'], str) or not re.fullmatch(r'img_[0-9a-f]{32}', item['id'])
                           or not isinstance(item['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])
                           for item in input_images)
                    or len({item['id'] for item in input_images}) != len(input_images)):
                raise ValueError('本轮图片核验结果无效。')
        stamp, run_id, reply_id, user_id = now(), str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        if snapshot is not None:
            self._write_profile_selection(db, chat_id, selection, snapshot['ai_profile']['id'], chosen_memory)
        db.execute('''INSERT INTO messages(id,chat_id,role,content,created_at,backend,status,attachment_ids)
                      VALUES(?,?,?,?,?,?,?,?)''', (user_id, chat_id, 'user', text, stamp, None, 'completed', json.dumps(attachments)))
        db.execute('''INSERT INTO messages(id,chat_id,role,content,created_at,backend,status,model,provider)
                      VALUES(?,?,?,?,?,?,?,?,?)''', (reply_id, chat_id, 'assistant', '', stamp, backend, 'queued', model, provider))
        db.execute('''INSERT INTO runs(id,chat_id,backend,client_id,status,message_id,created_at,updated_at,context_pins,route,context_info,input_images,request_snapshot,
                      user_message_id,memory_inputs,memory_connection_fingerprint,task_intent,file_inputs)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (run_id, chat_id, backend, client_id, 'queued', reply_id, stamp, stamp,
                      json.dumps(pins), json.dumps(route, ensure_ascii=False) if route is not None else None,
                      json.dumps(context_info, ensure_ascii=False) if context_info is not None else None, json.dumps(input_images),
                      json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None, user_id,
                      json.dumps(package, ensure_ascii=False) if package is not None else None, memory_connection_fingerprint,
                      json.dumps(intent) if intent['mode'] == 'files' or intent['artifact_ids'] else None,
                      json.dumps(frozen_files) if frozen_files else None))
        title = (text.strip().splitlines()[0][:40] if text.strip() else '图片对话') if row['title'] == '新对话' else row['title']
        db.execute('UPDATE chats SET updated_at=?,title=? WHERE id=?', (stamp, title, chat_id))
        self._bump(db, chat_id)
        return self._run(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone(), db=db), True

    def run(self, run_id):
        with self.lock, self._open() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise KeyError('找不到这次回答。')
            return self._run(row, db=db)

    def file_task_runs(self, chat_id):
        with self.lock, self._open(readonly=True) as db:
            return [self._run(row, db=db) for row in db.execute(
                'SELECT * FROM runs WHERE chat_id=? AND task_intent IS NOT NULL ORDER BY created_at,id', (chat_id,))]

    def get_memory_inputs(self, run_id):
        """Worker-only private frozen evidence; never current-profile lookup."""
        with self.lock, self._open(readonly=True) as db:
            row = db.execute('SELECT request_snapshot,memory_inputs FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise KeyError('找不到这次回答。')
            snapshot = validate_request_snapshot(json.loads(row['request_snapshot'])) if row['request_snapshot'] else None
            package = json.loads(row['memory_inputs']) if row['memory_inputs'] is not None else None
            return validate_memory_inputs(package, snapshot)

    def get_memory_connection_fingerprint(self, run_id):
        """Worker-only connection identity; omit this from public run rows."""
        with self.lock, self._open(readonly=True) as db:
            row = db.execute('SELECT memory_connection_fingerprint FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise KeyError('找不到这次回答。')
            value = row['memory_connection_fingerprint']
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)):
                raise ValueError('本轮冻结的记忆连接指纹无法核验。')
            return value

    def update_run(self, run_id, status=None, text=None, error=None, context_info=None, native_session_id=None,
                   model=None, provider=None, model_source=None):
        with self.lock, self._open() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise KeyError('找不到这次回答。')
            # A canceled or completed run cannot be revived by late callbacks.
            if row['status'] not in ACTIVE:
                return
            if model_source is not None and model_source not in ('requested_route', 'adapter_reported', 'service_reported'):
                raise ValueError('模型来源说明无效。')
            stamp = now()
            changes, values = ['updated_at=?'], [stamp]
            for key, value in (('status', status), ('error', error), ('context_info', json.dumps(context_info, ensure_ascii=False) if context_info is not None else None),
                               ('native_session_id', native_session_id), ('model_source', model_source)):
                if value is not None:
                    changes.append(key + '=?')
                    values.append(value)
            db.execute('UPDATE runs SET ' + ','.join(changes) + ' WHERE id=?', values + [run_id])
            if text is not None:
                db.execute('UPDATE messages SET content=? WHERE id=?', (text, row['message_id']))
            if status is not None:
                db.execute('UPDATE messages SET status=? WHERE id=?', (status, row['message_id']))
            for field, value in (('model', model), ('provider', provider), ('model_source', model_source)):
                if value is not None:
                    db.execute('UPDATE messages SET ' + field + '=? WHERE id=?', (value, row['message_id']))
            if status == 'completed':
                db.execute('SAVEPOINT memory_write_enqueue')
                try:
                    enqueue_completed_in(db, run_id, connection_fingerprint=row['memory_connection_fingerprint'],
                                         request_snapshot_validator=validate_request_snapshot)
                except Exception:
                    # Preserve the complete answer, but make the independent
                    # write failure durable and visible instead of pretending
                    # it was remembered or failing a valid model response.
                    db.execute('ROLLBACK TO memory_write_enqueue')
                    db.execute('RELEASE memory_write_enqueue')
                    db.execute("UPDATE runs SET memory_retain_error_code='memory_write_failed' WHERE id=?", (run_id,))
                else:
                    db.execute('RELEASE memory_write_enqueue')
            db.execute('UPDATE chats SET updated_at=? WHERE id=?', (stamp, row['chat_id']))
            self._bump(db, row['chat_id'])

    def recover_interrupted(self):
        with self.lock, self._open() as db:
            pending = list(db.execute("SELECT * FROM runs WHERE status IN ('queued','running')"))
            for row in pending:
                stamp = now()
                db.execute("UPDATE runs SET status='interrupted',error=?,updated_at=? WHERE id=?",
                           ('服务曾中断。这次回答的结果未确认，未自动重新发送。', stamp, row['id']))
                db.execute("UPDATE messages SET status='interrupted' WHERE id=?", (row['message_id'],))
                db.execute('UPDATE chats SET updated_at=? WHERE id=?', (stamp, row['chat_id']))
                self._bump(db, row['chat_id'])
            return len(pending)

    def active_count(self):
        with self.lock, self._open() as db:
            return db.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued','running')").fetchone()[0]
