"""Small client for a single local Rabbit workspace; never opens its databases.

The running application's existing validation and revisions own every write.
No redirect, proxy, arbitrary host/path or mutation retry is supported.
"""
import http.client
import hashlib
import json
import posixpath
import re
import unicodedata
import uuid
from urllib.parse import urlencode

MAX_RESPONSE_BYTES = 512 * 1024
MAX_REVISION = 2 ** 53 - 1
PROFILE_FIELDS = ('name', 'agent_instance_id', 'device_id', 'model_id', 'reasoning_effort')
PROFILE_COLUMNS = ('id', 'revision') + PROFILE_FIELDS + ('created_at', 'updated_at')
EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
OPERATIONS = '/api/management/ai-profile-operations'
SETTINGS = '/api/management/settings'
SETTINGS_OPTIONS = {'theme': ('dark', 'light', 'system'), 'density': ('comfortable', 'compact'),
                    'send_shortcut': ('ctrl-enter', 'enter'), 'markdown': (False, True),
                    'show_internal': (False, True)}
HERMES_EFFORT_SOURCE = 'Hermes 原生 parse_reasoning_effort；原生请求强度，提供商可能映射。'
NATIVE_CONNECTIONS = '/api/management/native-model-connections'
NATIVE_CATALOG = '/api/management/native-model-catalog'
SPECIALIST_MODEL_CATALOG = '/api/management/specialist-model-catalog'
NATIVE_MODEL_PREPARATION = '/api/management/native-model-preparation'
_NATIVE_LIMITS = {'codex': 2, 'pi': 1, 'hermes': 1}
NATIVE_CHAT_NAMES = '/api/management/native-chat-names'
NATIVE_CHAT_AGENTS = ('codex', 'pi', 'hermes', 'claude', 'dsh')
NATIVE_CHAT_CONFIGURATION = '/api/management/native-chat-configuration'
NATIVE_CHAT_MEMORY_CONFIGURATION = '/api/management/native-chat-memory-configuration'
NATIVE_CHAT_SELECTION_AGENTS = ('codex', 'pi', 'hermes')
_NATIVE_CHAT_SELECTION_ERRORS = frozenset('native_chat_selection_' + code for code in (
    'conflict', 'invalid', 'unavailable', 'not_found', 'corrupt'))
CODEX_HISTORY_PREVIEW = '/api/native-history/codex/preview'
CODEX_HISTORY_ADOPT = '/api/native-history/codex/adopt'
NATIVE_HISTORY_PATHS = {
    ('codex', 'preview'): CODEX_HISTORY_PREVIEW, ('codex', 'adopt'): CODEX_HISTORY_ADOPT,
    ('hermes', 'preview'): '/api/native-history/hermes/preview',
    ('hermes', 'adopt'): '/api/native-history/hermes/adopt',
    ('pi', 'preview'): '/api/native-history/pi/preview',
    ('pi', 'adopt'): '/api/native-history/pi/adopt',
    ('claude', 'preview'): '/api/native-history/claude/preview',
    ('claude', 'adopt'): '/api/native-history/claude/adopt',
    ('dsh', 'preview'): '/api/native-history/dsh/preview',
    ('dsh', 'adopt'): '/api/native-history/dsh/adopt',
}
NATIVE_HISTORY_MODES = {'codex': 'paginated', 'hermes': 'stored_session',
                        'pi': 'active_branch', 'claude': 'sdk_history', 'dsh': 'web_profile'}
MEMORY_PROFILES = '/api/management/memory-profiles'
MEMORY_OPERATIONS = '/api/management/memory-profile-operations'
MEMORY_CREATIONS = '/api/management/memory-profile-creations/'
MEMORY_CONNECTIONS = '/api/management/memory-connections/'
MEMORY_FIELDS = ('name', 'description', 'habits', 'program', 'connection_id', 'mode')
MEMORY_MUTABLE = ('name', 'description', 'habits', 'mode')
MEMORY_COLUMNS = ('id', 'revision') + MEMORY_FIELDS + ('bank_ref', 'locked_mode', 'source_kind')
MEMORY_RESPONSE_BYTES = 3 * 1024 * 1024  # 128 profiles, each up to 4000 Unicode code points.
MARKDOWN_CHATS = '/api/markdown-chats'
MARKDOWN_RESPONSE_BYTES = 3 * 1024 * 1024
_MARKDOWN_STATES = ('prepared', 'publishing', 'verified', 'conflict', 'blocked')
_MARKDOWN_UPDATE_ERRORS = frozenset((
    'invalid_arguments', 'invalid_document', 'document_changed',
    'directory_unavailable', 'head_changed', 'source_changed', 'file_changed',
    'stage_missing', 'stage_unapplied', 'observation_unknown', 'current_changed',
    'preview_expired', 'operation_conflict', 'capacity', 'closed', 'io_failed',
    'schema_changed', 'unsupported', 'not_found', 'invalid_path', 'invalid_text',
    'scope_unavailable', 'scope_changed', 'unsafe_file', 'stage_exists',
    'file_exists', 'unsupported_platform', 'swap_unsupported', 'readonly_file',
))
_MARKDOWN_ERRORS = frozenset(
    (
        'invalid_arguments',
        'operation_conflict',
        'binding_exists',
        'document_changed',
        'workspace_changed',
        'not_found',
        'artifact_changed',
        'source_not_generated',
        'preview_expired',
        'target_exists',
        'scope_changed',
        'scope_unavailable',
        'capacity',
        'io_failed',
        'closed',
        'invalid_path',
        'invalid_text',
        'unsafe_file',
        'file_exists',
        'file_changed',
        'stage_exists',
        'stage_missing',
        'current_changed',
        'source_changed',
        'markdown_chat_unavailable',
        'markdown_chat_disabled',
        'invalid_folder',
        'invalid_folder_parent',
        'folder_parent_missing',
        'folder_conflict',
        'directory_changed',
        'entry_capacity',
        'depth_capacity',
    )
) | _MARKDOWN_UPDATE_ERRORS


def _markdown_revision(value):
    if (
        type(value) is not str
        or re.fullmatch(r'0|[1-9][0-9]{0,18}', value) is None
        or int(value) > 2**63 - 1
    ):
        raise ValueError('Invalid chat revision.')
    return value


def _markdown_path(value, *, target=False, folder=False):
    if type(value) is not str or len(value) > 8192 or not value and not folder:
        raise ValueError('Invalid Markdown relative path.')
    value.encode('utf-8')
    if (
        value.startswith('/')
        or (target and ('\\' in value or ':' in value))
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or (value and any(part in ('', '.', '..') for part in value.split('/')))
    ):
        raise ValueError('Invalid Markdown relative path.')
    if not folder and not value.casefold().endswith('.md'):
        raise ValueError('A Markdown file is required.')
    if target and (
        len(value) > 240
        or len(value.split('/')) > 8
        or any(
            part.startswith('.') or len(part.encode('utf-8')) > 255
            for part in value.split('/')
        )
    ):
        raise ValueError('Invalid publication destination.')
    return value


def _markdown_folder_path(value, *, create=False):
    _markdown_path(value, target=create, folder=True)
    parts = value.split('/')
    if (not value or len(parts) > (7 if create else 8)
            or any(part.startswith('.') or len(part.encode('utf-8')) > 255
                   or create and not part.strip() for part in parts)):
        raise ValueError('Invalid visible Markdown folder.')
    return value


def validate_markdown_operation(value):
    if type(value) is not dict or type(value.get('action')) is not str:
        raise ValueError('Invalid Markdown operation.')
    required = {
        'preview': ('action', 'chat_id', 'source_revision', 'target_path'),
        'preview_native': ('action', 'agent', 'chat_id', 'target_path'),
        'preview_reference': ('action', 'agent', 'session_id', 'target_path'),
        'read': ('action', 'publication_id'),
        'confirm': ('action', 'publication_id', 'client_id'),
        'receipt': ('action', 'client_id'),
        'sync_preview': ('action', 'binding_id', 'expected_head_id'),
        'sync_read': ('action', 'update_id'),
        'sync_confirm': ('action', 'update_id', 'client_id'),
        'sync_receipt': ('action', 'client_id'),
        'create_folder': ('action', 'path'),
    }.get(value['action'])
    if required is None:
        raise ValueError('Invalid Markdown action.')
    _fields(value, required)
    for key in ('chat_id', 'publication_id', 'client_id', 'binding_id', 'expected_head_id', 'update_id'):
        if key in value:
            _uuid(value[key])
    if value['action'] in ('preview', 'preview_native', 'preview_reference'):
        if value['action'] == 'preview':
            _markdown_revision(value['source_revision'])
        elif type(value['agent']) is not str or value['agent'] not in NATIVE_CHAT_AGENTS:
            raise ValueError('Invalid native Markdown agent.')
        if value['action'] == 'preview_reference':
            _markdown_reference_session_id(value['session_id'])
        _markdown_path(value['target_path'], target=True)
    elif value['action'] == 'create_folder':
        _markdown_folder_path(value['path'], create=True)
    return dict(value)


def _markdown_reference_session_id(value):
    """An exact indexed source ID; unlike a native application chat, not a UUID."""
    if (type(value) is not str or not 1 <= len(value) <= 256
            or any(c.isspace() or unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in value)):
        raise ValueError('Invalid indexed history reference source.')
    return value


def _markdown_source(value, workspace):
    kind = value.get('kind') if type(value) is dict else None
    indexed = kind in ('native_index', 'history_reference')
    _fields(
        value, ('kind', 'workspace_id', 'chat_id', 'revision', 'sha256', 'size_bytes')
        + (('agent',) if indexed else ())
        + (('session_id',) if kind == 'history_reference' else ())
    )
    if kind not in ('markdown_chat', 'native_index', 'history_reference') or value['workspace_id'] != workspace:
        raise ValueError('Invalid Markdown source scope.')
    _uuid(value['chat_id'])
    if indexed:
        _markdown_index_identity(value)
        _native_history_revision(value['revision'])
        if kind == 'history_reference':
            _markdown_reference_session_id(value['session_id'])
    else:
        _markdown_revision(value['revision'])
    _native_history_revision(value['sha256'])
    if (
        type(value['size_bytes']) is not int
        or not 0 < value['size_bytes'] <= 16 * 1024 * 1024
    ):
        raise ValueError('Invalid Markdown source size.')
    return value


def _markdown_publication(value, workspace):
    _fields(
        value,
        (
            'id',
            'project_name',
            'target_path',
            'source',
            'state',
            'created_at',
            'updated_at',
            'current_verified',
            'error_code',
            'can_confirm',
        ),
    )
    _uuid(value['id'])
    _text(value['project_name'], 255)
    _markdown_path(value['target_path'], target=True)
    _markdown_source(value['source'], workspace)
    for key in ('created_at', 'updated_at'):
        _text(value[key], 80)
    if (
        type(value['state']) is not str
        or value['state'] not in _MARKDOWN_STATES
        or type(value['current_verified']) is not bool
        or type(value['can_confirm']) is not bool
        or (value['current_verified'] and value['state'] != 'verified')
        or (value['can_confirm'] and value['state'] != 'prepared')
        or (
            value['error_code'] is not None
            and (
                type(value['error_code']) is not str
                or value['error_code'] not in _MARKDOWN_ERRORS
            )
        )
    ):
        raise ValueError('Invalid Markdown publication.')
    return value


def _markdown_preview(value, publication):
    _fields(value, ('source_text', 'target_exists', 'target_text', 'can_confirm'))
    if type(value['source_text']) is not str:
        raise ValueError('Invalid complete Markdown preview.')
    raw = value['source_text'].encode('utf-8')
    source = publication['source']
    if (
        len(raw) != source['size_bytes']
        or hashlib.sha256(raw).hexdigest() != source['sha256']
        or type(value['target_exists']) is not bool
        or type(value['can_confirm']) is not bool
        or value['can_confirm'] != publication['can_confirm']
        or (value['target_exists'] and value['can_confirm'])
        or (not value['target_exists'] and value['target_text'] is not None)
        or (value['target_text'] is not None and type(value['target_text']) is not str)
    ):
        raise ValueError('The complete Markdown preview does not match its source.')
    if value['target_text'] is not None:
        value['target_text'].encode('utf-8')
    return value


def _markdown_source_identity(source):
    return source['kind'], source.get('agent', ''), source['chat_id']


def _markdown_update(value, workspace):
    _fields(value, ('id', 'binding_id', 'expected_head_id', 'previous_source',
        'source', 'target_path', 'state', 'created_at', 'updated_at',
        'current_verified', 'can_confirm', 'error_code', 'retained'))
    for key in ('id', 'binding_id', 'expected_head_id'):
        _uuid(value[key])
    previous = _markdown_source(value['previous_source'], workspace)
    source = _markdown_source(value['source'], workspace)
    if source['kind'] == 'history_reference' or previous['kind'] == 'history_reference':
        raise ValueError('A history reference is not a synchronizable snapshot.')
    _markdown_path(value['target_path'])
    for key in ('created_at', 'updated_at'):
        _text(value[key], 80)
    if (_markdown_source_identity(previous) != _markdown_source_identity(source)
            or max(previous['size_bytes'], source['size_bytes']) > 512 * 1024
            or value['state'] not in _MARKDOWN_STATES
            or type(value['current_verified']) is not bool
            or type(value['can_confirm']) is not bool
            or type(value['retained']) is not bool
            or (value['current_verified'] and value['state'] != 'verified')
            or (value['can_confirm'] and value['state'] != 'prepared')
            or (value['error_code'] is not None and (
                type(value['error_code']) is not str
                or value['error_code'] not in _MARKDOWN_UPDATE_ERRORS))):
        raise ValueError('Invalid Markdown update.')
    return value


def _markdown_update_preview(value, update):
    _fields(value, ('source_text', 'can_confirm'))
    if type(value['source_text']) is not str:
        raise ValueError('Invalid complete Markdown update preview.')
    raw = value['source_text'].encode('utf-8')
    source = update['source']
    if (len(raw) != source['size_bytes']
            or hashlib.sha256(raw).hexdigest() != source['sha256']
            or type(value['can_confirm']) is not bool
            or value['can_confirm'] != update['can_confirm']):
        raise ValueError('The complete Markdown update preview does not match its source.')
    return value


def _markdown_index_identity(value):
    """A source namespace is separate from its application chat UUID."""
    native = 'kind' in value or 'agent' in value
    if native and (value.get('kind') not in ('native_index', 'history_reference')
            or type(value.get('agent')) is not str or value['agent'] not in NATIVE_CHAT_AGENTS):
        raise ValueError('Invalid native Markdown identity.')
    return (value['kind'], value['agent']) if native else ('markdown_chat', '')


def _markdown_file(value):
    _fields(value, ('path', 'folder', 'name', 'rabbit_id', 'sha256', 'file_revision'),
            ('kind', 'agent'))
    namespace = _markdown_index_identity(value)
    _markdown_path(value['path'])
    _markdown_path(value['folder'], folder=True)
    _text(value['name'], 255)
    _uuid(value['rabbit_id'])
    _native_history_revision(value['sha256'])
    (_native_history_revision if namespace[0] != 'markdown_chat' else _markdown_revision)(value['file_revision'])
    if posixpath.dirname(value['path']) != value['folder']:
        raise ValueError('Invalid observed Markdown folder.')
    return value


def _markdown_inventory(value, workspace):
    _fields(
        value,
        (
            'workspace_id',
            'root',
            'complete',
            'chats',
            'unregistered',
            'issues',
            'files_examined',
            'files_read',
            'bytes_read',
            'publications',
            'source_chats',
            'source_chats_total',
            'source_chats_truncated',
        ),
        ('native_source_chats', 'native_source_chats_total', 'native_source_chats_truncated', 'bindings', 'folders'),
    )
    root = value['root']
    _text(root, 8192)
    if (
        not root.startswith('/')
        or posixpath.normpath(root) != root
        or type(value['complete']) is not bool
        or any(
            type(value[key]) is not list or len(value[key]) > maximum
            for key, maximum in (
                ('chats', 1000),
                ('unregistered', 1000),
                ('issues', 4096),
                ('publications', 1000),
                ('source_chats', 1000),
            )
        )
    ):
        raise ValueError('Invalid Markdown directory snapshot.')
    if 'folders' in value:
        folders = value['folders']
        if type(folders) is not list or len(folders) > 4096:
            raise ValueError('Invalid Markdown folders.')
        seen = set()
        for folder in folders:
            _markdown_folder_path(folder)
            if folder in seen:
                raise ValueError('Duplicate Markdown folder.')
            seen.add(folder)
    if (
        type(value['source_chats_total']) is not int
        or not 0 <= value['source_chats_total'] <= MAX_REVISION
        or value['source_chats_total'] < len(value['source_chats'])
        or type(value['source_chats_truncated']) is not bool
        or value['source_chats_truncated']
        != (value['source_chats_total'] > len(value['source_chats']))
    ):
        raise ValueError('Invalid common chat candidate coverage.')
    candidates = set()
    for row in value['source_chats']:
        _fields(row, ('id', 'title', 'revision', 'running'))
        _uuid(row['id'])
        _markdown_revision(row['revision'])
        if (
            row['id'] in candidates
            or type(row['title']) is not str
            or not 1 <= len(row['title']) <= 120
            or '\0' in row['title']
            or type(row['running']) is not bool
        ):
            raise ValueError('Invalid common chat metadata candidate.')
        row['title'].encode('utf-8')
        candidates.add(row['id'])
    native_fields = ('native_source_chats', 'native_source_chats_total', 'native_source_chats_truncated')
    if any(key in value for key in native_fields):
        if not all(key in value for key in native_fields):
            raise ValueError('Incomplete native chat candidate coverage.')
        rows, total, truncated = (value[key] for key in native_fields)
        if (type(rows) is not list or len(rows) > 1000 or type(total) is not int
                or not len(rows) <= total <= MAX_REVISION or type(truncated) is not bool
                or truncated != (total > len(rows))):
            raise ValueError('Invalid native chat candidate coverage.')
        seen = set()
        for row in rows:
            _fields(row, ('id', 'agent', 'title', 'source_count'))
            _uuid(row['id'])
            if (type(row['agent']) is not str or row['agent'] not in NATIVE_CHAT_AGENTS
                    or _native_chat_title(row['title']) != row['title']
                    or type(row['source_count']) is not int
                    or not 1 <= row['source_count'] <= (128 if row['agent'] == 'hermes' else 1)
                    or (row['agent'], row['id']) in seen):
                raise ValueError('Invalid native chat metadata candidate.')
            seen.add((row['agent'], row['id']))
    for key, maximum in (
        ('files_examined', 1000),
        ('files_read', 1000),
        ('bytes_read', 128 * 1024 * 1024),
    ):
        if type(value[key]) is not int or not 0 <= value[key] <= maximum:
            raise ValueError('Invalid Markdown scan count.')
    if value['files_read'] > value['files_examined'] or value['complete'] != (
        not bool(value['issues'])
    ):
        raise ValueError('Incomplete Markdown scan status.')
    identities = set()
    indexed = {}
    for row in value['chats']:
        _fields(
            row,
            ('rabbit_id', 'paths', 'source_revision', 'accepted_revision', 'status'),
            ('file', 'kind', 'agent'),
        )
        namespace = _markdown_index_identity(row)
        identity = (*namespace, row['rabbit_id'])
        _uuid(row['rabbit_id'])
        revision = _native_history_revision if namespace[0] != 'markdown_chat' else _markdown_revision
        revision(row['accepted_revision'])
        if row['source_revision'] is not None:
            revision(row['source_revision'])
        if (
            identity in identities
            or type(row['paths']) is not list
            or len(row['paths']) > 1000
            or len(set(row['paths'])) != len(row['paths'])
            or row['status']
            not in (
                'current',
                'duplicate',
                'unavailable',
                'missing',
                'external_modified',
                'registry_mismatch',
                'source_missing',
                'db_stale',
            )
        ):
            raise ValueError('Invalid registered Markdown identity.')
        identities.add(identity)
        indexed[identity] = row
        for path in row['paths']:
            _markdown_path(path)
        if (
            (row['status'] == 'duplicate' and len(row['paths']) < 2)
            or (row['status'] in ('missing', 'unavailable') and row['paths'])
            or (
                row['status'] not in ('duplicate', 'missing', 'unavailable')
                and len(row['paths']) != 1
            )
        ):
            raise ValueError('The Markdown status does not match observed paths.')
        if ('file' in row) != (len(row['paths']) == 1):
            raise ValueError('Ambiguous Markdown file binding.')
        if 'file' in row:
            file = _markdown_file(row['file'])
            if (file['rabbit_id'] != row['rabbit_id'] or file['path'] != row['paths'][0]
                    or _markdown_index_identity(file) != namespace):
                raise ValueError('Invalid Markdown file identity.')
    for row in value['unregistered']:
        _fields(
            row,
            (
                'path',
                'folder',
                'name',
                'rabbit_id',
                'sha256',
                'file_revision',
                'status',
            ),
            ('kind', 'agent'),
        )
        _markdown_file({key: item for key, item in row.items() if key != 'status'})
        if row['status'] not in ('foreign_workspace', 'unregistered'):
            raise ValueError('Invalid unregistered file.')
    for issue in value['issues']:
        _fields(issue, ('path', 'code'))
        _markdown_path(issue['path'], folder=True)
        if issue['code'] not in ('linked_entry', 'unsafe_file', 'invalid_document'):
            raise ValueError('Invalid Markdown scan issue.')
    publications = set()
    for row in value['publications']:
        _markdown_publication(row, workspace)
        if row['state'] == 'prepared' or row['id'] in publications:
            raise ValueError('Invalid accepted Markdown publication list.')
        publications.add(row['id'])
    if 'bindings' in value:
        bindings, seen, publication_ids = value['bindings'], set(), set()
        if type(bindings) is not list or len(bindings) > 1000:
            raise ValueError('Invalid Markdown bindings.')
        for binding in bindings:
            _fields(binding, ('publication_id', 'head_id', 'source', 'target_path', 'accepted_at'))
            _uuid(binding['publication_id'])
            _uuid(binding['head_id'])
            source = _markdown_source(binding['source'], workspace)
            identity = _markdown_source_identity(source)
            _markdown_path(binding['target_path'])
            _text(binding['accepted_at'], 80)
            row = indexed.get(identity)
            if (identity in seen or binding['publication_id'] in publication_ids or row is None
                    or source['revision'] != row['accepted_revision']):
                raise ValueError('The Markdown binding does not match its scanned identity.')
            seen.add(identity)
            publication_ids.add(binding['publication_id'])
    return value


def _markdown_route(path):
    return (
        path == MARKDOWN_CHATS
        or path.startswith(MARKDOWN_CHATS + '/')
        or re.fullmatch(r'/api/chats/[0-9a-f-]{36}/markdown-preview', path) is not None
    )


def _native_chat_title(value):
    # Same catalogue rule; preserve the caller's original expected_title for CAS.
    if (type(value) is not str or not 1 <= len(value.strip()) <= 80
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError('Invalid native chat title.')
    return value.strip()


def validate_native_chat_rename(agent, chat_id, expected_title, title):
    if type(agent) is not str or agent not in NATIVE_CHAT_AGENTS:
        raise ValueError('Invalid native chat agent.')
    _uuid(chat_id); _native_chat_title(expected_title); _native_chat_title(title)
    return {'agent': agent, 'chat_id': chat_id, 'expected_title': expected_title, 'title': title}


def _native_chat(row):
    _fields(row, ('id', 'title', 'agent', 'role', 'created_at'))
    if (type(row['agent']) is not str or row['agent'] not in NATIVE_CHAT_AGENTS
            or row['role'] != 'chat' or _native_chat_title(row['title']) != row['title']):
        raise ValueError('Invalid native chat row.')
    _uuid(row['id']); _text(row['created_at'], 80)
    return row


def _native_chat_scope(value):
    if value.get('scope') != 'native_chat_names' or value.get('inference_performed') is not False:
        raise ValueError('Invalid native chat names scope.')


def validate_native_chat_configuration(value):
    """One ordinary native chat's saved next-turn selection, never an Agent run."""
    if type(value) is not dict or type(value.get('operation')) is not str:
        raise ValueError('Invalid native chat configuration operation.')
    operation = value['operation']
    required = ('operation', 'agent', 'chat_id')
    if operation == 'select':
        required += ('expected_revision', 'ai_profile_id', 'ai_profile_revision')
    elif operation != 'read':
        raise ValueError('Invalid native chat configuration operation.')
    _fields(value, required)
    if type(value['agent']) is not str or value['agent'] not in NATIVE_CHAT_SELECTION_AGENTS:
        raise ValueError('Invalid native chat configuration agent.')
    _uuid(value['chat_id'])
    if operation == 'select':
        _revision(value['expected_revision'], minimum=0)
        profile_id, profile_revision = value['ai_profile_id'], value['ai_profile_revision']
        if (profile_id is None) != (profile_revision is None):
            raise ValueError('A profile identity and revision must be paired.')
        if profile_id is not None:
            _uuid(profile_id)
            _revision(profile_revision)
    return dict(value)


def _native_chat_configuration(value, workspace, agent, chat_id):
    _fields(value, ('workspace_id', 'agent', 'chat_id', 'selection', 'profile',
                    'profile_current', 'scope', 'inference_performed', 'connection_checked'))
    if (value['workspace_id'] != workspace or value['agent'] != agent or value['chat_id'] != chat_id
            or value['scope'] != 'native_next_turn_selection'
            or value['inference_performed'] is not False or value['connection_checked'] is not False
            or type(value['profile_current']) is not bool):
        raise ValueError('Invalid native chat configuration scope.')
    selection = value['selection']
    _fields(selection, ('revision', 'ai_profile_id', 'ai_profile_revision'))
    _revision(selection['revision'], minimum=0)
    profile_id, profile_revision = selection['ai_profile_id'], selection['ai_profile_revision']
    if (profile_id is None) != (profile_revision is None):
        raise ValueError('Invalid native chat profile reference.')
    if profile_id is not None:
        _uuid(profile_id)
        _revision(profile_revision)
    profile, current = value['profile'], value['profile_current']
    if selection['revision'] == 0 or profile_id is None:
        if profile_id is not None or profile is not None or current is not True:
            raise ValueError('Invalid inherited native chat configuration.')
    elif current:
        _fields(profile, ('id', 'revision') + PROFILE_FIELDS)
        _uuid(profile['id'])
        _revision(profile['revision'])
        normalized = _profile_fields({key: profile[key] for key in PROFILE_FIELDS})
        if (profile['id'] != profile_id or profile['revision'] != profile_revision
                or profile['agent_instance_id'] != 'local/' + agent
                or any(profile[key] != normalized[key] for key in PROFILE_FIELDS)):
            raise ValueError('Invalid native chat profile snapshot.')
    elif profile is not None:
        # The registry stores references, not a historical parameter snapshot.
        raise ValueError('A stale native chat profile must not include parameters.')
    return value


def validate_native_chat_memory_configuration(value):
    """An ordinary Codex chat's additional read-only memory for its next turn."""
    if type(value) is not dict or type(value.get('operation')) is not str:
        raise ValueError('Invalid native chat memory operation.')
    operation = value['operation']
    required = ('operation', 'agent', 'chat_id')
    if operation == 'select':
        required += ('expected_revision', 'memory_profile_id', 'memory_profile_revision')
    elif operation != 'read':
        raise ValueError('Invalid native chat memory operation.')
    _fields(value, required)
    if type(value['agent']) is not str or value['agent'] != 'codex':
        raise ValueError('Invalid native chat memory agent.')
    _uuid(value['chat_id'])
    if operation == 'select':
        _revision(value['expected_revision'], minimum=0)
        profile_id, profile_revision = value['memory_profile_id'], value['memory_profile_revision']
        if (profile_id is None) != (profile_revision is None):
            raise ValueError('A memory profile identity and revision must be paired.')
        if profile_id is not None:
            _uuid(profile_id)
            _revision(profile_revision)
    return dict(value)


def _native_chat_memory_configuration(value, workspace, agent, chat_id):
    _fields(value, ('workspace_id', 'agent', 'chat_id', 'selection', 'profile',
                    'profile_current', 'scope', 'inference_performed', 'connection_checked'))
    if (value['workspace_id'] != workspace or value['agent'] != agent or value['chat_id'] != chat_id
            or value['scope'] != 'native_next_turn_memory_selection'
            or value['inference_performed'] is not False or value['connection_checked'] is not False
            or type(value['profile_current']) is not bool):
        raise ValueError('Invalid native chat memory configuration scope.')
    selection = value['selection']
    _fields(selection, ('revision', 'memory_profile_id', 'memory_profile_revision'))
    _revision(selection['revision'], minimum=0)
    profile_id, profile_revision = selection['memory_profile_id'], selection['memory_profile_revision']
    if (profile_id is None) != (profile_revision is None):
        raise ValueError('Invalid native chat memory profile reference.')
    if profile_id is not None:
        _uuid(profile_id)
        _revision(profile_revision)
    profile, current = value['profile'], value['profile_current']
    if selection['revision'] == 0 or profile_id is None:
        if profile_id is not None or profile is not None or current is not True:
            raise ValueError('Invalid empty native chat memory configuration.')
    elif current:
        profile = _memory_profile(profile)
        if (profile['id'] != profile_id or profile['revision'] != profile_revision
                or profile['mode'] != 'read_only'):
            raise ValueError('Invalid read-only native chat memory profile.')
    elif profile is not None:
        raise ValueError('A stale native memory reference must not include parameters.')
    return value


def _native_history_revision(value):
    if type(value) is not str or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('Invalid native history preview revision.')
    return value


def validate_native_history_source(agent, session_id):
    if type(agent) is not str or agent not in NATIVE_HISTORY_MODES:
        raise ValueError('Invalid native history agent.')
    if agent in ('codex', 'claude'):
        return _uuid(session_id)
    pattern = (r'[A-Za-z0-9](?:[A-Za-z0-9._-]{0,254}[A-Za-z0-9])?' if agent == 'pi'
               else r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
    if type(session_id) is not str or not re.fullmatch(pattern, session_id):
        raise ValueError('Invalid native history source.')
    return session_id


def _native_identity(agent, chat_id, epoch):
    if (type(agent) is not str or agent not in _NATIVE_LIMITS or type(epoch) is not str
            or not re.fullmatch(r'[a-f0-9]{32}', epoch)):
        raise ValueError('Invalid native connection.')
    return {'agent': agent, 'chat_id': _uuid(chat_id), 'epoch': epoch}


def validate_native_model_preparation(agent, chat_id):
    if type(agent) is not str or agent not in _NATIVE_LIMITS or type(chat_id) is not str:
        raise ValueError('Invalid native model preparation target.')
    return {'agent': agent, 'chat_id': _uuid(chat_id)}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate local response field.')
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError('Nonfinite local response value.')


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:
        raise ValueError('Invalid identity.')
    return value


def _revision(value, *, minimum=1):
    if type(value) is not int or not minimum <= value <= MAX_REVISION:
        raise ValueError('Invalid revision.')
    return value


def _fields(value, required, optional=()):
    if type(value) is not dict or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError('Invalid fields.')


def _text(value, maximum, *, empty=False):
    if (type(value) is not str or not (0 if empty else 1) <= len(value) <= maximum
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('Invalid text.')
    value.encode('utf-8')
    return value


def validate_settings_update(expected_revision, patch):
    if type(expected_revision) is not str or not re.fullmatch(r'(?:0|[a-f0-9]{32})', expected_revision):
        raise ValueError('Invalid settings revision.')
    if type(patch) is not dict or not patch or patch.keys() - SETTINGS_OPTIONS.keys():
        raise ValueError('Invalid settings patch.')
    for key, value in patch.items():
        required_type = bool if key in ('markdown', 'show_internal') else str
        if type(value) is not required_type or value not in SETTINGS_OPTIONS[key]:
            raise ValueError('Invalid settings value.')
    return {'expected_revision': expected_revision, 'patch': dict(patch)}


def _settings(value):
    """Bounded public snapshot; no private chat-routing preferences or nonce."""
    _fields(value, ('workspace_id', 'scope', 'inference_performed', 'revision', 'settings', 'catalogue'))
    if value['scope'] != 'application_preferences' or value['inference_performed'] is not False:
        raise ValueError('Invalid settings scope.')
    if type(value['settings']) is not dict or value['settings'].keys() != SETTINGS_OPTIONS.keys():
        raise ValueError('Invalid settings projection.')
    validate_settings_update(value['revision'], value['settings'])
    catalogue = value['catalogue']
    _fields(catalogue, ('schema_version', 'read_tool', 'update_tool', 'fields'))
    if (type(catalogue['schema_version']) is not int or catalogue['schema_version'] != 1
            or catalogue['read_tool'] != 'rabbit_settings_read' or catalogue['update_tool'] != 'rabbit_settings_update'
            or type(catalogue['fields']) is not list or len(catalogue['fields']) != len(SETTINGS_OPTIONS)):
        raise ValueError('Invalid settings catalogue.')
    seen = set()
    for row in catalogue['fields']:
        _fields(row, ('key', 'label', 'description', 'type', 'allowed_values', 'default', 'surface', 'example'))
        key = row['key']
        if type(key) is not str or key not in SETTINGS_OPTIONS or key in seen:
            raise ValueError('Invalid setting field.')
        seen.add(key)
        _text(row['label'], 80); _text(row['description'], 600)
        boolean = key in ('markdown', 'show_internal')
        if (row['type'] != ('boolean' if boolean else 'string')
                or row['surface'] != ('ai_only' if boolean else 'human_and_ai')
                or type(row['allowed_values']) is not list
                or len(row['allowed_values']) != len(SETTINGS_OPTIONS[key])
                or any(type(item) is not (bool if boolean else str) for item in row['allowed_values'])
                or row['allowed_values'] != list(SETTINGS_OPTIONS[key])):
            raise ValueError('Invalid setting choices.')
        validate_settings_update('0', {key: row['default']})
        example = row['example']
        _fields(example, ('expected_revision', 'patch'))
        if example['expected_revision'] != '0' or type(example['patch']) is not dict or set(example['patch']) != {key}:
            raise ValueError('Invalid settings example.')
        validate_settings_update(example['expected_revision'], example['patch'])
    return value


def _native_capabilities(value):
    """Historical native candidates, independent of text-route availability.

    Only known public projections enter the management tool. Native references are
    recomputed from the exact provider/model pair; they never become a text
    model name or imply API entitlement.
    """
    _fields(value, (), ('codex', 'pi', 'hermes'))
    for backend, source in value.items():
        _fields(source, ('enabled', 'source', 'catalogue_available', 'observed_at', 'source_chat_id', 'models'))
        expected = {'codex': 'codex/model/list', 'pi': 'pi/get_available_models',
                    'hermes': 'hermes/model.options'}[backend]
        if (type(source['enabled']) is not bool or type(source['catalogue_available']) is not bool
                or source['source'] != expected or type(source['models']) is not list
                or len(source['models']) > (800 if backend == 'codex' else 4096)):
            raise ValueError('Invalid native candidates.')
        if source['catalogue_available']:
            if not source['enabled']:
                raise ValueError('Disabled native directory.')
            if backend != 'codex' or source['source_chat_id'] is not None:
                if type(source['source_chat_id']) is not str:
                    raise ValueError('Invalid native directory source.')
                _uuid(source['source_chat_id'])
            _text(source['observed_at'], 80)
        elif source['models'] or source['observed_at'] is not None or source['source_chat_id'] is not None:
            raise ValueError('Unobserved native candidates.')
        seen = set()
        for row in source['models']:
            columns = ('id', 'agent_id', 'model', 'name', 'supported_efforts', 'default_effort', 'explicit_effort_supported')
            extra = {'codex': (), 'pi': ('provider', 'native_thinking_levels', 'effort_source'),
                     'hermes': ('provider', 'effort_source')}[backend]
            _fields(row, columns + extra, ('input',) if backend == 'pi' else ())
            if 'input' in row and (type(row['input']) is not list or any(
                    type(item) is not str or item not in ('text', 'image') for item in row['input'])):
                raise ValueError('Invalid native input modalities.')
            model = _text(row['model'], 192 if backend == 'codex' else 1024)
            if (row['agent_id'] != backend or type(row['id']) is not str or row['id'] in seen
                    or type(row['explicit_effort_supported']) is not bool
                    or type(row['supported_efforts']) is not list or len(row['supported_efforts']) > 64):
                raise ValueError('Invalid native model candidate.')
            seen.add(row['id']); _text(row['name'], 1539 if backend == 'hermes' else 512)
            for effort in row['supported_efforts']:
                _text(effort, 256)
            if (len(set(row['supported_efforts'])) != len(row['supported_efforts'])
                    or row['explicit_effort_supported'] != bool(row['supported_efforts'])):
                raise ValueError('Invalid native effort candidates.')
            if backend == 'codex':
                if not re.fullmatch(r'[^\x00-\x20\x7f]{1,192}', model) or row['id'] != 'local/codex/'+model:
                    raise ValueError('Invalid Codex model identity.')
                _text(row['default_effort'], 256)
            else:
                provider = _text(row['provider'], 256)
                pair = json.dumps({'provider': provider, 'model': model}, ensure_ascii=False,
                                  sort_keys=True, separators=(',', ':'), allow_nan=False)
                if row['id'] != 'local/' + backend + '/native/' + hashlib.sha256(pair.encode('utf-8')).hexdigest():
                    raise ValueError('Invalid native model identity.')
                if backend == 'hermes':
                    if (row['supported_efforts'] != list(EFFORTS) or row['default_effort'] is not None
                            or row['explicit_effort_supported'] is not True
                            or row['effort_source'] != HERMES_EFFORT_SOURCE
                            or any(not text.strip() or any(unicodedata.category(char)
                                in ('Cc', 'Cf', 'Cs') for char in text)
                                for text in (provider, model, row['name']))):
                        raise ValueError('Invalid Hermes native request strength.')
                    continue
                levels = row['native_thinking_levels']
                allowed = ('off', 'minimal', 'low', 'medium', 'high', 'xhigh')
                if (type(levels) is not list or len(levels) > len(allowed)
                        or any(type(level) is not str or level not in allowed for level in levels)
                        or len(set(levels)) != len(levels) or row['default_effort'] is not None
                        or row['supported_efforts'] != ['none' if level == 'off' else level for level in levels]):
                    raise ValueError('Invalid Pi thinking levels.')
                _text(row['effort_source'], 256)


def _profile_fields(value, *, partial=False):
    _fields(value, () if partial else ('name', 'agent_instance_id'), PROFILE_FIELDS)
    result = dict(value) if partial else {'device_id': 'local', 'model_id': '', 'reasoning_effort': None} | value
    if 'name' in result:
        result['name'] = _text(result['name'], 40).strip()
        if not result['name']:
            raise ValueError('Invalid name.')
    if 'agent_instance_id' in result and not re.fullmatch(r'local/[a-z][a-z0-9_-]{0,31}', _text(result['agent_instance_id'], 38)):
        raise ValueError('Invalid Agent identity.')
    if 'device_id' in result and (type(result['device_id']) is not str or result['device_id'] != 'local'):
        raise ValueError('Invalid device.')
    if 'model_id' in result:
        model = _text(result['model_id'], 220, empty=True)
        match = re.fullmatch(r'local/([a-z][a-z0-9_-]{0,31})/[^\x00-\x20\x7f]{1,192}', model) if model else None
        if model and (match is None or 'agent_instance_id' in result and result['agent_instance_id'] != 'local/' + match[1]):
            raise ValueError('Invalid model identity.')
    if 'reasoning_effort' in result and result['reasoning_effort'] is not None and (
            type(result['reasoning_effort']) is not str or result['reasoning_effort'] not in EFFORTS):
        raise ValueError('Invalid effort.')
    return result


def validate_operation(value):
    """Pure bounded input codec; capability validation remains in the app."""
    if type(value) is not dict or type(value.get('action')) is not str:
        raise ValueError('Invalid operation.')
    action = value['action']
    required = {'create': ('action', 'creation_id', 'profile'),
        'update': ('action', 'profile_id', 'revision', 'changes'),
        'copy': ('action', 'creation_id', 'profile_id', 'revision', 'changes'),
        'set_default': ('action', 'profile_id', 'default_revision')}.get(action)
    if required is None:
        raise ValueError('Invalid operation.')
    _fields(value, required)
    result = dict(value)
    for key in ('creation_id', 'profile_id'):
        if key in result:
            _uuid(result[key])
    if action == 'create':
        result['profile'] = _profile_fields(result['profile'])
    elif action in ('update', 'copy'):
        _revision(result['revision'])
        result['changes'] = _profile_fields(result['changes'], partial=True)
        if action == 'update' and not result['changes']:
            raise ValueError('Empty update.')
    else:
        _revision(result['default_revision'], minimum=0)
    return result


def _creation_sha(operation):
    body = {key: value for key, value in operation.items() if key != 'creation_id'}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _memory_fields(value, *, partial=False):
    _fields(value, () if partial else ('name',), MEMORY_MUTABLE if partial else MEMORY_FIELDS)
    result = dict(value) if partial else dict(description='', habits='', program='hindsight',
        connection_id='local/hindsight', mode='read_only') | value
    if 'name' in result:
        result['name'] = _text(result['name'], 40).strip()
        if not result['name']: raise ValueError('Invalid memory name.')
    for key, maximum in (('description', 200), ('habits', 4000)):
        if key not in result: continue
        text = result[key]
        if (type(text) is not str or len(text) > maximum
                or any((ord(c) < 32 and ord(c) not in (9, 10, 13)) or ord(c) == 127 for c in text)):
            raise ValueError('Invalid memory text.')
        text.encode('utf-8', 'strict')
    for key, allowed in (('program', ('hindsight',)), ('connection_id', ('local/hindsight',)),
                         ('mode', ('read_only', 'read_write'))):
        if key in result and (type(result[key]) is not str or result[key] not in allowed):
            raise ValueError('Invalid memory binding.')
    return result


def validate_memory_operation(value):
    """Saved parameters only; no bank identity, credentials or service probing."""
    if type(value) is not dict or type(value.get('action')) is not str:
        raise ValueError('Invalid memory operation.')
    action = value['action']
    required = {'create': ('action', 'creation_id', 'profile'),
        'update': ('action', 'profile_id', 'revision', 'changes'),
        'copy': ('action', 'profile_id', 'revision', 'creation_id', 'changes')}.get(action)
    if required is None: raise ValueError('Invalid memory operation.')
    _fields(value, required); result = dict(value)
    for key in ('creation_id', 'profile_id'):
        if key in result: _uuid(result[key])
    if action == 'create': result['profile'] = _memory_fields(result['profile'])
    else:
        _revision(result['revision']); result['changes'] = _memory_fields(result['changes'], partial=True)
        if action == 'update' and not result['changes']: raise ValueError('Empty memory update.')
    return result


def _memory_profile(value):
    _fields(value, MEMORY_COLUMNS); _uuid(value['id']); _revision(value['revision'])
    normalized = _memory_fields({key: value[key] for key in MEMORY_FIELDS})
    if (any(value[key] != normalized[key] for key in MEMORY_FIELDS)
            or value['bank_ref'] != 'mem_' + uuid.UUID(value['id']).hex
            or type(value['locked_mode']) is not bool
            or type(value['source_kind']) is not str or value['source_kind'] not in ('owned', 'hermes_hindsight_readonly')
            or value['locked_mode'] != (value['source_kind'] == 'hermes_hindsight_readonly')
            or value['locked_mode'] and value['mode'] != 'read_only'):
        raise ValueError('Invalid memory profile.')
    return dict(value)


def _memory_scope(value):
    if (value.get('scope') != 'saved_memory_parameters' or value.get('connection_checked') is not False
            or value.get('inference_performed') is not False):
        raise ValueError('Invalid memory scope.')


def _profile(value):
    _fields(value, PROFILE_COLUMNS)
    _uuid(value['id']); _revision(value['revision'])
    normalized = _profile_fields({key: value[key] for key in PROFILE_FIELDS})
    if any(value[key] != normalized[key] for key in PROFILE_FIELDS):
        raise ValueError('Noncanonical profile.')
    for key in ('created_at', 'updated_at'):
        _text(value[key], 64)
    return dict(value)


def _default(value):
    _fields(value, ('ai_profile_id', 'revision'))
    if value['ai_profile_id'] is not None:
        _uuid(value['ai_profile_id'])
    _revision(value['revision'], minimum=0)
    return dict(value)


class ManagementClientError(RuntimeError):
    def __init__(self, code, outcome, *, current_profile=None, current_default_state=None, current_settings=None,
                 current_chat=None, current_configuration=None, current_memory_configuration=None):
        self.code, self.outcome = code, outcome
        self.current_profile = current_profile
        self.current_default_state = current_default_state
        self.current_settings = current_settings
        self.current_chat = current_chat
        self.current_configuration = current_configuration
        self.current_memory_configuration = current_memory_configuration
        super().__init__(code)

    def result(self):
        value = {'ok': False, 'code': self.code, 'outcome': self.outcome,
                 'automatically_retried': False}
        if self.current_profile is not None:
            value['current_profile'] = self.current_profile
        if self.current_default_state is not None:
            value['current_default_state'] = self.current_default_state
        if self.current_settings is not None:
            value['current_settings'] = self.current_settings
        if self.current_chat is not None:
            value['current_chat'] = self.current_chat
        if self.current_configuration is not None:
            value['current_configuration'] = self.current_configuration
        if self.current_memory_configuration is not None:
            value['current_memory_configuration'] = self.current_memory_configuration
        return value


class ManagementClient:
    def __init__(self, *, port, workspace_id):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('A local service port is required.')
        if (type(workspace_id) is not str or not 1 <= len(workspace_id) <= 128
                or any(ord(c) < 33 or ord(c) == 127 for c in workspace_id)):
            raise ValueError('An exact workspace identity is required.')
        self.port, self.workspace_id = port, workspace_id

    def _request(self, method, path, *, body=None, csrf=None):
        mutation = method != 'GET'
        data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        headers = {'Accept': 'application/json'}
        if mutation:
            headers.update({'Content-Type': 'application/json',
                'Origin': 'http://127.0.0.1:' + str(self.port), 'X-OneAI-CSRF': csrf})
        timeout = 60 if method == 'POST' and path == NATIVE_MODEL_PREPARATION else 10
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout)
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            limit = (
                MARKDOWN_RESPONSE_BYTES
                if _markdown_route(path)
                else MEMORY_RESPONSE_BYTES
                if path == MEMORY_PROFILES
                else MAX_RESPONSE_BYTES
            )
            raw = response.read(limit + 1)
            if (len(raw) > limit or
                response.getheader('Content-Type', '').split(';')[0].lower() != 'application/json'):
                raise ValueError('Invalid local response.')
            value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            if type(value) is not dict:
                raise ValueError('Invalid local response.')
            if not 200 <= response.status < 300:
                if path.split('?', 1)[0] == NATIVE_CHAT_MEMORY_CONFIGURATION:
                    outcome = 'unknown' if mutation else 'unavailable'
                    if value.get('workspace_id') != self.workspace_id:
                        raise ManagementClientError('workspace_changed', outcome)
                    code = value.get('code')
                    known = {400: ('native_chat_memory_selection_invalid',),
                        404: ('native_chat_memory_selection_not_found',),
                        409: ('native_chat_memory_selection_conflict', 'native_chat_memory_selection_corrupt'),
                        503: ('native_chat_memory_selection_unavailable',)}
                    conflict = mutation and response.status == 409 and code == 'native_chat_memory_selection_conflict'
                    _fields(value, ('workspace_id', 'error', 'code', 'accepted'),
                            ('current_memory_configuration',) if conflict else ())
                    if (type(value['error']) is not str or type(code) is not str
                            or code not in known.get(response.status, ()) or value['accepted'] is not False):
                        raise ValueError('Invalid native chat memory configuration error.')
                    current = None
                    if conflict:
                        current = _native_chat_memory_configuration(value.get('current_memory_configuration'),
                            self.workspace_id, body['agent'], body['chat_id'])
                    elif code == 'native_chat_memory_selection_conflict':
                        raise ValueError('Invalid native chat memory configuration conflict.')
                    raise ManagementClientError(code, 'rejected' if mutation else 'unavailable',
                        current_memory_configuration=current)
                if method == 'POST' and path == NATIVE_MODEL_PREPARATION:
                    # Opening may already have happened before directory read
                    # failed; even a 4xx/accepted:false is not an unsent proof.
                    if 'workspace_id' in value and value['workspace_id'] != self.workspace_id:
                        raise ManagementClientError('workspace_changed', 'unknown')
                    code = value.get('code')
                    known = ('native_model_catalog_busy', 'native_model_catalog_changed', 'native_model_catalog_unavailable')
                    raise ManagementClientError(code if type(code) is str and code in known
                        else 'native_model_catalog_unavailable', 'unknown')
                if path.split('?', 1)[0] == NATIVE_CHAT_CONFIGURATION:
                    outcome = 'unknown' if mutation else 'unavailable'
                    if value.get('workspace_id') != self.workspace_id:
                        raise ManagementClientError('workspace_changed', outcome)
                    code = value.get('code')
                    conflict = mutation and response.status == 409 and code == 'native_chat_selection_conflict'
                    _fields(value, ('workspace_id', 'error', 'code', 'accepted'),
                            ('current_configuration',) if conflict else ())
                    if (type(value['error']) is not str or type(code) is not str
                            or code not in _NATIVE_CHAT_SELECTION_ERRORS or value['accepted'] is not False):
                        raise ValueError('Invalid native chat configuration error.')
                    current = None
                    if conflict:
                        try:
                            current = _native_chat_configuration(value.get('current_configuration'),
                                self.workspace_id, body['agent'], body['chat_id'])
                        except (ValueError, TypeError, UnicodeError, AttributeError):
                            raise ValueError('Invalid native chat configuration conflict.') from None
                    elif code == 'native_chat_selection_conflict':
                        raise ValueError('Invalid native chat configuration conflict.')
                    raise ManagementClientError(code, 'unavailable' if not mutation else
                        'rejected' if 400 <= response.status < 500 else 'unknown', current_configuration=current)
                if _markdown_route(path):
                    outcome = 'unknown' if mutation else 'unavailable'
                    if (
                        'workspace_id' in value
                        and value['workspace_id'] != self.workspace_id
                    ):
                        raise ManagementClientError('workspace_changed', outcome)
                    code = value.get('code')
                    raise ManagementClientError(
                        code
                        if type(code) is str and code in _MARKDOWN_ERRORS
                        else 'markdown_chat_unavailable',
                        outcome,
                    )
                if path in NATIVE_HISTORY_PATHS.values():
                    outcome = 'unknown' if path.endswith('/adopt') else 'unavailable'
                    if 'workspace_id' in value and value['workspace_id'] != self.workspace_id:
                        raise ManagementClientError('workspace_changed', outcome)
                    _fields(value, ('error', 'code'), ('workspace_id', 'accepted', 'result_unknown'))
                    if (type(value['error']) is not str or 'accepted' in value and value['accepted'] is not False
                            or 'result_unknown' in value and value['result_unknown'] is not True):
                        raise ValueError('Invalid native history error.')
                    known = ('native_history_invalid', 'native_history_busy', 'native_history_disabled',
                             'native_history_unavailable')
                    code = value['code']
                    if type(code) is not str or code not in known:
                        code = 'native_history_unavailable'
                    # Registration crosses stores: accepted:false is not a
                    # durable proof that no reservation/publication occurred.
                    raise ManagementClientError(code, outcome)
                if method == 'GET' and path.startswith(MEMORY_CONNECTIONS):
                    if value.get('workspace_id') != self.workspace_id:
                        raise ManagementClientError('workspace_changed', 'unavailable')
                    if response.status == 409:
                        _fields(value, ('workspace_id', 'code', 'profile', 'error'))
                        target = re.fullmatch(re.escape(MEMORY_CONNECTIONS) + r'([^/?]+)\?revision=([1-9][0-9]*)', path)
                        current = _memory_profile(value['profile'])
                        if (target is None or value['code'] != 'memory_profile_version_conflict'
                                or type(value['error']) is not str or current['id'] != target[1]
                                or current['revision'] == int(target[2])):
                            raise ValueError('Invalid memory connection conflict.')
                        raise ManagementClientError('memory_profile_version_conflict', 'rejected', current_profile=current)
                    _fields(value, ('workspace_id', 'error'))
                    if type(value['error']) is not str: raise ValueError('Invalid memory connection error.')
                    raise ManagementClientError('request_rejected' if 400 <= response.status < 500
                        else 'service_error', 'unavailable')
                if path == NATIVE_CHAT_NAMES:
                    if value.get('workspace_id') != self.workspace_id:
                        raise ManagementClientError('workspace_changed', 'unknown' if mutation else 'unavailable')
                    code = value.get('code')
                    conflict = response.status == 409 and code == 'native_chat_name_conflict' and mutation
                    _fields(value, ('workspace_id', 'scope', 'inference_performed', 'error', 'code', 'accepted'),
                            ('current_chat',) if conflict else ())
                    _native_chat_scope(value)
                    known = ('native_chat_name_invalid', 'native_chat_name_disabled', 'native_chat_name_busy',
                             'native_chat_name_unavailable', 'native_chat_not_found', 'native_chat_name_conflict')
                    if type(code) is not str or code not in known or value['accepted'] is not False:
                        raise ValueError('Invalid native chat names error.')
                    current = None
                    if conflict:
                        current = _native_chat(value.get('current_chat'))
                        if (current['id'] != body['chat_id'] or current['agent'] != body['agent']
                                or current['title'] == body['expected_title']):
                            raise ValueError('Invalid native chat names conflict.')
                    elif code == 'native_chat_name_conflict':
                        raise ValueError('Invalid native chat names conflict.')
                    raise ManagementClientError(code, 'unavailable' if not mutation else
                        'rejected' if 400 <= response.status < 500 else 'unknown', current_chat=current)
                if method == 'GET' and path.split('?', 1)[0] in (
                        NATIVE_CONNECTIONS, NATIVE_CATALOG, SPECIALIST_MODEL_CATALOG):
                    if 'workspace_id' in value and value['workspace_id'] != self.workspace_id:
                        raise ManagementClientError('workspace_changed', 'unavailable')
                    code = value.get('code')
                    known = ('native_model_catalog_busy', 'native_model_catalog_changed', 'native_model_catalog_unavailable')
                    raise ManagementClientError(code if response.status == 409 and code in known
                        else 'native_model_catalog_unavailable', 'unavailable')
                if ((response.status == 409 or path in (OPERATIONS, SETTINGS, MEMORY_OPERATIONS) and 400 <= response.status < 500)
                        and value.get('workspace_id') != self.workspace_id):
                    raise ManagementClientError('workspace_changed', 'unknown' if mutation else 'unavailable')
                if response.status == 409 and path == MEMORY_OPERATIONS and method == 'POST':
                    code = value.get('code')
                    allowed = {'create': ('memory_profile_creation_conflict',),
                        'update': ('memory_profile_version_conflict',),
                        'copy': ('memory_profile_version_conflict', 'memory_profile_creation_conflict')}[body['action']]
                    if code not in allowed: raise ValueError('Invalid memory conflict.')
                    field = 'profile' if code == 'memory_profile_version_conflict' else 'creation_id'
                    _fields(value, ('workspace_id', 'code', field), ('error',))
                    current = _memory_profile(value['profile']) if field == 'profile' else None
                    if (current is not None and current['id'] != body['profile_id']
                            or field == 'creation_id' and value['creation_id'] != body['creation_id']):
                        raise ValueError('Invalid memory conflict target.')
                    raise ManagementClientError(code, 'rejected', current_profile=current)
                if method == 'GET' and (path == MEMORY_PROFILES or path.startswith(MEMORY_CREATIONS)):
                    if value.get('workspace_id') != self.workspace_id:
                        raise ManagementClientError('workspace_changed', 'unavailable')
                    _fields(value, ('workspace_id', 'error'))
                    if type(value['error']) is not str: raise ValueError('Invalid memory read error.')
                    raise ManagementClientError('request_rejected' if 400 <= response.status < 500
                        else 'service_error', 'unavailable')
                if response.status == 409 and path == OPERATIONS and method == 'POST':
                    code = value.get('code')
                    allowed = {'create': ('profile_creation_conflict',), 'update': ('profile_version_conflict',),
                        'copy': ('profile_version_conflict', 'profile_creation_conflict'),
                        'set_default': ('default_version_conflict',)}[body['action']]
                    if code not in allowed:
                        raise ValueError('Invalid operation conflict.')
                    field = {'profile_version_conflict': 'profile', 'default_version_conflict': 'default_state',
                             'profile_creation_conflict': 'creation_id'}[code]
                    _fields(value, ('workspace_id', 'code', field), ('error',))
                    current = _profile(value['profile']) if 'profile' in value else None
                    default = _default(value['default_state']) if 'default_state' in value else None
                    if code == 'profile_version_conflict' and (current is None or current['id'] != body['profile_id']):
                        raise ValueError('Invalid profile conflict.')
                    if code == 'default_version_conflict' and default is None:
                        raise ValueError('Invalid default conflict.')
                    if code == 'profile_creation_conflict' and (value.get('creation_id') != body['creation_id']
                            or current is not None or default is not None):
                        raise ValueError('Invalid creation conflict.')
                    raise ManagementClientError(code, 'rejected', current_profile=current, current_default_state=default)
                if response.status == 409 and path == SETTINGS and method == 'POST':
                    _fields(value, ('workspace_id', 'code', 'current_settings'), ('error',))
                    if value['code'] != 'settings_version_conflict':
                        raise ValueError('Invalid settings conflict.')
                    current = _settings(value['current_settings'])
                    if current['workspace_id'] != self.workspace_id:
                        raise ManagementClientError('workspace_changed', 'unknown')
                    raise ManagementClientError('settings_version_conflict', 'rejected', current_settings=current)
                rejected = 400 <= response.status < 500
                raise ManagementClientError('version_conflict' if response.status == 409 else 'request_rejected'
                    if rejected else 'service_error', 'rejected' if rejected and mutation else 'unknown'
                    if mutation else 'unavailable', current_profile=value.get('profile') if response.status == 409 else None)
            return value
        except ManagementClientError:
            raise
        except (OSError, http.client.HTTPException, ValueError, UnicodeError, RecursionError):
            raise ManagementClientError('response_unconfirmed', 'unknown' if mutation else 'unavailable') from None
        finally:
            connection.close()

    def _binding(self):
        value = self._request('GET', '/api/management/context')
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', 'not_sent')
        token = value.get('csrf_token')
        if type(token) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', token):
            raise ManagementClientError('service_identity_unavailable', 'not_sent')
        return token

    def _inventory(self, value):
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', 'unavailable')
        if (value.get('scope') != 'saved_parameters' or value.get('connection_checked') is not False
                or type(value.get('ai_profiles')) is not list or len(value['ai_profiles']) > 128
                or any(type(row) is not dict for row in value['ai_profiles'])):
            raise ManagementClientError('invalid_management_response', 'unavailable')
        try:
            if 'native_profile_capabilities' in value:
                _native_capabilities(value['native_profile_capabilities'])
            if 'default_state' in value:
                _fields(value, ('workspace_id', 'scope', 'connection_checked', 'ai_profiles', 'default_state'),
                        ('native_profile_capabilities',))
                _default(value['default_state'])
                for profile in value['ai_profiles']:
                    _profile(profile)
        except (ValueError, TypeError, UnicodeError):
            raise ManagementClientError('invalid_management_response', 'unavailable') from None
        return value

    def list_profiles(self):
        self._binding()
        return self._inventory(self._request('GET', '/api/management/ai-profiles'))

    def markdown_chats_list(self):
        self._binding()
        value = self._request('GET', MARKDOWN_CHATS)
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', 'unavailable')
        try:
            return _markdown_inventory(value, self.workspace_id)
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError(
                'invalid_markdown_response', 'unavailable'
            ) from None

    def markdown_chat_publish(self, operation):
        try:
            fields = validate_markdown_operation(operation)
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError(
                'invalid_markdown_arguments', 'not_sent'
            ) from None
        action = fields['action']
        body = None
        method = 'GET'
        if action == 'create_folder':
            method = 'POST'
            path = MARKDOWN_CHATS + '/folders'
            body = {'path': fields['path']}
        elif action == 'preview':
            method = 'POST'
            path = '/api/chats/' + fields['chat_id'] + '/markdown-preview'
            body = {key: fields[key] for key in ('source_revision', 'target_path')}
        elif action == 'preview_native':
            method = 'POST'
            path = MARKDOWN_CHATS + '/native-preview'
            body = {key: fields[key] for key in ('agent', 'chat_id', 'target_path')}
        elif action == 'preview_reference':
            method = 'POST'
            path = MARKDOWN_CHATS + '/history-reference-preview'
            body = {key: fields[key] for key in ('agent', 'session_id', 'target_path')}
        elif action == 'receipt':
            path = (
                MARKDOWN_CHATS
                + '/receipt?'
                + urlencode({'client_id': fields['client_id']})
            )
        elif action == 'sync_preview':
            method = 'POST'
            path = MARKDOWN_CHATS + '/updates/preview'
            body = {key: fields[key] for key in ('binding_id', 'expected_head_id')}
        elif action == 'sync_receipt':
            path = MARKDOWN_CHATS + '/updates/receipt?' + urlencode({'client_id': fields['client_id']})
        elif action in ('sync_read', 'sync_confirm'):
            path = MARKDOWN_CHATS + '/updates/' + fields['update_id']
            if action == 'sync_confirm':
                method = 'POST'
                path += '/confirm'
                body = {'client_id': fields['client_id']}
        else:
            path = MARKDOWN_CHATS + '/' + fields['publication_id']
            if action == 'confirm':
                method = 'POST'
                path += '/confirm'
                body = {'client_id': fields['client_id']}
        csrf = self._binding()
        value = self._request(
            method, path, body=body, csrf=csrf if method == 'POST' else None
        )
        outcome = 'unknown' if method == 'POST' else 'unavailable'
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', outcome)
        try:
            if action == 'create_folder':
                _fields(value, ('workspace_id', 'folder'))
                folder = value['folder']
                _fields(folder, ('path', 'created'))
                _markdown_folder_path(folder['path'], create=True)
                if folder['path'] != fields['path'] or type(folder['created']) is not bool:
                    raise ValueError('The Markdown folder acknowledgement changed.')
            elif action in ('sync_preview', 'sync_read'):
                _fields(value, ('workspace_id', 'update', 'preview'))
                update = _markdown_update(value['update'], self.workspace_id)
                _markdown_update_preview(value['preview'], update)
                if ((action == 'sync_preview' and (
                            update['binding_id'] != fields['binding_id']
                            or update['expected_head_id'] != fields['expected_head_id']))
                        or (action == 'sync_read' and update['id'] != fields['update_id'])):
                    raise ValueError('The reopened Markdown update identity changed.')
            elif action in ('sync_confirm', 'sync_receipt'):
                if value.get('received') is False and action == 'sync_receipt':
                    _fields(value, ('workspace_id', 'received'))
                else:
                    _fields(value, ('workspace_id', 'received', 'state', 'update'))
                    update = _markdown_update(value['update'], self.workspace_id)
                    if (value['received'] is not True or value['state'] != update['state']
                            or value['state'] == 'prepared'
                            or (action == 'sync_confirm' and update['id'] != fields['update_id'])):
                        raise ValueError('The accepted Markdown update receipt is not exact.')
            elif action in ('preview', 'preview_native', 'preview_reference', 'read'):
                _fields(value, ('workspace_id', 'publication', 'preview'))
                publication = _markdown_publication(
                    value['publication'], self.workspace_id
                )
                _markdown_preview(value['preview'], publication)
                if action in ('preview', 'preview_native', 'preview_reference'):
                    source = publication['source']
                    if (
                        publication['target_path'] != fields['target_path']
                        or (action != 'preview_reference' and source['chat_id'] != fields['chat_id'])
                        or (action == 'preview' and (source['kind'] != 'markdown_chat'
                            or source['revision'] != fields['source_revision']))
                        or (action == 'preview_native' and (source['kind'] != 'native_index'
                            or source['agent'] != fields['agent']))
                        or (action == 'preview_reference' and (source['kind'] != 'history_reference'
                            or source['agent'] != fields['agent']
                            or source['session_id'] != fields['session_id']))
                    ):
                        raise ValueError('Preview source or destination changed.')
                elif publication['id'] != fields['publication_id']:
                    raise ValueError('The reopened publication identity changed.')
            else:
                if value.get('received') is False and action == 'receipt':
                    _fields(value, ('workspace_id', 'received'))
                else:
                    _fields(value, ('workspace_id', 'received', 'state', 'publication'))
                    publication = _markdown_publication(
                        value['publication'], self.workspace_id
                    )
                    if (
                        value['received'] is not True
                        or value['state'] != publication['state']
                        or value['state'] == 'prepared'
                        or (
                            action == 'confirm'
                            and publication['id'] != fields['publication_id']
                        )
                    ):
                        raise ValueError(
                            'The accepted publication receipt is not exact.'
                        )
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_markdown_response', outcome) from None
        return (
            dict(value, ok=True, outcome='acknowledged', automatically_retried=False)
            if action in ('confirm', 'sync_confirm')
            else value
        )

    def list_native_model_connections(self):
        self._binding()
        value = self._request('GET', NATIVE_CONNECTIONS)
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', 'unavailable')
        try:
            _fields(value, ('workspace_id', 'connections'))
            rows, seen, counts = value['connections'], set(), dict.fromkeys(_NATIVE_LIMITS, 0)
            if type(rows) is not list or len(rows) > 4: raise ValueError('Invalid connection list.')
            for row in rows:
                _fields(row, ('agent', 'chat_id', 'title', 'epoch'))
                _native_identity(row['agent'], row['chat_id'], row['epoch']); _text(row['title'], 80)
                key = row['agent'], row['chat_id']; counts[row['agent']] += 1
                if (key in seen or row['title'] != row['title'].strip()
                        or counts[row['agent']] > _NATIVE_LIMITS[row['agent']]): raise ValueError('Invalid connections.')
                seen.add(key)
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_native_model_response', 'unavailable') from None
        return value

    def native_chat_configuration(self, operation, agent, chat_id, **selection):
        try:
            intent = validate_native_chat_configuration(dict(operation=operation,
                agent=agent, chat_id=chat_id, **selection))
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_native_chat_configuration_arguments', 'not_sent') from None
        mutation = operation == 'select'
        try:
            token = self._binding()
        except ManagementClientError as error:
            raise ManagementClientError(error.code, 'not_sent' if mutation else 'unavailable') from None
        body = {key: item for key, item in intent.items() if key != 'operation'} if mutation else None
        path = NATIVE_CHAT_CONFIGURATION if mutation else NATIVE_CHAT_CONFIGURATION + '?' + urlencode(
            {'agent': agent, 'chat_id': chat_id})
        value = self._request('POST' if mutation else 'GET', path, body=body,
            csrf=token if mutation else None)
        outcome = 'unknown' if mutation else 'unavailable'
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', outcome)
        try:
            _native_chat_configuration(value, self.workspace_id, agent, chat_id)
            if mutation:
                saved = value['selection']
                expected = intent['expected_revision']
                if (saved['revision'] not in ((expected, expected + 1) if expected else (1,))
                        or saved['ai_profile_id'] != intent['ai_profile_id']
                        or saved['ai_profile_revision'] != intent['ai_profile_revision']
                        or value['profile_current'] is not True):
                    raise ValueError('Invalid native chat selection acknowledgement.')
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_write_response' if mutation else
                'invalid_native_chat_configuration_response', outcome) from None
        # This exact POST acknowledgement records the accepted selection only.
        # A later GET is mutable current state, never a receipt for a lost POST.
        return dict(value, ok=True, outcome='acknowledged', automatically_retried=False) if mutation else value

    def native_chat_memory_configuration(self, operation, agent, chat_id, **selection):
        try:
            intent = validate_native_chat_memory_configuration(dict(operation=operation,
                agent=agent, chat_id=chat_id, **selection))
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_native_chat_memory_configuration_arguments', 'not_sent') from None
        mutation = operation == 'select'
        try:
            token = self._binding()
        except ManagementClientError as error:
            raise ManagementClientError(error.code, 'not_sent' if mutation else 'unavailable') from None
        body = {key: item for key, item in intent.items() if key != 'operation'} if mutation else None
        path = NATIVE_CHAT_MEMORY_CONFIGURATION if mutation else NATIVE_CHAT_MEMORY_CONFIGURATION + '?' + urlencode(
            {'agent': agent, 'chat_id': chat_id})
        value = self._request('POST' if mutation else 'GET', path, body=body,
            csrf=token if mutation else None)
        outcome = 'unknown' if mutation else 'unavailable'
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', outcome)
        try:
            _native_chat_memory_configuration(value, self.workspace_id, agent, chat_id)
            if mutation:
                saved, expected = value['selection'], intent['expected_revision']
                if (saved['revision'] not in ((expected, expected + 1) if expected else (1,))
                        or saved['memory_profile_id'] != intent['memory_profile_id']
                        or saved['memory_profile_revision'] != intent['memory_profile_revision']
                        or value['profile_current'] is not True):
                    raise ValueError('Invalid native chat memory selection acknowledgement.')
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_write_response' if mutation else
                'invalid_native_chat_memory_configuration_response', outcome) from None
        # Current GET state cannot serve as a receipt for a lost selection POST.
        return dict(value, ok=True, outcome='acknowledged', automatically_retried=False) if mutation else value

    def native_chat_names_list(self):
        self._binding()
        value = self._request('GET', NATIVE_CHAT_NAMES)
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', 'unavailable')
        try:
            _fields(value, ('workspace_id', 'scope', 'inference_performed', 'chats', 'enabled_agents'))
            _native_chat_scope(value)
            enabled, rows = value['enabled_agents'], value['chats']
            if (type(enabled) is not list or any(type(agent) is not str or agent not in NATIVE_CHAT_AGENTS for agent in enabled)
                    or enabled != [agent for agent in NATIVE_CHAT_AGENTS if agent in enabled]
                    or type(rows) is not list or len(rows) > 640):
                raise ValueError('Invalid native chat list.')
            seen, counts = set(), dict.fromkeys(NATIVE_CHAT_AGENTS, 0)
            for row in rows:
                _native_chat(row)
                key = row['agent'], row['id']; counts[row['agent']] += 1
                if row['agent'] not in enabled or key in seen or counts[row['agent']] > 128:
                    raise ValueError('Invalid native chat list.')
                seen.add(key)
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_native_chat_names_response', 'unavailable') from None
        return value

    def native_chat_rename(self, agent, chat_id, expected_title, title):
        try:
            body = validate_native_chat_rename(agent, chat_id, expected_title, title)
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_native_chat_name_arguments', 'not_sent') from None
        try:
            token = self._binding()
        except ManagementClientError as error:
            raise ManagementClientError(error.code, 'not_sent') from None
        value = self._request('POST', NATIVE_CHAT_NAMES, body=body, csrf=token)
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', 'unknown')
        try:
            _fields(value, ('workspace_id', 'scope', 'inference_performed', 'chat', 'previous_title', 'changed'))
            _native_chat_scope(value)
            row = _native_chat(value['chat'])
            if (row['agent'] != agent or row['id'] != chat_id or row['title'] != _native_chat_title(title)
                    or value['previous_title'] != expected_title or type(value['changed']) is not bool
                    or value['changed'] != (row['title'] != expected_title)):
                raise ValueError('Invalid native chat rename acknowledgement.')
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_write_response', 'unknown') from None
        # Only this exact HTTP ACK is evidence. Current names are mutable and are
        # not a receipt for a lost POST; no readback inference or automatic retry.
        return dict(value, ok=True, outcome='acknowledged', automatically_retried=False)

    def _native_history_request(self, agent, action, body):
        path = NATIVE_HISTORY_PATHS[(agent, action)]
        try:
            token = self._binding()
        except ManagementClientError as error:
            raise ManagementClientError(error.code, 'not_sent') from None
        try:
            value = self._request('POST', path, body=body, csrf=token)
        except ManagementClientError as error:
            if action == 'preview':
                raise ManagementClientError(error.code, 'unavailable') from None
            raise
        if type(value) is not dict:
            raise ManagementClientError('invalid_native_history_response',
                'unknown' if action == 'adopt' else 'unavailable')
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed',
                'unknown' if action == 'adopt' else 'unavailable')
        return value

    def native_history_preview(self, agent, session_id):
        try:
            body = {'session_id': validate_native_history_source(agent, session_id)}
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_codex_history_arguments' if agent == 'codex'
                else 'invalid_native_history_arguments', 'not_sent') from None
        value = self._native_history_request(agent, 'preview', body)
        try:
            _fields(value, ('agent', 'source_session_id', 'title', 'cwd', 'workspace_id', 'revision',
                'history_mode', 'execution_started', 'body_copied', 'current_executor_ownership'))
            _native_history_revision(value['revision'])
            cwd = value['cwd']
            if (type(cwd) is not str or not 1 <= len(cwd) <= 4096 or '\x00' in cwd
                    or not cwd.startswith('/') or posixpath.normpath(cwd) != cwd):
                raise ValueError('Invalid native history directory.')
            cwd.encode('utf-8')
            if (value['agent'] != agent or value['source_session_id'] != session_id
                    or _native_chat_title(value['title']) != value['title']
                    or value['history_mode'] != NATIVE_HISTORY_MODES[agent] or value['execution_started'] is not False
                    or value['body_copied'] is not False or value['current_executor_ownership'] != 'not_checked'):
                raise ValueError('Invalid native history preview.')
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_native_history_response', 'unavailable') from None
        return value

    def native_history_adopt(self, agent, session_id, revision):
        try:
            body = {'session_id': validate_native_history_source(agent, session_id),
                    'revision': _native_history_revision(revision)}
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_codex_history_arguments' if agent == 'codex'
                else 'invalid_native_history_arguments', 'not_sent') from None
        value = self._native_history_request(agent, 'adopt', body)
        try:
            _fields(value, ('chat', 'source_session_id', 'workspace_id', 'adopted', 'execution_started', 'body_copied'))
            row = _native_chat(value['chat'])
            if (row['agent'] != agent or value['source_session_id'] != session_id
                    or value['adopted'] is not True or value['execution_started'] is not False
                    or value['body_copied'] is not False):
                raise ValueError('Invalid native history registration acknowledgement.')
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_write_response', 'unknown') from None
        return dict(value, ok=True, outcome='acknowledged', automatically_retried=False)

    def codex_history_preview(self, session_id):
        return self.native_history_preview('codex', session_id)

    def codex_history_adopt(self, session_id, revision):
        return self.native_history_adopt('codex', session_id, revision)

    def refresh_native_model_catalog(self, agent, chat_id, epoch):
        try: fields = _native_identity(agent, chat_id, epoch)
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_native_model_arguments', 'not_sent') from None
        self._binding()
        value = self._request('GET', NATIVE_CATALOG + '?' + urlencode(fields))
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', 'unavailable')
        try:
            _fields(value, ('workspace_id', 'agent', 'chat_id', 'epoch', 'native_catalog_ready'))
            if value['native_catalog_ready'] is not True or any(value[key] != item for key, item in fields.items()):
                raise ValueError('Invalid native directory acknowledgement.')
        except (ValueError, TypeError, UnicodeError):
            raise ManagementClientError('invalid_native_model_response', 'unavailable') from None
        return value

    def refresh_specialist_model_catalog(self):
        self._binding()
        value = self._request('GET', SPECIALIST_MODEL_CATALOG)
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', 'unavailable')
        try:
            _fields(value, ('workspace_id', 'agent', 'source', 'native_catalog_ready'))
            if (value['agent'] != 'codex' or value['source'] != 'specialist'
                    or value['native_catalog_ready'] is not True):
                raise ValueError('Invalid specialist directory acknowledgement.')
        except (ValueError, TypeError, UnicodeError):
            raise ManagementClientError('invalid_native_model_response', 'unavailable') from None
        return value

    def prepare_native_model_catalog(self, agent, chat_id):
        try:
            body = validate_native_model_preparation(agent, chat_id)
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_native_model_arguments', 'not_sent') from None
        try:
            token = self._binding()
        except ManagementClientError as error:
            raise ManagementClientError(error.code, 'not_sent') from None
        value = self._request('POST', NATIVE_MODEL_PREPARATION, body=body, csrf=token)
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', 'unknown')
        try:
            _fields(value, ('workspace_id', 'agent', 'chat_id', 'epoch', 'native_catalog_ready'))
            identity = _native_identity(value['agent'], value['chat_id'], value['epoch'])
            if (value['native_catalog_ready'] is not True or identity['agent'] != agent
                    or identity['chat_id'] != chat_id):
                raise ValueError('Invalid native model preparation acknowledgement.')
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_write_response', 'unknown') from None
        return dict(value, ok=True, outcome='acknowledged', automatically_retried=False)

    def read_settings(self):
        self._binding()
        value = self._request('GET', SETTINGS)
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', 'unavailable')
        try:
            return _settings(value)
        except (ValueError, TypeError, UnicodeError):
            raise ManagementClientError('invalid_settings_response', 'unavailable') from None

    def update_settings(self, expected_revision, patch):
        try:
            body = validate_settings_update(expected_revision, patch)
        except (ValueError, TypeError, UnicodeError):
            raise ManagementClientError('invalid_settings_arguments', 'not_sent') from None
        try:
            token = self._binding()
        except ManagementClientError as error:
            raise ManagementClientError(error.code, 'not_sent') from None
        value = self._request('POST', SETTINGS, body=body, csrf=token)
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', 'unknown')
        try:
            _settings(value)
            if any(value['settings'][key] != item for key, item in body['patch'].items()):
                raise ValueError('Unexpected settings result.')
        except (ValueError, TypeError, UnicodeError):
            raise ManagementClientError('invalid_write_response', 'unknown') from None
        result = dict(value, ok=True, outcome='acknowledged', readback_state='unavailable', automatically_retried=False)
        try:
            current = self.read_settings()
            result.update(readback_state='confirmed' if current['revision'] == value['revision']
                and current['settings'] == value['settings'] else 'changed', current_settings=current)
        except ManagementClientError:
            pass  # Do not revoke the exact write ACK when later reads fail.
        return result

    def rename_profile(self, profile_id, revision, name):
        try:
            valid_id = type(profile_id) is str and str(uuid.UUID(profile_id)) == profile_id
        except ValueError:
            valid_id = False
        if (not valid_id or type(revision) is not int or not 1 <= revision <= 2**53-1
                or type(name) is not str or not 1 <= len(name) <= 40 or not name.strip()
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in name)):
            raise ManagementClientError('invalid_rename_arguments', 'not_sent')
        token = self._binding()
        value = self._request('PUT', '/api/management/ai-profiles/' + profile_id + '/name',
            body={'revision': revision, 'name': name}, csrf=token)
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', 'unknown')
        profile = value.get('profile')
        if (value.get('scope') != 'saved_parameters' or value.get('connection_checked') is not False
                or type(profile) is not dict or profile.get('id') != profile_id
                or profile.get('name') != name.strip() or type(profile.get('revision')) is not int):
            raise ManagementClientError('invalid_write_response', 'unknown')
        result = dict(value, ok=True, outcome='acknowledged', readback_state='unavailable')
        try:
            current = next((row for row in self.list_profiles()['ai_profiles'] if row.get('id') == profile_id), None)
            result.update(readback_state='confirmed' if current == profile else 'changed', current_profile=current)
        except ManagementClientError:
            pass  # An acknowledged write is not undone by a later read failure.
        return result

    def creation_receipt(self, creation_id):
        """Read an immutable create/copy receipt; never infer it from current data."""
        try: _uuid(creation_id)
        except (ValueError, TypeError, AttributeError):
            raise ManagementClientError('invalid_creation_identity', 'not_sent') from None
        self._binding()
        value = self._request('GET', '/api/management/ai-profile-creations/' + creation_id)
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', 'unavailable')
        try:
            _fields(value, ('workspace_id', 'scope', 'receipt'))
            if value['scope'] != 'saved_parameters':
                raise ValueError('Invalid scope.')
            receipt = value['receipt']
            if receipt is not None:
                _fields(receipt, ('creation_id', 'action', 'request_sha256', 'profile'))
                if (receipt['creation_id'] != creation_id or receipt['action'] not in ('create', 'copy')
                        or type(receipt['request_sha256']) is not str
                        or not re.fullmatch(r'[a-f0-9]{64}', receipt['request_sha256'])
                        or _profile(receipt['profile'])['revision'] != 1):
                    raise ValueError('Invalid receipt.')
        except (ValueError, TypeError, UnicodeError):
            raise ManagementClientError('invalid_creation_receipt', 'unavailable') from None
        return value

    def _acknowledged(self, operation, value, *, source='http_response'):
        try:
            action = operation['action']
            target = 'default_state' if action == 'set_default' else 'profile'
            _fields(value, ('workspace_id', 'scope', 'inference_performed', 'action', target))
            if (value['scope'] != 'saved_parameters' or value['inference_performed'] is not False
                    or value['action'] != action):
                raise ValueError('Invalid acknowledgement.')
            record = _default(value[target]) if target == 'default_state' else _profile(value[target])
            if action == 'set_default':
                valid = record['ai_profile_id'] == operation['profile_id'] and record['revision'] in (operation['default_revision'], operation['default_revision'] + 1)
            elif action in ('create', 'copy'):
                valid = record['revision'] == 1 and (action == 'create' or record['id'] != operation['profile_id'])
            else:
                valid = record['id'] == operation['profile_id'] and record['revision'] in (operation['revision'], operation['revision'] + 1)
            requested = operation.get('profile', operation.get('changes', {}))
            if not valid or any(record[key] != val for key, val in requested.items()):
                raise ValueError('Unexpected saved record.')
        except (ValueError, TypeError, UnicodeError):
            raise ManagementClientError('invalid_write_response', 'unknown') from None
        result = dict(value, ok=True, outcome='acknowledged', readback_state='unavailable',
                      acknowledgement_source=source, automatically_retried=False)
        try:
            inventory = self.list_profiles()
            if target == 'default_state':
                current = _default(inventory['default_state'])
                key = 'current_default_state'
            else:
                current = next((row for row in inventory['ai_profiles'] if row.get('id') == record['id']), None)
                if current is not None: current = _profile(current)
                key = 'current_profile'
            result.update(readback_state='confirmed' if current == record else 'changed', **{key: current})
        except (ManagementClientError, ValueError, TypeError, KeyError):
            pass  # A later read failure never revokes an acknowledged write.
        return result

    def manage_profile(self, operation):
        try: operation = validate_operation(operation)
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_operation_arguments', 'not_sent') from None
        try: token = self._binding()
        except ManagementClientError as error:
            raise ManagementClientError(error.code, 'not_sent') from None
        try:
            value = self._request('POST', OPERATIONS, body=operation, csrf=token)
            if value.get('workspace_id') != self.workspace_id:
                raise ManagementClientError('workspace_changed', 'unknown')
            return self._acknowledged(operation, value)
        except ManagementClientError as error:
            if error.outcome == 'unknown' and operation['action'] in ('create', 'copy'):
                try:
                    receipt = self.creation_receipt(operation['creation_id'])['receipt']
                    if receipt is not None and receipt['action'] == operation['action'] and receipt['request_sha256'] == _creation_sha(operation):
                        value = {'workspace_id': self.workspace_id, 'scope': 'saved_parameters',
                            'inference_performed': False, 'action': operation['action'], 'profile': receipt['profile']}
                        return self._acknowledged(operation, value, source='creation_receipt')
                except ManagementClientError:
                    pass
            raise

    def list_memory_profiles(self):
        self._binding(); value = self._request('GET', MEMORY_PROFILES)
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', 'unavailable')
        try:
            _fields(value, ('workspace_id', 'scope', 'connection_checked', 'inference_performed', 'memory_profiles'))
            _memory_scope(value); rows = value['memory_profiles']
            if type(rows) is not list or len(rows) > 128: raise ValueError('Invalid memory inventory.')
            seen = set()
            for row in rows:
                profile = _memory_profile(row)
                if profile['id'] in seen: raise ValueError('Duplicate memory identity.')
                seen.add(profile['id'])
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_memory_management_response', 'unavailable') from None
        return value

    def check_memory_connection(self, profile_id, revision):
        """Read local service metadata once; never provision or recall a Bank."""
        try: _uuid(profile_id); _revision(revision)
        except (ValueError, TypeError, AttributeError):
            raise ManagementClientError('invalid_memory_connection_arguments', 'not_sent') from None
        try: self._binding()
        except ManagementClientError as error:
            raise ManagementClientError(error.code, 'unavailable') from None
        value = self._request('GET', MEMORY_CONNECTIONS + profile_id + '?revision=' + str(revision))
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', 'unavailable')
        try:
            _fields(value, ('workspace_id', 'scope', 'inference_performed', 'profile', 'status',
                            'write_supported', 'bank_state', 'counts'))
            profile = _memory_profile(value['profile'])
            status, state, counts = value['status'], value['bank_state'], value['counts']
            if (value['scope'] != 'memory_connection_status' or value['inference_performed'] is not False
                    or profile['id'] != profile_id or profile['revision'] != revision
                    or type(value['write_supported']) is not bool or type(status) is not str
                    or status not in ('connected', 'missing', 'unavailable', 'identity_conflict', 'source_changed')):
                raise ValueError('Invalid memory connection status.')
            if status == 'connected':
                _fields(counts, ('facts', 'documents', 'pending_operations'))
                if (type(state) is not str or state not in ('empty', 'ready')
                        or any(type(count) is not int or not 0 <= count <= MAX_REVISION for count in counts.values())
                        or (state == 'empty') != (counts['facts'] == counts['documents'] == 0)):
                    raise ValueError('Invalid memory connection counts.')
            elif state is not None or counts is not None:
                raise ValueError('Unexpected memory connection counts.')
        except (ValueError, TypeError, UnicodeError):
            raise ManagementClientError('invalid_memory_connection_response', 'unavailable') from None
        return value

    def memory_creation_receipt(self, creation_id):
        try: _uuid(creation_id)
        except (ValueError, TypeError, AttributeError):
            raise ManagementClientError('invalid_creation_identity', 'not_sent') from None
        self._binding(); value = self._request('GET', MEMORY_CREATIONS + creation_id)
        if value.get('workspace_id') != self.workspace_id:
            raise ManagementClientError('workspace_changed', 'unavailable')
        try:
            _fields(value, ('workspace_id', 'scope', 'connection_checked', 'inference_performed', 'receipt'))
            _memory_scope(value); receipt = value['receipt']
            if receipt is not None:
                _fields(receipt, ('creation_id', 'action', 'request_sha256', 'profile'))
                profile = _memory_profile(receipt['profile'])
                if (receipt['creation_id'] != creation_id or receipt['action'] not in ('create', 'copy')
                        or type(receipt['request_sha256']) is not str or re.fullmatch('[a-f0-9]{64}', receipt['request_sha256']) is None
                        or profile['revision'] != 1 or profile['source_kind'] != 'owned' or profile['locked_mode'] is not False):
                    raise ValueError('Invalid memory receipt.')
        except (ValueError, TypeError, UnicodeError):
            raise ManagementClientError('invalid_memory_creation_receipt', 'unavailable') from None
        return value

    def _memory_acknowledged(self, operation, value, *, source='http_response'):
        try:
            _fields(value, ('workspace_id', 'scope', 'connection_checked', 'inference_performed', 'action', 'profile'))
            _memory_scope(value); action = operation['action']; record = _memory_profile(value['profile'])
            if value['action'] != action: raise ValueError('Unexpected memory action.')
            if action in ('create', 'copy'):
                valid = (record['revision'] == 1 and record['source_kind'] == 'owned' and record['locked_mode'] is False
                         and (action == 'create' or record['id'] != operation['profile_id']))
            else:
                valid = record['id'] == operation['profile_id'] and record['revision'] in (operation['revision'], operation['revision'] + 1)
            requested = operation.get('profile', operation.get('changes', {}))
            if not valid or any(record[key] != val for key, val in requested.items()):
                raise ValueError('Unexpected memory record.')
        except (ValueError, TypeError, UnicodeError):
            raise ManagementClientError('invalid_memory_write_response', 'unknown') from None
        result = dict(value, ok=True, outcome='acknowledged', readback_state='unavailable',
                      acknowledgement_source=source, automatically_retried=False)
        try:
            current = next((row for row in self.list_memory_profiles()['memory_profiles'] if row['id'] == record['id']), None)
            result.update(readback_state='confirmed' if current == record else 'changed', current_profile=current)
        except ManagementClientError: pass
        return result

    def manage_memory_profile(self, operation):
        try: operation = validate_memory_operation(operation)
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ManagementClientError('invalid_memory_operation_arguments', 'not_sent') from None
        try: token = self._binding()
        except ManagementClientError as error:
            raise ManagementClientError(error.code, 'not_sent') from None
        try:
            value = self._request('POST', MEMORY_OPERATIONS, body=operation, csrf=token)
            if value.get('workspace_id') != self.workspace_id:
                raise ManagementClientError('workspace_changed', 'unknown')
            return self._memory_acknowledged(operation, value)
        except ManagementClientError as error:
            if error.outcome == 'unknown' and operation['action'] in ('create', 'copy'):
                try:
                    receipt = self.memory_creation_receipt(operation['creation_id'])['receipt']
                    if receipt is not None and receipt['action'] == operation['action'] and receipt['request_sha256'] == _creation_sha(operation):
                        value = dict(workspace_id=self.workspace_id, scope='saved_memory_parameters',
                            connection_checked=False, inference_performed=False, action=operation['action'], profile=receipt['profile'])
                        return self._memory_acknowledged(operation, value, source='creation_receipt')
                except ManagementClientError: pass
            raise
