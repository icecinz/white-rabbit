"""Bounded, explicit MCP metadata projection; no tool calls or native writes.

The holder supplies its already-owned thread. This module reads a complete
catalog once, strips private descriptions/configuration, and caches nothing.
Connection metadata is not proof that any listed tool executed successfully.
"""
import re
import time

from .codex_rpc import RemoteError, TransportError


PAGE_SIZE = 50
MAX_PAGES = 8
MAX_SERVERS = 256
MAX_TOOLS = 4096
DEADLINE_SECONDS = 15
MAX_NAME_CHARS = 160
RUNTIME_STATUSES = frozenset(('notStarted', 'starting', 'connected',
    'authenticationRequired', 'failed', 'cancelled', 'disabled'))
AUTH_STATUSES = frozenset(('unknown', 'unsupported', 'notLoggedIn', 'bearerToken', 'oAuth'))
_NAME = re.compile(r'[A-Za-z0-9_.:/-]{1,160}\Z')
_EPOCH = re.compile(r'[0-9a-f]{32}\Z')


class SpecialistToolStatusError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in ('invalid_arguments', 'scope_changed',
            'catalog_invalid', 'catalog_capacity', 'catalog_incomplete',
            'deadline', 'unavailable', 'unsupported') else 'unavailable'
        super().__init__(self.code)


def validate_scope(thread_id, epoch):
    try:
        if (type(thread_id) is not str or not 1 <= len(thread_id) <= 256
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in thread_id)
                or type(epoch) is not str or _EPOCH.fullmatch(epoch) is None):
            raise SpecialistToolStatusError('invalid_arguments')
        thread_id.encode('utf-8', 'strict')
    except UnicodeError:
        raise SpecialistToolStatusError('invalid_arguments') from None


def _name(value):
    if type(value) is not str or _NAME.fullmatch(value) is None:
        raise SpecialistToolStatusError('catalog_invalid')
    return value


def _status(value, known):
    if value is None:
        return None
    if (type(value) is not str or not 1 <= len(value) <= 80
            or re.fullmatch(r'[A-Za-z0-9_-]+', value) is None):
        raise SpecialistToolStatusError('catalog_invalid')
    return value if value in known else None


def _server(row):
    if type(row) is not dict or type(row.get('tools')) is not dict:
        raise SpecialistToolStatusError('catalog_invalid')
    name = _name(row.get('name'))
    tools = row['tools']
    if len(tools) > MAX_TOOLS:
        raise SpecialistToolStatusError('catalog_capacity')
    names = []
    for key, value in tools.items():
        key = _name(key)
        if type(value) is not dict or type(value.get('name')) is not str or value['name'] != key:
            raise SpecialistToolStatusError('catalog_invalid')
        names.append(key)
    return {'name': name, 'runtime_status': _status(row.get('runtimeStatus'), RUNTIME_STATUSES),
        'auth_status': _status(row.get('authStatus'), AUTH_STATUSES),
        'tools_error_present': row.get('toolsError') is not None,
        'tool_names': sorted(names)}


def _cursor(value):
    try:
        if (type(value) is not str or not 1 <= len(value) <= 4096
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
            raise SpecialistToolStatusError('catalog_invalid')
        value.encode('utf-8', 'strict')
    except UnicodeError:
        raise SpecialistToolStatusError('catalog_invalid') from None
    return value


def read_tool_status(request, *, thread_id, epoch):
    """Read only MCP server/tool names and status for one loaded native thread."""
    validate_scope(thread_id, epoch)
    if not callable(request):
        raise SpecialistToolStatusError('invalid_arguments')
    deadline = time.monotonic() + DEADLINE_SECONDS
    rows, names, cursor, cursors, tools_count = [], set(), None, set(), 0
    for _ in range(MAX_PAGES):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SpecialistToolStatusError('deadline')
        params = {'threadId': thread_id, 'detail': 'toolsAndAuthOnly', 'limit': PAGE_SIZE}
        if cursor is not None:
            params['cursor'] = cursor
        try:
            page = request('mcpServerStatus/list', params, timeout=remaining)
        except RemoteError as error:
            raise SpecialistToolStatusError('unsupported' if error.code == -32601 else 'unavailable') from None
        except (TransportError, TimeoutError):
            raise SpecialistToolStatusError('unavailable') from None
        except Exception:
            raise SpecialistToolStatusError('unavailable') from None
        if time.monotonic() >= deadline:
            raise SpecialistToolStatusError('deadline')
        if (type(page) is not dict or type(page.get('data')) is not list
                or len(page['data']) > PAGE_SIZE or 'nextCursor' not in page):
            raise SpecialistToolStatusError('catalog_invalid')
        for row in page['data']:
            projected = _server(row)
            if projected['name'] in names:
                raise SpecialistToolStatusError('catalog_invalid')
            names.add(projected['name'])
            rows.append(projected)
            tools_count += len(projected['tool_names'])
            if len(rows) > MAX_SERVERS or tools_count > MAX_TOOLS:
                raise SpecialistToolStatusError('catalog_capacity')
        cursor = page['nextCursor']
        if time.monotonic() >= deadline:
            raise SpecialistToolStatusError('deadline')
        if cursor is None:
            return {'schema_version': 1, 'session_key': 'specialist/codex',
                'thread_id': thread_id, 'epoch': epoch,
                'servers': sorted(rows, key=lambda value: value['name']), 'complete': True}
        cursor = _cursor(cursor)
        if not page['data'] or cursor in cursors:
            raise SpecialistToolStatusError('catalog_invalid')
        cursors.add(cursor)
    raise SpecialistToolStatusError('catalog_incomplete')
