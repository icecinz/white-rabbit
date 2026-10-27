"""Bounded results from original receipts and verified native results.

No owner creation, open/resume, prompt, approval answer, filesystem discovery or
new ledger. Claude may read its original ledger's frozen successful SDK result.
Native messages are untrusted Agent data, never executable output.
An existing owner's normal view may refresh/flush native state; this is marked.
"""
from copy import deepcopy
import json
import re
import uuid
from urllib.parse import urlencode

from native_http_common import SpecialistHTTPError


MAX_RESULT_BYTES = 128 * 1024
MAX_PAGES = 8
_AGENTS = {
    'codex': ('native_chats', 'native_chat_request', '/api/native-chats'),
    'pi': ('pi_native_chats', 'pi_native_request', '/api/pi-chats'),
    'hermes': ('hermes_native_chats', 'hermes_native_request', '/api/hermes-chats'),
    'claude': ('claude_native_chats', 'claude_native_request', '/api/claude-chats'),
    'dsh': ('dsh_native_chats', 'dsh_native_request', '/api/dsh-chats'),
}
_STATES = {'submitted_unknown': 'unknown', 'accepted': 'unknown',
           'completed': 'completed', 'failed': 'failed', 'interrupted': 'interrupted',
           'rejected': 'rejected', 'command_completed': 'unknown'}
_REFERENCE_KEYS = frozenset(('agent', 'thread_id', 'turn_id', 'session_id', 'native_user_uuid',
    'native_result_uuid', 'native_message_id', 'native_turn', 'native_turn_start_seq',
    'native_message_seq', 'native_end_seq', 'durable_seq', 'evidence_kind', 'reason'))
_RECEIPT_KEYS = frozenset(('client_id', 'chat_id', 'session_key', 'kind', 'request_sha256', 'status',
    'thread_id', 'session_id', 'turn_id', 'target_turn_id', 'rpc_error_code', 'created_at', 'updated_at',
    'epoch', 'message_sha256', 'native_user_uuid', 'native_result_uuid', 'native_message_id',
    'native_insert_seq', 'native_turn', 'native_turn_start_seq', 'native_message_seq', 'native_end_seq',
    'native_reason', 'durable_seq', 'native_command_sha256', 'native_command_kind',
    'native_error_code', 'native_error_sha256'))


def _error(code='unavailable', status=503):
    return SpecialistHTTPError(status, 'native_task_result_' + code,
        '原生任务结果暂不能完整核对；原请求保留，没有重新发送或另建连接。')


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise _error('invalid', 400) from None
    return value


def _id(value):
    try:
        return (type(value) is str and 0 < len(value) <= 256 and not value.startswith('/')
                and not re.match(r'^[A-Za-z]:[\\/]', value) and bool(value.encode('utf-8'))
                and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value))
    except UnicodeError:
        return False


def _integer(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def _references(value):
    result = {}
    for key, item in value.items():
        if key not in _REFERENCE_KEYS or type(item) not in (str, int, bool, type(None)):
            continue
        if type(item) is str and (len(item) > 256 or item.startswith('/')
                or re.match(r'^[A-Za-z]:[\\/]', item) or any(ord(c) < 32 or ord(c) == 127 for c in item)):
            continue
        if type(item) is int and not _integer(item):
            continue
        result[key] = item
    return result


def _result(reason, *, state='unavailable', references=None):
    return {'state': state, 'messages': [], 'references': _references(dict(references or {}, reason=reason))}


def _complete(messages, references):
    try:
        if not messages or any(not _id(row['id']) or type(row['text']) is not str for row in messages):
            return _result('final_text_unavailable', references=references)
        size = sum(len(row['text'].encode('utf-8')) for row in messages)
        if not size:
            return _result('final_text_unavailable', references=references)
        if size > MAX_RESULT_BYTES:
            return _result('result_capacity', state='incomplete', references=references)
    except (KeyError, UnicodeError, TypeError):
        return _result('final_text_unavailable', references=references)
    return {'state': 'available', 'messages': deepcopy(messages),
            'references': _references(dict(references, reason='verified_native_result'))}


def _receipt(service, agent, chat, client):
    manager_name, method_name, root = _AGENTS[agent]
    manager, method = getattr(service, manager_name, None), getattr(service, method_name, None)
    if manager is None or not callable(method):
        raise _error()
    # These exact public receipt routes validate catalogue ownership without
    # constructing a lazy native owner or asking any native process to start.
    value = method('GET', root + '/' + chat + '/receipt', query=urlencode({'client_id': client}))
    if (type(value) is not dict or value.get('workspace_id') != service.workspace_id
            or value.get('chat_id') != chat):
        raise _error('scope', 409)
    key = 'receipt' if agent in ('codex', 'pi', 'hermes') else 'operation'
    if key not in value:
        raise _error()
    operation = value[key]
    if operation is not None:
        if (type(operation) is not dict or operation.get('client_id') != client
                or type(operation.get('status')) is not str or operation['status'] not in _STATES
                or type(operation.get('request_sha256')) is not str
                or re.fullmatch(r'[0-9a-f]{64}', operation['request_sha256']) is None):
            raise _error('scope', 409)
        if agent == 'codex':
            if operation.get('session_key') != 'chat/codex/' + chat or operation.get('kind') != 'turn_start':
                raise _error('scope', 409)
        elif operation.get('chat_id') != chat:
            raise _error('scope', 409)
        if agent in ('claude', 'dsh') and operation.get('kind') != 'prompt':
            raise _error('scope', 409)
        if operation.get('native_command_kind') not in (None, 'success'):
            raise _error('scope', 409)
        try:
            if (not operation.keys() <= _RECEIPT_KEYS or any(type(k) is not str or type(v) not in (str, int, bool, type(None))
                    or type(v) is int and abs(v) > 2**53 - 1
                    or type(v) is str and (len(v) > 512 or v.startswith('/') or re.match(r'^[A-Za-z]:[\\/]', v))
                    for k, v in operation.items())
                    or len(json.dumps(operation, ensure_ascii=False, allow_nan=False).encode()) > 32 * 1024):
                raise ValueError
        except (ValueError, UnicodeError, TypeError):
            raise _error() from None
    return manager, deepcopy(operation)


def _attention(view, agent, operation, holder):
    if operation['status'] != 'accepted':
        return False
    if (agent == 'codex' and getattr(holder, '_turn_client_id', None) != operation['client_id']
            or agent != 'codex' and getattr(holder, '_active', None) != operation['client_id']):
        return False
    requests = view.get('extension_requests' if agent == 'pi' else 'pending' if agent == 'dsh' else 'requests')
    if requests is None:
        return True
    if type(requests) is not list:
        return True
    return any(type(row) is not dict or row.get('state') in ('pending', 'unknown', 'request_snapshot_changed',
        'awaiting_event_registration') for row in requests)


def _hermes(holder, operation):
    refs = {'agent': 'hermes', 'evidence_kind': 'current_gateway_result'}
    reader = getattr(holder, 'read_task_result', None)
    if not callable(reader):
        return _result('current_result_unavailable', references=refs)
    value = reader(operation['client_id'])
    if type(value) is not dict or set(value) != {'state', 'reason', 'answer', 'source'}:
        return _result('current_result_unavailable', references=refs)
    if value['state'] == 'incomplete':
        return _result('result_capacity', state='incomplete', references=refs)
    answer, source = value['answer'], value['source']
    if (value['state'] != 'available' or value['reason'] != 'current_connection_result'
            or type(answer) is not dict or set(answer) != {'text', 'sha256', 'bytes'}
            or type(source) is not dict or source.get('kind') != 'hermes_gateway_completion'
            or source.get('submission_id') != operation['client_id']
            or source.get('request_sha256') != operation['request_sha256']
            or source.get('terminal_status') != 'completed' or not _id(source.get('runtime_sid'))):
        return _result('current_result_unavailable', references=refs)
    refs['session_id'] = source['runtime_sid']
    return _complete([{'id': 'hermes-cache:' + operation['client_id'], 'text': answer['text'], 'phase': None}], refs)


def _pi(holder, operation):
    refs = {'agent': 'pi', 'evidence_kind': 'current_prompt_result'}
    reader = getattr(holder, 'read_task_result', None)
    if not callable(reader):
        return _result('current_result_unavailable', references=refs)
    value = reader(operation['client_id'])
    if type(value) is not dict or set(value) != {'state', 'reason', 'answer', 'source'}:
        return _result('current_result_unavailable', references=refs)
    if value['state'] == 'incomplete':
        return _result('result_capacity', state='incomplete', references=refs)
    answer, source = value['answer'], value['source']
    if (value['state'] != 'available' or value['reason'] != 'current_connection_result'
            or type(answer) is not dict or set(answer) != {'text', 'sha256', 'bytes'}
            or type(source) is not dict or source.get('kind') != 'pi_prompt_completion'
            or source.get('submission_id') != operation['client_id']
            or source.get('request_sha256') != operation['request_sha256']
            or source.get('terminal_status') != 'completed' or not _id(source.get('runtime_sid'))):
        return _result('current_result_unavailable', references=refs)
    refs['session_id'] = source['runtime_sid']
    return _complete([{'id': 'pi-cache:' + operation['client_id'], 'text': answer['text'], 'phase': None}], refs)


def _codex(holder, operation, first):
    thread, turn = operation.get('thread_id'), operation.get('turn_id')
    refs = {'thread_id': thread, 'turn_id': turn, 'evidence_kind': 'native_turn'}
    binding = first.get('binding', {})
    native = first.get('native', {}).get('thread', {}) if type(first.get('native')) is dict else {}
    if (not _id(thread) or not _id(turn) or binding.get('thread_id') != thread
            or native.get('id') != thread or native.get('ephemeral') is not False):
        return _result('native_turn_unverified', references=refs)
    turns = first.get('turns_page')
    matches = [row for row in turns.get('data', []) if type(row) is dict and row.get('id') == turn] if type(turns) is dict else []
    if len(matches) != 1 or matches[0].get('status') != 'completed':
        return _result('native_terminal_unavailable', references=refs)
    cursor, seen, ids, messages = None, set(), set(), []
    epoch = first.get('connection', {}).get('epoch')
    if type(epoch) is not str or re.fullmatch(r'[0-9a-f]{32}', epoch) is None:
        return _result('owner_changed', references=refs)
    for _ in range(MAX_PAGES):
        view = holder.view(turn_id=turn, cursor=cursor)
        if (view.get('connection', {}).get('state') != 'ready'
                or view.get('connection', {}).get('epoch') != epoch
                or view.get('binding', {}).get('thread_id') != thread):
            return _result('owner_changed', references=refs)
        page = view.get('history_page')
        if type(page) is not dict or type(page.get('data')) is not list or 'nextCursor' not in page:
            return _result('native_page_unavailable', references=refs)
        for row in page['data']:
            item = row.get('item') if type(row) is dict else None
            if (type(item) is not dict or row.get('turnId') != turn or not _id(item.get('id'))
                    or item['id'] in ids):
                return _result('native_page_unverified', references=refs)
            ids.add(item['id'])
            if item.get('type') == 'agentMessage' and item.get('phase') == 'final_answer':
                if item.get('display_truncated') is True or type(item.get('text')) is not str:
                    return _result('native_output_incomplete', state='incomplete', references=refs)
                messages.append({'id': item['id'], 'text': item['text'], 'phase': item['phase']})
                if sum(len(message['text'].encode('utf-8')) for message in messages) > MAX_RESULT_BYTES:
                    return _result('result_capacity', state='incomplete', references=refs)
        cursor = page['nextCursor']
        if cursor is None:
            return _complete(messages, refs)
        try: marker = json.dumps(cursor, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError): return _result('native_page_unverified', references=refs)
        if len(marker) > 4096 or marker in seen:
            return _result('native_page_unverified', references=refs)
        seen.add(marker)
    return _result('native_page_capacity', state='incomplete', references=refs)


def _claude_record(operation, record, *, evidence_kind):
    sid, result_id = operation.get('session_id'), operation.get('native_result_uuid')
    refs = {'session_id': sid, 'native_user_uuid': operation.get('native_user_uuid'),
            'native_result_uuid': result_id, 'evidence_kind': evidence_kind}
    if (not _id(sid) or not _id(result_id) or operation.get('native_user_uuid') != operation['client_id']
            or type(operation.get('epoch')) is not str or re.fullmatch(r'[0-9a-f]{32}', operation['epoch']) is None
            or type(record) is not dict or record.get('sdk_type') != 'ResultMessage'
            or type(record.get('data')) is not dict or record['data'].get('uuid') != result_id):
        return _result('result_identity_unverified', references=refs)
    data = record['data']
    if record.get('display_truncated') is True or data.get('display_truncated') is True:
        return _result('native_output_incomplete', state='incomplete', references=refs)
    origin = data.get('origin')
    if (data.get('session_id') != sid or data.get('parent_tool_use_id') is not None
            or origin is not None and (type(origin) is not dict or origin.get('kind') != 'human')
            or data.get('is_error') is not False or data.get('subtype') != 'success'
            or data.get('terminal_reason') not in (None, 'completed') or type(data.get('result')) is not str):
        return _result('terminal_result_unverified', references=refs)
    return _complete([{'id': result_id, 'text': data['result'], 'phase': None}], refs)


def _claude(operation, view):
    refs = {'session_id': operation.get('session_id'), 'native_user_uuid': operation.get('native_user_uuid'),
            'native_result_uuid': operation.get('native_result_uuid'), 'evidence_kind': 'current_sdk_result'}
    if (view.get('binding', {}).get('session_id') != operation.get('session_id')
            or view.get('binding', {}).get('persisted') is not True
            or view.get('epoch') != operation.get('epoch')):
        return _result('result_identity_unverified', references=refs)
    matches = [row for row in view.get('native_records', []) if type(row) is dict
        and row.get('sdk_type') == 'ResultMessage' and type(row.get('data')) is dict
        and row['data'].get('uuid') == operation.get('native_result_uuid')]
    if len(matches) != 1:
        return _result('result_not_in_current_cache', references=refs)
    return _claude_record(operation, matches[0], evidence_kind='current_sdk_result')


def _saved_claude(manager, operation, chat, client):
    """Historical success in this ledger; never a current-owner observation."""
    reader = getattr(getattr(manager, 'store', None), 'completed_result', None)
    if not callable(reader) or operation['status'] != 'completed':
        return None
    saved = reader(chat, client)
    if saved is None:
        return None
    if (type(saved) is not dict or set(saved) != {'receipt', 'record'}
            or saved['receipt'] != operation):
        raise _error('scope', 409)
    return _claude_record(operation, saved['record'], evidence_kind='stored_sdk_result')


def _dsh(operation, view):
    keys = ('native_turn', 'native_turn_start_seq', 'native_message_seq', 'native_end_seq', 'durable_seq')
    refs = {key: operation.get(key) for key in ('session_id', 'native_message_id', *keys)}
    refs['evidence_kind'] = 'durable_native_turn'
    if (view.get('binding', {}).get('sessionId') != operation.get('session_id')
            or view.get('binding', {}).get('persisted') is not True
            or not _id(operation.get('native_message_id')) or any(not _integer(operation.get(key)) for key in keys)
            or operation.get('native_reason') != 'completed' or not 0 < operation['native_turn']
            or not operation['native_turn_start_seq'] < operation['native_message_seq'] < operation['native_end_seq'] <= operation['durable_seq']):
        return _result('durable_turn_unverified', references=refs)
    history = view.get('history')
    rows = history.get('events') if type(history) is dict else None
    if type(rows) is not list or len(rows) > 2000:
        return _result('native_history_unavailable', references=refs)
    events, previous = {}, -1
    for row in rows:
        event = row.get('event') if type(row) is dict else None
        if (type(event) is not dict or not _integer(event.get('seq')) or event['seq'] <= previous
                or type(event.get('data')) is not dict):
            return _result('native_history_unverified', references=refs)
        previous = event['seq']; events[previous] = event
    start, user, end = (events.get(operation[key]) for key in keys[1:4])
    if (not start or not user or not end or start.get('type') != 'turn/start'
            or start['data'].get('turn') != operation['native_turn'] or user.get('type') != 'user/message'
            or user['data'].get('id') != operation['native_message_id']
            or user['data'].get('source', {}).get('rpcId') != operation['client_id']
            or end.get('type') != 'turn/end' or end['data'].get('turn') != operation['native_turn']
            or end['data'].get('reason', {}).get('kind') != 'completed'):
        return _result('native_turn_window_incomplete', state='incomplete', references=refs)
    assistants = [event for seq, event in events.items()
        if operation['native_message_seq'] < seq < operation['native_end_seq']
        and event.get('type') == 'assistant/message' and event['data'].get('turn') == operation['native_turn']]
    message = assistants[-1]['data'].get('message') if assistants else None
    if (type(message) is not dict or message.get('role') != 'assistant'
            or message.get('tool_calls') or message.get('toolCalls') or type(message.get('content')) is not list
            or any(type(part) is not dict or part.get('type') not in ('text', 'reasoning')
                   or part.get('type') == 'text' and type(part.get('text')) is not str
                   for part in message['content'])):
        return _result('final_text_unavailable', references=refs)
    return _complete([{'id': message.get('id'), 'text': ''.join(part['text'] for part in message['content']
        if part['type'] == 'text'), 'phase': None}], refs)


def read(service, agent, chat_id, client_id, *, refresh_native=False):
    """Read an exact client; a missing receipt is never an unsent assertion."""
    if type(agent) is not str or agent not in _AGENTS or type(refresh_native) is not bool:
        raise _error('invalid', 400)
    chat, client = _uuid(chat_id), _uuid(client_id)
    manager, operation = _receipt(service, agent, chat, client)
    value = {'receipt': operation, 'execution_state': _STATES.get(operation['status']) if operation else 'unknown',
             'needs_user': operation is None or operation.get('status') == 'submitted_unknown',
             'result': _result('receipt_absent' if operation is None else 'not_completed'),
             'native_refresh_performed': False}
    if operation is None:
        return value
    if not refresh_native:
        value['result'] = _result('native_refresh_not_requested')
        return value
    if agent == 'claude':
        try:
            saved = _saved_claude(manager, operation, chat, client)
        except SpecialistHTTPError:
            raise
        except Exception:
            value['result'] = _result('saved_result_unavailable'); value['needs_user'] = True
            return value
        if saved is not None:
            value['result'] = saved
            value['needs_user'] = saved['state'] != 'available'
            return value
    with manager._lock:
        owner = manager._owners.get(chat)
    if owner is None:
        value['result'] = _result('existing_owner_unavailable')
        value['needs_user'] = True
        return value
    lock = owner.lock if agent == 'codex' else owner._action
    holder = owner.holder if agent == 'codex' else owner
    with lock:
        with manager._lock:
            if manager._owners.get(chat) is not owner:
                value['result'] = _result('owner_changed'); value['needs_user'] = True
                return value
        try:
            value['native_refresh_performed'] = True
            view = holder.view() if agent == 'codex' else holder.view(client_id=client)
            previous = operation
            _manager, operation = _receipt(service, agent, chat, client)
            if operation is not None and any(operation.get(key) != previous.get(key)
                    for key in ('request_sha256', 'epoch', 'session_key', 'chat_id')):
                raise _error('scope', 409)
            value['receipt'] = operation
            value['execution_state'] = _STATES.get(operation['status']) if operation else 'unknown'
            if operation is None:
                value['result'] = _result('receipt_absent'); value['needs_user'] = True
                return value
            if (type(view) is not dict or agent != 'codex' and (view.get('workspace_id') != service.workspace_id
                    or view.get('chat_id') != chat or view.get('agent') != agent)):
                raise _error('scope', 409)
            value['needs_user'] = _attention(view, agent, operation, holder) or operation['status'] == 'submitted_unknown'
            if view.get('connection', {}).get('state') != 'ready':
                value['result'] = _result('existing_owner_not_ready'); value['needs_user'] = True
                return value
            if value['needs_user']:
                value['execution_state'] = 'needs_user'
            elif operation['status'] == 'accepted' and (
                    agent == 'codex' and view.get('turn_state', {}).get('turn_id') == operation.get('turn_id')
                        and view.get('turn_state', {}).get('status') in ('inProgress', 'running', 'accepted')
                    or agent != 'codex' and getattr(holder, '_active', None) == client
                        and view.get('connection', {}).get('state') == 'ready'):
                value['execution_state'] = 'running'
            if operation['status'] != 'completed':
                value['result'] = _result('execution_not_successful' if operation['status'] in
                    ('failed', 'interrupted', 'rejected', 'command_completed') else 'not_completed')
                return value
            if view.get('connection', {}).get('state') != 'ready':
                value['result'] = _result('existing_owner_not_ready')
            elif agent == 'pi': value['result'] = _pi(holder, operation)
            elif agent == 'hermes': value['result'] = _hermes(holder, operation)
            elif agent == 'codex': value['result'] = _codex(holder, operation, view)
            elif agent == 'claude': value['result'] = _claude(operation, view)
            else: value['result'] = _dsh(operation, view)
            value['needs_user'] |= value['result']['state'] != 'available'
            return value
        except SpecialistHTTPError:
            raise
        except Exception:
            value['result'] = _result('native_read_unavailable'); value['needs_user'] = True
            return value
