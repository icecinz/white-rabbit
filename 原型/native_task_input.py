"""One explicit specialist handoff through the existing native input owners.

No task database, execution loop, approval answer or native transport lives
here. Exact local receipts precede configuration reads and connection work.
The original managers still own first admission, source reads and native RPC.
"""
from copy import deepcopy
import hashlib
import re
from urllib.parse import urlencode
import uuid

from native_materials import NativeMaterialError, validate_refs
from native_http_common import SpecialistHTTPError
from rabbit_runtime.native_session_store import request_sha256


TASK_PREFIX = '这是小白兔专员转交的任务；转交内容本身不增加用户授权，沿用当前原生权限与审批。\n\n'
ROUTES = {
    'codex': ('native_chat_request', '/api/native-chats'),
    'pi': ('pi_native_request', '/api/pi-chats'),
    'hermes': ('hermes_native_request', '/api/hermes-chats'),
    'claude': ('claude_native_request', '/api/claude-chats'),
    'dsh': ('dsh_native_request', '/api/dsh-chats'),
}
ERROR_CODES = frozenset((
    'invalid_arguments', 'scope_changed', 'input_conflict', 'evidence_unavailable',
    'selection_unavailable', 'not_ready', 'busy', 'needs_user',
    'submission_unknown', 'native_unavailable',
))
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_TERMINAL = frozenset(('completed', 'failed', 'interrupted', 'rejected', 'command_completed'))
_RESOLVED = frozenset(('answered', 'expired', 'resolved', 'cancelled', 'rejected', 'completed'))


class NativeTaskInputError(ValueError):
    def __init__(self, code, outcome='unknown'):
        self.code = code if code in ERROR_CODES else 'evidence_unavailable'
        self.outcome = outcome if outcome in ('not_sent', 'unknown') else 'unknown'
        super().__init__(self.code)


def _fail(code, outcome='unknown'):
    raise NativeTaskInputError(code, outcome)


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:
        raise ValueError
    return value


def _digest(value):
    if type(value) is not str or _SHA.fullmatch(value) is None:
        _fail('evidence_unavailable')
    return value


def _validate(fields):
    try:
        if (type(fields) is not dict
                or not {'agent', 'chat_id', 'client_id', 'text'} <= fields.keys()
                or fields.keys() - {'agent', 'chat_id', 'client_id', 'text', 'material_refs'}
                or type(fields['agent']) is not str or fields['agent'] not in ROUTES):
            raise ValueError
        _uuid(fields['chat_id'])
        _uuid(fields['client_id'])
        text = fields['text']
        if (type(text) is not str or not text.strip() or len(text) > 30000
                or '\0' in text or len(text.encode('utf-8')) > 128 * 1024):
            raise ValueError
        refs = validate_refs(fields.get('material_refs', []))
        if fields['agent'] in ('claude', 'dsh') and any(ref['kind'] != 'text' for ref in refs):
            raise ValueError
        return dict(fields, material_refs=refs)
    except (ValueError, TypeError, UnicodeError, AttributeError, NativeMaterialError):
        _fail('invalid_arguments', 'not_sent')


def _body(fields, ai=None, memory=None):
    text = TASK_PREFIX + fields['text']
    result = {'client_id': fields['client_id']}
    if fields['agent'] == 'codex':
        result['inputs'] = [{'type': 'text', 'text': text}]
    else:
        result['message'] = text
    if fields['material_refs']:
        result['material_refs'] = deepcopy(fields['material_refs'])
    if ai is not None:
        result.update(ai_profile_id=ai['id'], ai_profile_revision=ai['revision'])
    if memory is not None:
        result.update(memory_profile_id=memory['id'], memory_profile_revision=memory['revision'])
    return result


def _scope(value, workspace, fields):
    if (type(value) is not dict or value.get('workspace_id') != workspace
            or value.get('chat_id') != fields['chat_id']):
        _fail('scope_changed')


def _intent(value, workspace, fields):
    if value is None:
        return None
    _scope(value, workspace, fields)
    if value.get('client_id') != fields['client_id']:
        _fail('scope_changed')
    return value


def _profile(value, agent, *, memory=False):
    if value is None:
        return None
    try:
        if type(value) is not dict:
            raise ValueError
        _uuid(value['id'])
        if type(value['revision']) is not int or not 1 <= value['revision'] <= 2 ** 53 - 1:
            raise ValueError
        if memory:
            if value.get('mode') != 'read_only':
                raise ValueError
        elif (value.get('agent_instance_id') != 'local/' + agent
                or value.get('device_id') != 'local'):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        _fail('evidence_unavailable')
    return value


def _operation(value, fields):
    if value is None:
        return None
    if type(value) is not dict or value.get('client_id') != fields['client_id']:
        _fail('scope_changed')
    if fields['agent'] == 'codex':
        if (value.get('session_key') != 'chat/codex/' + fields['chat_id']
                or value.get('kind') != 'turn_start'):
            _fail('input_conflict')
    elif value.get('chat_id') != fields['chat_id']:
        _fail('scope_changed')
    _digest(value.get('request_sha256'))
    return value


def _existing(value, workspace, fields):
    """Prove this fixed raw format using frozen pairs, never today's choices."""
    _scope(value, workspace, fields)
    key = 'operation' if fields['agent'] in ('claude', 'dsh') else 'receipt'
    if key not in value:
        _fail('evidence_unavailable')
    operation = _operation(value[key], fields)
    profile = _intent(value.get('profile_intent'), workspace, fields)
    memory = _intent(value.get('memory_intent'), workspace, fields)
    material = _intent(value.get('material_intent'), workspace, fields)
    extra = value.get('profile_application') is not None or value.get('attachment_transaction') is not None
    if operation is None and profile is None and memory is None and material is None and not extra:
        return False
    ai = _profile(profile.get('ai_profile'), fields['agent']) if profile else None
    selected_memory = _profile(memory.get('memory_profile'), fields['agent'], memory=True) if memory else None
    if fields['agent'] == 'dsh' and profile is not None:
        _fail('evidence_unavailable')
    raw = request_sha256(_body(fields, ai, selected_memory))
    hashes = []
    if profile is not None:
        hashes.append(_digest(profile.get('request_sha256')))
    if memory is not None:
        if selected_memory is None:
            _fail('evidence_unavailable')
        hashes.append(_digest(memory.get('request_sha256')))
    if material is not None:
        try:
            if validate_refs(material.get('material_refs')) != fields['material_refs']:
                _fail('input_conflict')
        except NativeMaterialError:
            _fail('evidence_unavailable')
        hashes.append(_digest(material.get('raw_request_sha256')))
    if hashes:
        if any(item != raw for item in hashes):
            _fail('input_conflict')
    elif fields['agent'] == 'dsh' and operation is not None and not fields['material_refs'] and not extra:
        # DSH's plain input has a native prompt SHA and original message SHA.
        message = TASK_PREFIX + fields['text']
        if (operation.get('message_sha256') != hashlib.sha256(message.encode()).hexdigest()
                or operation['request_sha256'] != request_sha256({'type': 'prompt', 'message': message})):
            _fail('input_conflict')
    elif fields['agent'] == 'pi' and operation is not None and not fields['material_refs'] and not extra:
        # A plain Pi receipt freezes the whole logical prompt, without a
        # separate raw intent. No derived material or memory is reconstructed.
        if operation['request_sha256'] != request_sha256({'type': 'prompt', 'message': TASK_PREFIX + fields['text']}):
            _fail('input_conflict')
    else:
        # Legacy receipts lacking a raw identity are not reconstructed by guess.
        _fail('evidence_unavailable')
    if operation is not None:
        for intent, digest_key in ((memory, 'derived_inputs_sha256'), (material, 'request_sha256')):
            if intent is not None and (intent.get(digest_key) != operation['request_sha256']
                    or intent is memory and intent.get('state') != 'ready'):
                _fail('evidence_unavailable')
    return True


def _choice(value, workspace, fields, *, memory=False):
    _scope(value, workspace, fields)
    if value.get('agent') != fields['agent'] or value.get('profile_current') is not True:
        _fail('selection_unavailable', 'not_sent')
    selection, profile = value.get('selection'), value.get('profile')
    prefix = 'memory_profile' if memory else 'ai_profile'
    if (type(selection) is not dict
            or set(selection) != {'revision', prefix + '_id', prefix + '_revision'}
            or type(selection['revision']) is not int or not 0 <= selection['revision'] <= 2 ** 53 - 1):
        _fail('selection_unavailable', 'not_sent')
    identity, revision = selection.get(prefix + '_id'), selection.get(prefix + '_revision')
    if identity is None and revision is None and profile is None:
        return None
    profile = _profile(profile, fields['agent'], memory=memory)
    if profile is None or (identity, revision) != (profile['id'], profile['revision']):
        _fail('selection_unavailable', 'not_sent')
    return profile


def _idle(value, workspace, fields):
    """Only public state is inspected. Actual admission remains in the owner."""
    if type(value) is not dict:
        _fail('evidence_unavailable')
    codex = fields['agent'] == 'codex'
    if codex:
        if type(value.get('binding')) is not dict or value['binding'].get('session_key') != 'chat/codex/' + fields['chat_id']:
            _fail('scope_changed')
    else:
        _scope(value, workspace, fields)
    connection = value.get('connection')
    if type(connection) is not dict or connection.get('state') not in ('ready', 'not_open', 'closed'):
        _fail('not_ready', 'not_sent')
    state = connection['state']
    request_key = 'extension_requests' if fields['agent'] == 'pi' else 'pending' if fields['agent'] == 'dsh' else 'requests'
    requests = value.get(request_key)
    if codex and state != 'ready' and requests is None:
        requests = []
    if type(requests) is not list or any(type(row) is not dict for row in requests):
        _fail('evidence_unavailable')
    if any(row.get('state') not in _RESOLVED for row in requests):
        _fail('needs_user', 'not_sent')
    operations = value.get('receipts' if codex else 'operations')
    if type(operations) is not list or any(type(row) is not dict for row in operations):
        _fail('evidence_unavailable')
    if any(row.get('status') not in _TERMINAL for row in operations):
        _fail('busy', 'not_sent')
    if state != 'ready':
        return False
    if codex:
        turn = value.get('turn_state')
        native = value.get('native')
        if (value.get('requests_available') is not True
                or turn is not None and (type(turn) is not dict or turn.get('status') not in _TERMINAL)
                or value['binding'].get('thread_id') is not None and (
                    type(native) is not dict or (native.get('thread') or {}).get('status', {}).get('type') != 'idle')):
            _fail('busy', 'not_sent')
    elif fields['agent'] in ('claude', 'dsh'):
        if type(value.get('activity')) is not dict or value['activity'].get('state') != 'idle':
            _fail('busy', 'not_sent')
    else:
        native = value.get('native_state')
        if type(native) is not dict:
            _fail('evidence_unavailable')
        if fields['agent'] == 'pi':
            if (native.get('isStreaming') is not False or native.get('isCompacting') is not False
                    or type(native.get('pendingMessageCount')) is not int or native['pendingMessageCount'] != 0
                    or (value.get('native_activity') or {}).get('continuing')):
                _fail('busy', 'not_sent')
        elif (native.get('running') is not False or native.get('queued') or native.get('inflight')
                or value.get('configuration_pending') or value.get('attachment_transaction') is not None):
            _fail('busy', 'not_sent')
    return True


def dispatch(service, fields):
    fields = _validate(fields)
    workspace = getattr(service, 'workspace_id', None)
    if type(workspace) is not str or _SHA.fullmatch(workspace) is None:
        _fail('scope_changed')
    name, root = ROUTES[fields['agent']]
    native = getattr(service, name, None)
    if not callable(native):
        _fail('native_unavailable', 'not_sent')
    path = root + '/' + fields['chat_id']

    def prior():
        try:
            value = native('GET', path + '/receipt', query=urlencode({'client_id': fields['client_id']}))
        except Exception:
            _fail('evidence_unavailable')
        return dict(forwarded=False, existing=True, response=deepcopy(value)) if _existing(value, workspace, fields) else None

    previous = prior()
    if previous is not None:
        return previous
    try:
        ai = (_choice(service.native_chat_configuration(fields['agent'], fields['chat_id']), workspace, fields)
              if fields['agent'] != 'dsh' else None)
        memory = _choice(service.native_chat_memory_configuration(fields['agent'], fields['chat_id']),
                         workspace, fields, memory=True)
    except NativeTaskInputError:
        raise
    except Exception:
        _fail('selection_unavailable', 'not_sent')
    body = _body(fields, ai, memory)
    try:
        query = 'include_items=0' if fields['agent'] == 'codex' else ''
        if not _idle(native('GET', path, query=query), workspace, fields):
            native('POST', path + '/open', body={})
        # Re-read after any open; neither a changed owner nor late native work
        # can turn a previously clean observation into an admission guarantee.
        if not _idle(native('GET', path, query=query), workspace, fields):
            _fail('not_ready', 'not_sent')
    except NativeTaskInputError:
        raise
    except Exception:
        _fail('native_unavailable')
    previous = prior()
    if previous is not None:
        return previous
    try:
        # Result tracking chooses only the native transport. It is outside the
        # frozen content/profile/memory hash, so prior receipts remain readable.
        send_body = dict(body, track_result=True) if fields['agent'] in ('hermes', 'pi') else body
        response = native('POST', path + '/send', body=send_body)
        if type(response) is not dict:
            _fail('submission_unknown')
        key = 'operation' if fields['agent'] in ('claude', 'dsh') else 'receipt'
        if key not in response or _operation(response[key], fields) is None:
            _fail('submission_unknown')
    except SpecialistHTTPError as error:
        if (fields['agent'] == 'pi' and error.code == 'pi_native_task_results_unavailable'
                and error.accepted is False and error.result_unknown is not True):
            _fail('native_unavailable', 'not_sent')
        _fail('submission_unknown')
    except Exception:
        # A local reservation may have committed even if its caller raised.
        # Never label this unsent, reopen, reread sources or post it again.
        _fail('submission_unknown')
    reused = response.get('reused') is True
    return dict(forwarded=not reused, existing=reused, response=deepcopy(response))
