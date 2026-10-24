"""Finite specialist HTTP routes; no transport or Agent loop.

Only a trusted caller supplies the holder. This adapter never chooses a cwd,
ledger, native thread or arbitrary RPC method from HTTP input. Native input and
human response objects retain their full JSON shape after bounded validation.
"""
import json
import re
from urllib.parse import parse_qs

from rabbit_runtime.codex_rpc import RemoteError, TransportError
from rabbit_runtime.codex_native_session import NativeSessionError
from rabbit_runtime.native_session_store import NativeSessionStoreError
from rabbit_runtime.native_session_lease import NativeSessionLeaseError
from rabbit_runtime.specialist_service import (SpecialistServiceError, SpecialistSendNotSubmitted,
                                SpecialistSendNotReady)

from native_http_common import SpecialistHTTPError, invalid, _json, validate_fields
from service_admission import admitted_native
from rabbit_runtime.specialist_tool_status import SpecialistToolStatusError

ROOT = '/api/specialist'
TOOLS_PATH = ROOT + '/tools'
POST_PATHS = frozenset(ROOT + '/' + action for action in ('open', 'send', 'answer', 'control', 'disconnect'))
_CLIENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z')
_SEQ = re.compile(r'(0|[1-9][0-9]{0,15})\Z')


def _decode(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                invalid()
            value[key] = item
        return value
    def constant(_value): invalid()
    try:
        text = raw.decode('utf-8') if type(raw) is bytes else raw
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        invalid()
    return value


def decode_body(raw):
    value = _decode(raw)
    if type(value) is not dict:
        invalid()
    return value


def _opaque(value, *, limit=256):
    if (type(value) is not str or not 1 <= len(value) <= limit
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        invalid()
    try: value.encode('utf-8')
    except UnicodeError: invalid()
    return value


def _client(value):
    if type(value) is not str or not _CLIENT.fullmatch(value):
        invalid()
    return value


def _query(raw):
    if type(raw) is not str or len(raw) > 8192:
        invalid()
    values = parse_qs(raw, keep_blank_values=True)
    if values.keys() - {'cursor', 'turn_id', 'after_seq', 'include_items'} or any(len(items) != 1 or not items[0] for items in values.values()):
        invalid()
    result = {}
    if 'include_items' in values:
        value = values['include_items'][0]
        if value not in ('0', '1') or value == '0' and ('cursor' in values or 'turn_id' in values):
            invalid()
        result['include_items'] = value == '1'
    if 'turn_id' in values:
        result['turn_id'] = _opaque(values['turn_id'][0])
    if 'after_seq' in values:
        value = values['after_seq'][0]
        if not _SEQ.fullmatch(value) or int(value) > 2**53 - 1:
            invalid()
        result['after_seq'] = int(value)
    if 'cursor' in values:
        cursor = _decode(values['cursor'][0])
        if type(cursor) is dict:
            if set(cursor) != {'type', 'itemId'} or cursor['type'] != 'item' or 'turn_id' not in result:
                invalid()
            _opaque(cursor['itemId'])
        elif type(cursor) is str:
            _opaque(cursor, limit=4096)
        else:
            invalid()
        result['cursor'] = cursor
    return result


def _tools_query(raw):
    try:
        if (type(raw) is not str or len(raw) > 8192
                or re.search(r'%(?![0-9A-Fa-f]{2})', raw)):
            invalid()
        values = parse_qs(raw, keep_blank_values=True, strict_parsing=True, errors='strict')
        if set(values) != {'thread_id', 'epoch'} or any(len(items) != 1 for items in values.values()):
            invalid()
        thread_id = _opaque(values['thread_id'][0])
        epoch = values['epoch'][0]
        if re.fullmatch(r'[0-9a-f]{32}', epoch) is None:
            invalid()
        return {'thread_id': thread_id, 'epoch': epoch}
    except (ValueError, UnicodeError):
        invalid()


class SpecialistHTTP:
    def __init__(self, holder, *, send_options=None, admission=None):
        self.admission = admission
        self.holder = holder
        # Trusted application metadata only. HTTP still accepts the same
        # strict input fields; it cannot supply arbitrary native options.
        self.send_options = send_options

    @admitted_native
    def dispatch(self, method, path, *, query='', body=None):
        # Preparation invokes no holder method. A rejected mutation here says
        # only that this HTTP attempt was unsent, never that an older unknown
        # attempt with the same client ID has been cancelled.
        try:
            operation, sending = self._prepare(method, path, query=query, body=body)
        except SpecialistHTTPError as error:
            if method != 'GET':
                error.accepted = False
            raise
        return self._call(operation, sending=sending)

    def _prepare(self, method, path, *, query='', body=None):
        if path not in (ROOT, TOOLS_PATH) and path not in POST_PATHS:
            raise SpecialistHTTPError(404, 'specialist_not_found', '找不到这个专员入口。')
        if method != ('GET' if path in (ROOT, TOOLS_PATH) else 'POST'):
            raise SpecialistHTTPError(405, 'specialist_method_not_allowed', '请使用专员页面提供的操作入口。')
        if path == TOOLS_PATH:
            if body is not None:
                invalid()
            fields = _tools_query(query)
            return (lambda: self.holder.tools(**fields)), False
        if method == 'GET':
            fields = _query(query)
            return (lambda: self.holder.view(**fields)), False
        if query:
            invalid()
        if type(body) is not dict:
            invalid()
        # This is only the existing IM.codes authentication wrapper, not a
        # native input field. The caller has verified it before dispatch.
        body = {key: value for key, value in body.items() if key != '_one_ai_csrf'}
        if path.endswith('/open'):
            validate_fields(body, ())
            return self.holder.open, False
        if path.endswith('/disconnect'):
            validate_fields(body, ())
            return self.holder.disconnect, False
        if path.endswith('/send'):
            validate_fields(body, ('client_id', 'inputs'))
            client = _client(body['client_id']); inputs = body['inputs']
            if type(inputs) is not list or not inputs or any(type(item) is not dict for item in inputs):
                invalid()
            if self.send_options:
                return (lambda: self.holder.send(client, inputs, options=self.send_options)), True
            return (lambda: self.holder.send(client, inputs)), True
        if path.endswith('/answer'):
            validate_fields(body, ('token', 'result'))
            token = _opaque(body['token'])
            if type(body['result']) is not dict:
                invalid()
            return (lambda: self.holder.answer(token, body['result'])), False
        validate_fields(body, ('action', 'client_id'), ('turn_id',))
        action, client = body['action'], _client(body['client_id'])
        if action == 'interrupt' and 'turn_id' in body:
            turn_id = _opaque(body['turn_id'])
        elif action == 'reconcile' and 'turn_id' not in body:
            turn_id = None
        else:
            invalid()
        return (lambda: self.holder.control(action, client, turn_id=turn_id)), False

    def _call(self, operation, *, sending=False):
        try:
            return operation()
        except SpecialistHTTPError:
            raise
        except SpecialistToolStatusError as error:
            status = 400 if error.code == 'invalid_arguments' else 503 if error.code in (
                'deadline', 'unavailable', 'unsupported') else 409
            raise SpecialistHTTPError(status, 'specialist_tools_' + error.code,
                '扩展工具连接摘要未能完整核验；原连接和对话保留，请稍后明确重试。') from None
        except SpecialistSendNotReady:
            if sending:
                raise SpecialistHTTPError(409, 'specialist_not_ready_before_send',
                    '原生连接尚未就绪，这条消息尚未发送；草稿保留，请打开连接后再试。', accepted=False) from None
            raise SpecialistHTTPError(409, 'specialist_state_conflict',
                '专员状态或请求归属已变化，请刷新核对原回执。') from None
        except SpecialistSendNotSubmitted:
            if sending:
                raise SpecialistHTTPError(409, 'specialist_thread_not_created',
                    '原生会话未创建，这条消息尚未发送；草稿保留，可另建一段原生对话后再试。', accepted=False) from None
            raise SpecialistHTTPError(409, 'specialist_state_conflict',
                '专员状态或请求归属已变化，请刷新核对原回执。') from None
        except RemoteError:
            # An explicit native rejection must not expose its private message.
            raise SpecialistHTTPError(409, 'specialist_native_rejected',
                '原生专员未接受这次操作，请核对当前状态。') from None
        except TransportError:
            raise SpecialistHTTPError(503, 'specialist_result_unconfirmed',
                '原生操作结果尚未确认，请查看原回执，不要重复发送。', result_unknown=True) from None
        except NativeSessionStoreError as error:
            if sending and error.code == 'busy':
                # begin() checks an existing client receipt first. This exact
                # failure precedes INSERT and turn/start, so this client turn
                # was unsent. The holder may already have created a thread;
                # do not claim that the whole call had zero native side effects.
                raise SpecialistHTTPError(409, 'specialist_busy',
                    '专员正在处理其他请求，这条消息尚未发送，草稿已保留。', accepted=False) from None
            raise SpecialistHTTPError(409, 'specialist_state_conflict',
                '专员状态或请求归属已变化，请刷新核对原回执。') from None
        except SpecialistServiceError as error:
            if error.reason == 'specialist_disconnect_pending':
                raise SpecialistHTTPError(409, 'specialist_disconnect_pending',
                    '本轮、审批或回执仍待确认，请先结束并核对再断开。') from None
            raise SpecialistHTTPError(409, 'specialist_state_conflict',
                '专员状态或请求归属已变化，请刷新核对原回执。') from None
        except (NativeSessionError, NativeSessionLeaseError):
            raise SpecialistHTTPError(409, 'specialist_state_conflict',
                '专员状态或请求归属已变化，请刷新核对原回执。') from None
        except (ValueError, TypeError):
            raise SpecialistHTTPError(400, 'specialist_invalid_request',
                '专员请求格式无效，请重新打开页面后再试。') from None
        except Exception:
            raise SpecialistHTTPError(503, 'specialist_operation_unconfirmed',
                '专员操作未完成，请核对当前状态与原回执。', result_unknown=True) from None


class _ValidateOnly:
    def open(self):
        raise AssertionError('validation must not execute a native operation')

    def disconnect(self):
        raise AssertionError('validation must not execute a native operation')


def validate_request(method, path, *, query='', body=None):
    """Validate the fixed native contract without selecting or calling an owner."""
    try:
        SpecialistHTTP(_ValidateOnly())._prepare(method, path, query=query, body=body)
    except SpecialistHTTPError as error:
        if method != 'GET':
            error.accepted = False
        raise
