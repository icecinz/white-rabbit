"""Readonly shared memory in the one trusted specialist, without an Agent loop.

The public scope is a specialist, never a catalogue chat. A deterministic private
UUID is only an index in the existing preparation table. Native thread, workdir,
role, fixed model policy and submission authority stay with the original holder.
"""
from copy import deepcopy
import re
from urllib.parse import parse_qs
import uuid

from memory_context import selected_memory_block
from native_http_common import SpecialistHTTPError, validate_fields
from native_memory_intents import NativeMemoryIntents, NativeMemoryIntentError, validate_selection
from rabbit_runtime.native_session_store import NativeSessionStoreError, request_sha256
from rabbit_specialist.specialist_connection import SPECIALIST_MODEL, SPECIALIST_EFFORT
from specialist_http import ROOT, _client, validate_request
from rabbit_runtime.specialist_service import SpecialistService

SCOPE = 'specialist'
SCOPE_ID = 'specialist/codex'
LIMIT = 64
RECEIPT = ROOT + '/receipt'
POLICY = {'model': SPECIALIST_MODEL, 'effort': SPECIALIST_EFFORT}


def _error(code, text, *, status=409, unsent=False, unknown=False):
    return SpecialistHTTPError(status, 'specialist_memory_' + code, text,
                              accepted=False if unsent else None, result_unknown=unknown)


def capable(holder):
    return (isinstance(holder, SpecialistService) and holder.session_key == SCOPE_ID
            and holder.fixed_model_options == POLICY)


def _unsubmitted(record):
    if record['state'] == 'failed':
        raise _error('prepare_failed', '原共享记忆准备失败，未进入原生提交；草稿保留，没有重试。', unsent=True)
    raise _error('submission_unconfirmed', '原发送编号已有共享记忆准备记录，尚无原生回执；没有重新检索或发送。', unknown=True)


class SpecialistMemoryHTTP:
    def __init__(self, inner, catalogue_path, *, workspace_id, recaller, query_filter=None):
        if not capable(inner.holder) or not callable(recaller):
            raise ValueError('A trusted fixed-policy specialist and recaller are required.')
        self.inner, self.holder = inner, inner.holder
        self.workspace_id, self.recaller = workspace_id, recaller
        self._cwd = self.holder.cwd
        self._key = str(uuid.uuid5(uuid.NAMESPACE_URL, 'xiaobaitu:shared-memory:' + workspace_id + '\0' + SCOPE_ID))
        self.intents = NativeMemoryIntents(catalogue_path, workspace_id=workspace_id, query_filter=query_filter)

    def _header(self):
        return {'workspace_id': self.workspace_id, 'scope': SCOPE, 'scope_id': SCOPE_ID}

    def _binding(self):
        if not capable(self.holder):
            raise _error('scope_changed', '专员固定身份或模型请求策略已变化，原请求保留。', status=503, unknown=True)
        binding = self.holder.store.read_binding(SCOPE_ID)
        if binding is None or binding['cwd'] != self._cwd:
            raise _error('scope_changed', '专员工作目录与固定身份无法核验，原请求保留。', status=503, unknown=True)
        return binding

    def _public(self, record):
        value = self.intents.public(record)
        if value is not None:
            if value.pop('chat_id') != self._key:
                raise _error('scope_changed', '专员共享记忆归属不同，原请求保留。', status=503)
            value.update(scope=SCOPE, scope_id=SCOPE_ID)
        return value

    def _metadata(self, client):
        self._binding()
        receipt = self.holder.store.read_operation(client, session_key=SCOPE_ID)
        memory = self.intents.read(self._key, client)
        if memory is not None and receipt is not None and (receipt['kind'] != 'turn_start'
                or memory['state'] != 'ready' or memory['derived_inputs_sha256'] is None):
            raise _error('metadata_unavailable', '专员记忆准备与原生回执不一致，请保留待核对状态。', status=503, unknown=True)
        return receipt, memory

    def _receipt(self, query, body):
        if body is not None or type(query) is not str or len(query) > 1024 or re.search(r'%(?![0-9A-Fa-f]{2})', query):
            raise _error('invalid_receipt', '请使用原发送编号只读核对专员回执。', status=400)
        try:
            args = parse_qs(query, keep_blank_values=True, strict_parsing=True, errors='strict', max_num_fields=2)
            if set(args) != {'client_id'} or len(args['client_id']) != 1:
                raise ValueError
            client = _client(args['client_id'][0])
        except (ValueError, UnicodeError):
            raise _error('invalid_receipt', '请使用一个完整的原发送编号。', status=400) from None
        receipt, memory = self._metadata(client)
        # Association metadata, not an independent reconstruction of a native
        # request: this GET has no original user inputs and performs no RPC.
        return {**self._header(), 'receipt': receipt, 'memory_intent': self._public(memory)}

    def _view(self, query, body):
        validate_request('GET', ROOT, query=query, body=body)
        self._binding()
        recent = self.intents.recent(self._key, limit=LIMIT)
        memory, receipts = [], []
        for record in recent['records']:
            receipt, checked = self._metadata(record['client_id'])
            memory.append(self._public(checked))
            if receipt is not None: receipts.append(receipt)
        # All saved metadata is checked before the original view may read RPC.
        result = self.inner.dispatch('GET', ROOT, query=query, body=body)
        combined = {row['client_id']: row for row in receipts}
        combined.update({row['client_id']: row for row in result.get('receipts', [])})
        return {**result, **self._header(), 'memory_intents': memory,
                'memory_intents_has_more': recent['has_more'], 'receipts': list(combined.values())}

    @staticmethod
    def _derived(raw, record):
        return [{'type': 'text', 'text': selected_memory_block(record['memory_inputs'], record['memory_profile'])},
                *deepcopy(raw['inputs'])]

    def _verify_native(self, raw, record, receipt):
        binding = self._binding()
        derived = self._derived(raw, record)
        if record['derived_inputs_sha256'] != request_sha256({'input': derived}):
            raise _error('request_conflict', '原共享记忆完整输入指纹不匹配，没有重新发送。')
        params = {**POLICY, 'threadId': binding['thread_id'], 'input': derived,
                  'clientUserMessageId': raw['client_id']}
        if binding['thread_id'] is None or receipt['request_sha256'] != request_sha256(params):
            raise _error('request_conflict', '原生专员参数与原共享记忆请求不一致，没有重新发送。')

    def _send(self, body, query):
        raw = {key: value for key, value in body.items() if key != '_one_ai_csrf'} if type(body) is dict else body
        validate_fields(raw, ('client_id', 'inputs'), ('memory_profile_id', 'memory_profile_revision'))
        base = {key: raw[key] for key in ('client_id', 'inputs')}
        validate_request('POST', ROOT + '/send', query=query, body=base)
        selected = validate_selection(raw) is not None
        receipt, record = self._metadata(raw['client_id'])
        if record is not None:
            if record['raw_request_sha256'] != request_sha256(raw):
                raise _error('request_conflict', '原发送编号包含不同的共享记忆请求，没有移除选择后重新发送。')
            if receipt is None: _unsubmitted(record)
            self._verify_native(raw, record, receipt)
            return {**self._header(), 'receipt': receipt, 'memory_intent': self._public(record), 'reused': True}
        if receipt is not None and selected:
            raise _error('request_conflict', '这个原生发送编号没有对应的记忆准备记录，未改写或重新发送。')
        if not selected:
            return self.inner.dispatch('POST', ROOT + '/send', query=query, body=base)
        if any(part.get('type') != 'text' or type(part.get('text')) is not str
                or part.keys() - {'type', 'text', 'text_elements'} for part in raw['inputs']):
            raise _error('invalid_input', '共享记忆只检索本轮完整文字，尚未发送。', status=400, unsent=True)
        self.inner._call(self.holder.require_ready)  # No sensitive recall before explicit open.
        record = self.intents.prepare(self._key, raw, self.recaller)
        if not record.get('created'): _unsubmitted(record)
        derived = self._derived(raw, record)
        record = self.intents.bind_inputs(self._key, raw, derived)
        result = self.inner.dispatch('POST', ROOT + '/send', body={'client_id': raw['client_id'], 'inputs': derived})
        receipt = result.get('receipt')
        if receipt is not None: self._verify_native(raw, record, receipt)
        return {**result, **self._header(), 'memory_intent': self._public(record)}

    def _retain_uncertainty(self, body, error):
        if type(body) is not dict: return error
        try: client = _client(body.get('client_id'))
        except SpecialistHTTPError: return error
        try:
            receipt, memory = self._metadata(client)
        except (NativeMemoryIntentError, NativeSessionStoreError, SpecialistHTTPError):
            return _error('metadata_unavailable', '专员本地回执元数据无法核验，请保留原编号。', status=503, unknown=True)
        if receipt is not None or memory is not None and memory['state'] != 'failed':
            error.accepted = None
            if receipt is None: error.result_unknown = True
        elif receipt is None and memory is None and not error.result_unknown:
            error.accepted = False
        return error

    def dispatch(self, method, path, *, query='', body=None):
        try:
            if path == RECEIPT:
                if method != 'GET': raise _error('invalid_receipt', '回执只接受只读查询。', status=405)
                return self._receipt(query, body)
            if path == ROOT and method == 'GET': return self._view(query, body)
            if path == ROOT + '/send' and method == 'POST': return self._send(body, query)
            return self.inner.dispatch(method, path, query=query, body=body)
        except NativeMemoryIntentError as error:
            status = 503 if error.code == 'invalid_record' else 409
            failure = _error(error.code, str(error), status=status,
                unsent=method != 'GET' and error.code not in ('invalid_record', 'intent_conflict'),
                unknown=error.code == 'invalid_record')
        except NativeSessionStoreError:
            failure = _error('metadata_unavailable', '专员本地回执元数据无法核验，请保留原编号。', status=503, unknown=True)
        except SpecialistHTTPError as error:
            failure = error
        if method == 'POST' and path == ROOT + '/send':
            failure = self._retain_uncertainty(body, failure)
        raise failure from None
