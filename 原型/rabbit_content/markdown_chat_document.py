"""Deterministic Markdown projections of common chats and native text indexes.

SQLite remains authoritative. Parsing reads document metadata and exact body
bytes only; Markdown headings and message labels are never a role/edit protocol.
Image/text-file references are identities, not an attachment backup.
"""
import hashlib
import json
import re
import uuid

from .history_records import source_phase
from .history_markdown_reference import reference_identity, reference_source


MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_HEADER_BYTES = 4096
MAX_NATIVE_MESSAGES = 20000
_FIELDS = {'schema_version', 'workspace_id', 'rabbit_id', 'kind', 'source',
           'revision', 'body_sha256'}
_AGENTS = {'codex', 'pi', 'hermes', 'claude', 'dsh'}
_ACTIVE = {'queued', 'running', 'cancelling', 'pending'}
_NATIVE_FIELDS = {'agent', 'chat', 'session_ids', 'sources'}
_NATIVE_MESSAGE = {'message_id', 'role', 'phase', 'timestamp', 'ordinal',
                   'content', 'native_image_only'}


class MarkdownChatDocumentError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code='invalid_document'):
    raise MarkdownChatDocumentError(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _text(value, maximum, *, empty=False):
    if (type(value) is not str or not (0 if empty else 1) <= len(value) <= maximum
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        _fail()
    try:
        value.encode('utf-8')
    except UnicodeError:
        _fail()
    return value


def _workspace(value):
    value = _text(value, 128)
    if any(c.isspace() for c in value):
        _fail()
    return value


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            _fail()
    except (ValueError, AttributeError):
        _fail()
    return value


def _source(value):
    if value is None:
        return None
    if (type(value) is not dict or type(value.get('agent')) is not str
            or value['agent'] not in _AGENTS):
        _fail()
    return {'agent': value['agent'], 'session_id': _text(value.get('session_id'), 256)}


def _metadata(value):
    if (type(value) is not dict or set(value) != _FIELDS
            or type(value['schema_version']) is not int):
        _fail()
    _workspace(value['workspace_id']); _uuid(value['rabbit_id'])
    revision = value['revision']
    if value['schema_version'] == 1 and value['kind'] == 'work':
        if (type(revision) is not str or not re.fullmatch(r'0|[1-9][0-9]{0,18}', revision)
                or int(revision) > 2**63-1):
            _fail()
        projected = _source(value['source'])
        if projected != value['source']:
            _fail()
    elif value['schema_version'] == 2 and value['kind'] == 'native_index':
        source = value['source']
        if (type(revision) is not str or not re.fullmatch(r'[0-9a-f]{64}', revision)
                or type(source) is not dict or set(source) != {'agent', 'snapshot_sha256'}
                or type(source['agent']) is not str or source['agent'] not in _AGENTS
                or source['snapshot_sha256'] != revision):
            _fail()
        projected = dict(source)
    elif value['schema_version'] == 3 and value['kind'] == 'history_reference':
        source = value['source']
        try:
            if type(source) is not dict or set(source) != {'agent', 'session_id'}:
                _fail()
            projected = reference_source(source['agent'], source['session_id'])
            identity, expected_revision = reference_identity(value['workspace_id'], **projected)
        except (ValueError, TypeError):
            _fail()
        if value['rabbit_id'] != identity or revision != expected_revision:
            _fail()
    else:
        _fail()
    if (type(value['body_sha256']) is not str
            or not re.fullmatch(r'[0-9a-f]{64}', value['body_sha256'])):
        _fail()
    return dict(value, source=projected)


def document_identity(metadata):
    """Return a typed navigation identity after validating the fixed header."""
    metadata = _metadata(metadata)
    if metadata['kind'] in ('native_index', 'history_reference'):
        return (metadata['kind'], metadata['source']['agent'], metadata['rabbit_id'])
    return metadata['rabbit_id']


def render_history_reference(snapshot, workspace_id):
    """A small organization reference, never a copy of indexed messages."""
    if (type(snapshot) is not dict or set(snapshot) != {'agent', 'session_id', 'title'}
            or type(snapshot['title']) is not str or len(snapshot['title']) > 4096):
        _fail('invalid_snapshot')
    try:
        source = reference_source(snapshot['agent'], snapshot['session_id'])
        identity, revision = reference_identity(workspace_id, **source)
        title = re.sub(r'[\r\n]+', ' ', snapshot['title'] or '未命名对话')
        title = re.sub(r'([\\`*_[\]<>])', r'\\\1', title)
        body = ('\n# ' + title + '\n\n> 对话索引引用，不含聊天正文。\n\n'
                '- Agent：' + source['agent'] + '\n- 原会话：'
                + _json(source['session_id']) + '\n').encode('utf-8')
    except (ValueError, TypeError, UnicodeError):
        _fail('invalid_snapshot')
    metadata = _metadata(dict(schema_version=3, workspace_id=workspace_id, rabbit_id=identity,
        kind='history_reference', source=source, revision=revision,
        body_sha256=hashlib.sha256(body).hexdigest()))
    data = ('---\n' + _json(metadata) + '\n---\n').encode('utf-8') + body
    return {'data': data, 'sha256': hashlib.sha256(data).hexdigest(),
            'size_bytes': len(data), 'metadata': metadata}


def _inline(value):
    # Only small metadata is quoted; message content stays ordinary Markdown.
    text = _json(value)
    fence = '`' * max(1, 1 + max((len(run) for run in re.findall(r'`+', text)), default=0))
    return fence + ' ' + text + ' ' + fence


def _message_source(message):
    value = message.get('provenance')
    if value is None:
        return None
    if type(value) is not dict:
        _fail('invalid_snapshot')
    result = {}
    for key in ('agent', 'session_id', 'message_id', 'timestamp'):
        if value.get(key) is not None:
            result[key] = _text(value[key], 4096)
    return result or None


def _references(message):
    result = {}
    images = message.get('attachment_ids') or []
    if type(images) is not list or any(type(item) is not str or not re.fullmatch(r'img_[0-9a-f]{32}', item) for item in images):
        _fail('invalid_snapshot')
    if images:
        result['image_ids'] = list(images)
    native = message.get('native_attachments') or []
    if type(native) is not list:
        _fail('invalid_snapshot')
    if native:
        selected = []
        for item in native:
            if type(item) is not dict:
                _fail('invalid_snapshot')
            if (type(item.get('token')) is not str or not re.fullmatch(r'natimg_[0-9a-f]{64}', item['token'])
                    or type(item.get('fingerprint')) is not str or not re.fullmatch(r'[0-9a-f]{64}', item['fingerprint'])
                    or type(item.get('attachment_ordinal')) is not int
                    or not 0 <= item['attachment_ordinal'] < 514):
                _fail('invalid_snapshot')
            selected.append({key: item.get(key) for key in ('token', 'fingerprint',
                'attachment_ordinal', 'attachment_id', 'state', 'reason',
                'material_state', 'material_reason', 'omitted_count')})
        result['native_images'] = selected
    files = message.get('markdown_file_inputs')
    if message.get('role') != 'user' and files is None:
        files = []
    if type(files) is not list:
        _fail('invalid_snapshot')
    if files:
        selected = []
        for item in files:
            if (type(item) is not dict or type(item.get('sha256')) is not str
                    or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])):
                _fail('invalid_snapshot')
            selected.append({'id': _uuid(item.get('id')), 'sha256': item['sha256']})
        result['text_file_inputs'] = selected
    return result


def render(snapshot, workspace_id):
    """Return bytes, complete-file SHA/size and the seven metadata fields."""
    if type(snapshot) is not dict or type(snapshot.get('chat')) is not dict:
        _fail('invalid_snapshot')
    projection = snapshot.get('markdown_projection')
    if (type(projection) is not dict or set(projection) != {'schema_version', 'frozen_file_inputs_complete'}
            or type(projection['schema_version']) is not int or projection['schema_version'] != 1
            or projection['frozen_file_inputs_complete'] is not True):
        _fail('incomplete_file_input_projection')
    chat, messages, run = snapshot['chat'], snapshot.get('messages'), snapshot.get('run')
    if type(messages) is not list or run is not None and type(run) is not dict:
        _fail('invalid_snapshot')
    if run is not None and type(run.get('status')) is not str:
        _fail('invalid_snapshot')
    if chat.get('running') is True or run is not None and run['status'] in _ACTIVE:
        _fail('active_run')
    metadata = _metadata({'schema_version': 1, 'workspace_id': _workspace(workspace_id),
        'rabbit_id': _uuid(chat.get('id')), 'kind': 'work', 'source': _source(chat.get('source')),
        'revision': chat.get('revision'), 'body_sha256': '0'*64})
    if snapshot.get('revision') != metadata['revision']:
        _fail('invalid_snapshot')
    title = chat.get('title')
    if type(title) is not str:
        _fail('invalid_snapshot')
    header_size = len(('---\n' + _json(metadata) + '\n---\n').encode('utf-8'))
    parts, size = [], header_size
    def append(text):
        nonlocal size
        if type(text) is not str or len(text) > MAX_DOCUMENT_BYTES:
            _fail('capacity' if type(text) is str else 'invalid_snapshot')
        try:
            raw = text.encode('utf-8')
        except UnicodeError:
            _fail('invalid_snapshot')
        if size + len(raw) > MAX_DOCUMENT_BYTES:
            _fail('capacity')
        parts.append(raw); size += len(raw)
    label = re.sub(r'[\r\n]+', ' ', title)
    label = re.sub(r'([\\`*_[\]<>])', r'\\\1', label)
    append('\n# ' + label + '\n\n> SQLite 对话的整理快照；正文当前不支持回读或角色编辑。'
           '附件仅保留身份与状态，未复制附件文件，不是完整资料备份。\n\n')
    seen = set()
    for index, message in enumerate(messages, 1):
        if type(message) is not dict or message.get('role') not in ('user', 'assistant'):
            _fail('invalid_snapshot')
        identity = _uuid(message.get('id'))
        if identity in seen:
            _fail('invalid_snapshot')
        seen.add(identity)
        role = message['role']
        status = _text(message.get('status'), 64)
        if status in _ACTIVE:
            _fail('active_run')
        phase = _text(message.get('phase', ''), 128, empty=True)
        agent = message.get('backend')
        if agent is not None and (type(agent) is not str or agent not in _AGENTS):
            _fail('invalid_snapshot')
        info = {'message_id': identity, 'role': role, 'status': status, 'phase': phase,
                'agent': agent, 'source': _message_source(message)}
        refs = _references(message)
        label = source_phase(phase).get('source_kind') or ('未完成的助手回复，未带入接续'
            if phase == 'incomplete_reply' else '用户记录' if role == 'user' else '助手记录')
        append('---\n\n## ' + str(index) + '. ' + label
               + '\n\n- 身份与来源：' + _inline(info) + '\n')
        if refs:
            append('- 附件引用（未打包）：' + _inline(refs) + '\n')
        append('\n')
        append(message.get('content'))
        append('\n\n')
    body = b''.join(parts)
    metadata['body_sha256'] = hashlib.sha256(body).hexdigest()
    data = ('---\n' + _json(metadata) + '\n---\n').encode('utf-8') + body
    return {'data': data, 'sha256': hashlib.sha256(data).hexdigest(),
            'size_bytes': len(data), 'metadata': metadata}


def _native_json(value, depth=0):
    if depth > 32:
        _fail('invalid_snapshot')
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                _fail('invalid_snapshot')
            _native_json(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _native_json(item, depth + 1)
    elif value is not None and type(value) not in (str, int, float, bool):
        _fail('invalid_snapshot')


def _native_core(snapshot):
    if (type(snapshot) is not dict or not _NATIVE_FIELDS <= set(snapshot)
            or set(snapshot) - _NATIVE_FIELDS - {'indexed_at'}):
        _fail('invalid_snapshot')
    if 'indexed_at' in snapshot:
        _text(snapshot['indexed_at'], 128, empty=True)
    agent, chat = snapshot['agent'], snapshot['chat']
    if (type(agent) is not str or agent not in _AGENTS
            or type(chat) is not dict or set(chat) != {'id', 'title'}
            or type(chat['title']) is not str):
        _fail('invalid_snapshot')
    _uuid(chat['id'])
    sessions, sources = snapshot['session_ids'], snapshot['sources']
    if (type(sessions) is not list or not 1 <= len(sessions) <= 128
            or any(type(item) is not str for item in sessions)
            or sessions != sorted(set(sessions))
            or agent != 'hermes' and len(sessions) != 1
            or type(sources) is not list or len(sources) != len(sessions)):
        _fail('invalid_snapshot')
    for identity in sessions:
        _text(identity, 256)
        if any(c.isspace() for c in identity):
            _fail('invalid_snapshot')
    seen_sessions = set()
    count = 0
    for source in sources:
        if (type(source) is not dict or set(source) != {'session_id', 'coverage', 'messages'}
                or type(source['session_id']) is not str or source['session_id'] not in sessions
                or source['session_id'] in seen_sessions or type(source['coverage']) is not dict
                or type(source['messages']) is not list):
            _fail('invalid_snapshot')
        seen_sessions.add(source['session_id'])
        _native_json(source['coverage'])
        seen_messages = set()
        for message in source['messages']:
            count += 1
            if count > MAX_NATIVE_MESSAGES:
                _fail('capacity')
            if (type(message) is not dict or set(message) != _NATIVE_MESSAGE
                    or message['role'] not in ('user', 'assistant')
                    or type(message['ordinal']) is not int
                    or not 0 <= message['ordinal'] <= 2**63 - 1
                    or type(message['content']) is not str
                    or type(message['native_image_only']) is not bool):
                _fail('invalid_snapshot')
            identity = _text(message['message_id'], 4096)
            if identity in seen_messages:
                _fail('invalid_snapshot')
            seen_messages.add(identity)
            _text(message['phase'], 128, empty=True)
            _text(message['timestamp'], 128, empty=True)
    if [source['session_id'] for source in sources] != sessions:
        _fail('invalid_snapshot')
    core = {key: snapshot[key] for key in _NATIVE_FIELDS}
    try:
        raw = _json(core).encode('utf-8')
        detached = json.loads(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail('invalid_snapshot')
    if len(raw) > MAX_DOCUMENT_BYTES:
        _fail('capacity')
    return detached, raw


def native_revision(snapshot):
    """Hash the exact validated index core, excluding observation time."""
    _core, raw = _native_core(snapshot)
    return hashlib.sha256(raw).hexdigest()


def render_native(snapshot, workspace_id):
    """Render indexed text without inventing native message UUIDs or status."""
    core, raw = _native_core(snapshot)
    revision = hashlib.sha256(raw).hexdigest()
    metadata = _metadata({
        'schema_version': 2, 'workspace_id': _workspace(workspace_id),
        'rabbit_id': core['chat']['id'], 'kind': 'native_index',
        'source': {'agent': core['agent'], 'snapshot_sha256': revision},
        'revision': revision, 'body_sha256': '0' * 64,
    })
    parts = []
    size = len(('---\n' + _json(metadata) + '\n---\n').encode('utf-8'))

    def append(text):
        nonlocal size
        try:
            data = text.encode('utf-8')
        except UnicodeError:
            _fail('invalid_snapshot')
        size += len(data)
        if size > MAX_DOCUMENT_BYTES:
            _fail('capacity')
        parts.append(data)

    title = re.sub(r'[\r\n]+', ' ', core['chat']['title'])
    title = re.sub(r'([\\`*_[\]<>])', r'\\\1', title)
    append('\n# ' + title + '\n\n> 原生对话的完整已索引文字快照，内容已脱敏；'
           '不代表原生完整状态或最新现场。附件、工具、思考及未收入索引的内容未打包，'
           '正文不回读为消息。\n\n')
    append('- 原生目录：' + _inline({'agent': core['agent'], 'chat_id': core['chat']['id']}) + '\n')
    append('- 索引会话：' + _inline(core['session_ids']) + '\n\n')
    for number, source in enumerate(core['sources'], 1):
        append('---\n\n## 索引来源 ' + str(number) + '\n\n- 原会话ID：'
               + _inline(source['session_id']) + '\n- 索引覆盖：' + _inline(source['coverage']) + '\n\n')
        for index, message in enumerate(source['messages'], 1):
            label = source_phase(message['phase']).get('source_kind') or (
                '索引用户文字' if message['role'] == 'user' else '索引助手文字')
            info = {key: message[key] for key in ('message_id', 'phase', 'timestamp', 'ordinal', 'native_image_only')}
            info['indexed_role'] = message['role']
            append('### ' + str(index) + '. ' + label + '\n\n- 索引身份：' + _inline(info) + '\n\n')
            if message['native_image_only']:
                append('> 此索引记录含图片位置，原图未打包。\n\n')
            append(message['content'])
            append('\n\n')
    body = b''.join(parts)
    metadata['body_sha256'] = hashlib.sha256(body).hexdigest()
    data = ('---\n' + _json(metadata) + '\n---\n').encode('utf-8') + body
    return {'data': data, 'sha256': hashlib.sha256(data).hexdigest(),
            'size_bytes': len(data), 'metadata': metadata}


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _fail()
        result[key] = value
    return result


def parse(data, workspace_id=None):
    """Read only identity metadata and exact body bytes; never parse messages.

    A mismatched body hash is returned explicitly, so a scanner can retain the
    document identity while reporting an external edit. A file's own declared
    hash is never a substitute for the mapping's trusted published-file SHA.
    """
    if type(data) is not bytes:
        _fail()
    if len(data) > MAX_DOCUMENT_BYTES:
        _fail('capacity')
    if not data.startswith(b'---\n'):
        _fail()
    end = data.find(b'\n', 4, MAX_HEADER_BYTES + 5)
    if end < 0 or data[end:end+5] != b'\n---\n':
        _fail()
    try:
        line = data[4:end].decode('utf-8')
        body = data[end+5:]
        body.decode('utf-8')
        value = json.loads(line, object_pairs_hook=_pairs,
            parse_constant=lambda _value: _fail())
        metadata = _metadata(value)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        _fail()
    if workspace_id is not None and metadata['workspace_id'] != _workspace(workspace_id):
        _fail('workspace_changed')
    body_sha256 = hashlib.sha256(body).hexdigest()
    return {'metadata': metadata, 'body': body, 'sha256': hashlib.sha256(data).hexdigest(),
            'size_bytes': len(data), 'body_sha256': body_sha256,
            'body_matches_header': body_sha256 == metadata['body_sha256']}
