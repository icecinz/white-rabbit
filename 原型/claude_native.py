"""Claude-native ownership and HTTP composition, separate from the text bridge.

The original SDK/CLI owns execution and transcripts. Application state contains
only catalogue metadata, exact identity anchors and conservative receipts.
The event reader alone consumes the worker stream; navigation never closes it.
"""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import threading
from urllib.parse import parse_qs, quote
import uuid

from native_catalogue import NativeChatCatalogue, chat_id
from rabbit_runtime.native_session_store import request_sha256
from native_http_common import SpecialistHTTPError, validate_fields
from rabbit_runtime.claude.session_store import ClaudeNativeStore
from rabbit_runtime.claude.rpc import ClaudeRpcError, ClaudeRemoteError
from claude_material_intents import ClaudeMaterialIntents, ClaudeMaterialIntentError
from native_materials import NativeMaterialError, validate_refs
from native_memory_intents import NativeMemoryIntents, NativeMemoryIntentError, validate_selection
from native_memory_selection import NativeMemorySelections
from native_profile_intents import NativeProfileIntents, NativeProfileIntentError
from native_profile_catalog import (NativeProfileUnavailable, normalize_claude_catalogue,
    resolve_claude_profile, project_claude_profiles)
from profiles import AIProfiles
from memory_context import selected_memory_block
from rabbit_runtime.claude.message_view import original_record, original_image, ClaudeMessageViewError
from rabbit_runtime.claude.material_protocol import (canonical_metadata, user_content, ClaudeMaterialProtocolError,
    request as material_request, MAX_VIEW_BYTES as HISTORY_VIEW_BYTES)
from native_maintenance import maintenance_state as observe_maintenance
from service_admission import admitted_native

ROOT = '/api/claude-chats'
MAX_CONNECTIONS = 1
MAX_PROMPT_BYTES = 128 * 1024
MAX_VIEW_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 512
_MISSING = object()


def _read_query(query, fields, *, defaults=None):
    try:
        parsed = parse_qs(query, keep_blank_values=True, strict_parsing=True) if query else {}
        expected = set(fields)
        if set(parsed) - expected or any(len(values) != 1 for values in parsed.values()):
            raise ValueError
        values = {key: values[0] for key, values in parsed.items()}
        for key, value in (defaults or {}).items():
            values.setdefault(key, value)
        if set(values) != expected:
            raise ValueError
        return values
    except ValueError:
        raise _error('receipt_invalid', '请使用完整、单一的Claude原生读取引用。', status=400, unsent=False) from None


def _index(value):
    if (type(value) is not str or not value.isascii() or not value.isdecimal()
            or not 1 <= len(value) <= 6 or len(value) > 1 and value.startswith('0') or int(value) > 100000):
        raise _error('record_reference_invalid', 'Claude SDK记录位置无效。', status=400, unsent=False)
    return int(value)


def _error(code, text, *, status=409, unsent=True):
    return SpecialistHTTPError(status, 'claude_native_' + code, text,
                              accepted=False if unsent else None)


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise _error('invalid_id', 'Claude原生请求编号无效。', status=400) from None
    return value


def _fields(body, required=(), optional=()):
    value = {key: item for key, item in body.items() if key != '_one_ai_csrf'} if type(body) is dict else body
    validate_fields(value, required, optional)
    return value


def _message(value):
    try:
        if type(value) is not str or not value.strip() or len(value.encode('utf-8')) > MAX_PROMPT_BYTES:
            raise ValueError
    except (ValueError, UnicodeError):
        raise _error('invalid_message', '消息需要非空文字，最多128 KiB。', status=400) from None
    return value


def _unsubmitted_memory(record):
    if record['state'] == 'failed':
        raise _error('memory_prepare_failed', '原共享记忆准备失败，未进入原生提交；草稿保留，没有重试。')
    raise SpecialistHTTPError(409, 'claude_native_memory_submission_unconfirmed',
        '原发送编号已有共享记忆准备记录，尚无原生回执；保留待核对状态，没有重新检索或发送。', result_unknown=True)


def _recover_send(store, material_intents, memory_intents, identity, body, profile_intents=None):
    """Saved local identities only; no owner, source read, SDK or recall."""
    client = body['client_id']
    operation = store.lookup(client)
    profile = profile_intents.lookup(identity, body) if profile_intents is not None else None
    memory = memory_intents.read(identity, client) if memory_intents is not None else None
    intent = material_intents.lookup(identity, client) if material_intents is not None else None
    if memory is not None and memory['raw_request_sha256'] != request_sha256(body):
        raise _error('memory_request_conflict', '原共享记忆请求身份不同，未重新发送。', unsent=False)
    if intent is not None:
        if 'material_refs' not in body:
            raise _error('request_conflict', '原发送编号包含材料身份，未移除材料后重新发送。', unsent=False)
        intent = material_intents.lookup_body(identity, body)
    if operation is not None:
        if operation['chat_id'] != identity:
            raise _error('request_conflict', '原回执属于不同Claude对话。', unsent=False)
        if 'ai_profile_id' in body and profile is None:
            raise _error('profile_request_conflict', '原回执没有这份模型配置意图，未重新发送。', unsent=False)
        selected = validate_selection(body) is not None
        if selected or memory is not None:
            if (memory is None or memory['state'] != 'ready' or not memory['derived_inputs_sha256']
                    or memory['derived_inputs_sha256'] != operation['request_sha256']
                    or operation['message_sha256'] != hashlib.sha256(body['message'].encode()).hexdigest()
                    or 'material_refs' in body and intent is None):
                raise _error('memory_request_conflict', '原回执缺少匹配的完整记忆输入身份，未重新发送。', unsent=False)
        elif intent is None and ('material_refs' in body or operation['request_sha256'] !=
                request_sha256({'type': 'prompt', 'message': body['message']})):
            raise _error('request_conflict', '原发送编号已有不同消息或材料。', unsent=False)
        if intent is not None and intent['request_sha256'] != operation['request_sha256']:
            raise _error('request_conflict', '原材料输入与原生回执不匹配。', unsent=False)
        return {'operation': operation, **({'material_intent': intent} if intent is not None else {}),
            **({'profile_intent': profile} if profile is not None else {}),
            **({'memory_intent': memory_intents.public(memory)} if memory is not None else {}), 'reused': True}
    if memory is not None: _unsubmitted_memory(memory)
    return None


def _with_memory(message, prepared, memory):
    # Only host context is composed. The final native text block and the
    # ledger's message SHA retain the original user's exact message.
    context = selected_memory_block(memory['memory_inputs'], memory['memory_profile'])
    previous = prepared['material_input'] if prepared is not None else None
    images = deepcopy(previous['images']) if previous is not None else []
    if previous is not None: context += '\n\n' + previous['context_text']
    metadata = [{key: image[key] for key in ('sha256', 'mime', 'size_bytes')} for image in images]
    request = material_request(message, context, metadata)
    digest = request_sha256(request)
    packet = {'schema_version': 1, 'context_text': context, 'images': images, 'request_sha256': digest}
    user_content(message, packet)  # Recheck complete text/image/frame budgets.
    return {**(prepared or {}), 'message': message, 'request': request,
            'material_input': packet, 'request_sha256': digest}


class ClaudeNativeHolder:
    def __init__(self, store, identity, connection, *, workspace_id, materials=None, material_intents=None,
                 memory_intents=None, memory_recall=None, memory_selections=None,
                 profile_intents=None, catalogue_observer=None):
        self.store, self.identity, self.connection = store, chat_id(identity), connection
        self.workspace_id = workspace_id
        self.materials, self.material_intents = materials, material_intents
        self.memory_intents, self.memory_recall = memory_intents, memory_recall
        self.memory_selections = memory_selections
        self.profile_intents, self._catalogue_observer = profile_intents, catalogue_observer
        self._action = threading.RLock()
        self._cache = threading.Condition(threading.RLock())
        self._rpc = self._reader = None
        self._stop = threading.Event()
        self._epoch = secrets.token_hex(16)
        self._state, self._problem = 'not_open', ''
        self._slot_reserved = False
        self._records, self._record_bytes = [], 0
        self._omitted = self._consumed = 0
        self._requests = OrderedDict()
        self._active = None
        self._info = None
        self._history = {'available': False, 'has_more': False}

    def _record(self, value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(raw) > MAX_VIEW_BYTES // 2:
            value = {'sdk_type': value.get('sdk_type', 'Unknown'), 'data': {
                'display_truncated': True, 'original_bytes': len(raw),
                'sha256': hashlib.sha256(raw).hexdigest()}}
            raw = json.dumps(value).encode('utf-8')
        self._records.append((deepcopy(value), len(raw))); self._record_bytes += len(raw)
        while len(self._records) > MAX_RECORDS or self._record_bytes > MAX_VIEW_BYTES:
            _, count = self._records.pop(0); self._record_bytes -= count; self._omitted += 1

    def _binding(self):
        return self.store.binding(self.identity)

    def _scope(self, value):
        return type(value) is dict and value.get('session_id') == self._binding()['session_id']

    def _apply_info(self, info):
        binding = self._binding()
        if (type(info) is not dict or info.get('configured_session_id') != binding['session_id']
                or info.get('cwd') != binding['cwd']
                or info.get('observed_session_id') not in (None, binding['session_id'])):
            raise _error('identity_mismatch', '原生Claude身份与固定会话不一致，未另建替代会话。', unsent=False)
        descriptor = info.get('native_file')
        if descriptor is not None:
            self.store.attach_file(self.identity, descriptor)
            creation = self.store.creation(self.identity)
            # A result may have been durably received immediately before the
            # previous service exited. Zero-prompt resume need not emit init;
            # that exact prior Result plus this same original file preserves
            # its proof without inventing a new live observation or query.
            prior_identity = creation is not None and any(
                row['session_id'] == binding['session_id'] and row['epoch'] == creation['epoch']
                and row['status'] in ('completed', 'failed', 'interrupted')
                and row.get('native_result_uuid') is not None
                for row in self.store.operations(self.identity, limit=128))
            if creation is not None and (info.get('identity_verified') is True or prior_identity):
                self.store.confirm_create(self.identity, creation['client_id'], binding['session_id'], creation['epoch'])
        with self._cache:
            self._info = deepcopy(info)

    def _refresh_info(self):
        binding = self._binding()
        info = self._rpc.request('session.info', {'session_id': binding['session_id']}, timeout=15)
        self._apply_info(info)

    def open(self):
        with self._action:
            if self._rpc is not None:
                if self._state == 'ready' and self._rpc.running:
                    return self.view()
                raise _error('connection_unconfirmed', '原连接仍需核对，未另开进程。')
            binding = self._binding()
            creation = self.store.creation(self.identity)
            # An adopted original file is already a native conversation. It has
            # no application-issued native-creation receipt and must never be
            # reopened with a new --session-id invocation.
            resume = creation is not None or binding.get('native_file') is not None
            if not resume:
                creation = self.store.begin_create(self.identity, str(uuid.uuid4()), self._epoch)
                if not creation['created']:
                    raise _error('creation_unconfirmed', '原生创建已有未确认回执，未另建会话。')
            self._state, self._problem = 'opening', ''
            rpc = self.connection.create_rpc(cwd=binding['cwd'], session_id=binding['session_id'], resume=resume)
            self._rpc = rpc
            try:
                rpc.start()
                self._refresh_info()
                if resume:
                    history = rpc.request('session/history', {'session_id': binding['session_id'], 'limit': 100}, timeout=20)
                    if (not self._scope(history) or history.get('cwd') != binding['cwd']
                            or type(history.get('messages')) is not list or type(history.get('native_file')) is not dict):
                        raise ValueError('Unverified original Claude history.')
                    self.store.attach_file(self.identity, history['native_file'])
                    with self._cache:
                        for record in history['messages']: self._record(record)
                        self._history = {'available': True, 'has_more': history.get('has_more') is True,
                                         'next_offset': history.get('next_offset')}
                self._state = 'ready'
                self._reader = threading.Thread(target=self._read, name='rabbit-claude-reader', daemon=True)
                self._reader.start()
                self._barrier()
                return self.view()
            except BaseException:
                self._state, self._problem = 'failed', '原生连接未能核实；固定会话保留，未另建或重发。'
                try: rpc.close()
                except BaseException: pass  # Keep owner/lease until close is proven.
                raise _error('open_unconfirmed', self._problem, status=503) from None

    def _barrier(self):
        if self._rpc is None or self._reader is None or threading.current_thread() is self._reader:
            return
        target = self._rpc.stats.get('events_received', 0)
        with self._cache:
            self._cache.wait_for(lambda: self._consumed >= target or self._state == 'failed' or self._stop.is_set(), timeout=2)

    def _read(self):
        while not self._stop.is_set():
            try:
                event = self._rpc.next_event(timeout=.15)
            except TimeoutError:
                continue
            except BaseException:
                if not self._stop.is_set():
                    with self._cache:
                        self._state, self._problem = 'failed', '原生事件连接中断；待确认请求保留，不自动重发。'
                        self._cache.notify_all()
                return
            try:
                with self._cache:
                    self._event(event)
            except BaseException:
                with self._cache:
                    self._state, self._problem = 'failed', '原生身份或事件未能核对，已停止接收新请求。'
            finally:
                with self._cache:
                    self._consumed += 1; self._cache.notify_all()

    def _event(self, event):
        if not self._scope(event): raise ValueError('Foreign native event.')
        kind, client = event.get('type'), event.get('client_id')
        binding = self._binding()
        if kind == 'sdk.message':
            record = event.get('message')
            if type(record) is not dict or type(record.get('data')) is not dict:
                raise ValueError('Invalid native record.')
            self._record(record)
            if record.get('sdk_type') == 'ResultMessage' and client is not None:
                data = record['data']
                origin = data.get('origin')
                human = origin is None or type(origin) is dict and origin.get('kind') == 'human'
                if (client != self._active or data.get('session_id') != binding['session_id'] or not human):
                    raise ValueError('Result does not prove the active original query.')
                status = 'failed' if data.get('is_error') is True else 'completed'
                if data.get('terminal_reason') in ('aborted_streaming', 'aborted_tools'): status = 'interrupted'
                self.store.finish(client, binding['session_id'], self._epoch, data.get('uuid'),
                                  status=status, result=record)
                self._active = None
        elif kind == 'prompt.accepted':
            if client != self._active: raise ValueError('Foreign prompt acknowledgement.')
            self.store.accepted(client, binding['session_id'], self._epoch,
                                event.get('native_user_uuid'))
        elif kind == 'session.identity':
            if event.get('cwd') not in (None, binding['cwd']): raise ValueError('Foreign native cwd.')
        elif kind == 'permission.request':
            native = event.get('request') if type(event.get('request')) is dict else event
            request_id = native.get('request_id')
            if (type(request_id) is not str or not request_id or request_id in self._requests
                    or self._active is None or client not in (None, self._active)):
                raise ValueError('Permission request is not owned by an active query.')
            self._requests[request_id] = {'token': secrets.token_hex(16), 'epoch': self._epoch,
                'state': 'pending', 'answerable': native.get('tool_name') != 'AskUserQuestion',
                'tool_name': native.get('tool_name'), 'input': deepcopy(native.get('input')),
                'tool_use_id': native.get('tool_use_id'), 'session_id': binding['session_id'],
                'request_id': request_id}
            while len(self._requests) > 64:
                key, row = next(iter(self._requests.items()))
                if row['state'] == 'pending': raise ValueError('Too many pending native requests.')
                self._requests.pop(key)
        elif kind == 'permission.resolved':
            row = self._requests.get(event.get('request_id'))
            if row is not None:
                row['state'] = 'expired' if event.get('state') == 'expired' else 'answered'
                row['answerable'] = False
        elif kind == 'session.closed':
            self._state = 'closed'
            for row in self._requests.values(): row.update(state='expired', answerable=False)
        else:
            self._record({'sdk_type': 'WorkerEvent', 'data': deepcopy(event)})

    def models(self, *, remember=True):
        """Observe initialize candidates on this already-open SDK connection."""
        with self._action:
            with self._cache:
                if self._state != 'ready' or self._rpc is None or not self._rpc.running or type(self._info) is not dict:
                    raise _error('models_not_open', '请先明确连接这段Claude原生对话，再更新模型目录。')
                scope = (self._epoch, self._rpc, self._reader, self._info.get('configured_session_id'), self._info.get('cwd'))
            try:
                raw = scope[1].request('model.options', {'session_id': scope[3]}, timeout=20)
            except ClaudeRpcError:
                raise NativeProfileUnavailable('Claude SDK 模型目录暂时无法读取，未替换原目录。') from None
            with self._cache:
                current = (self._epoch, self._rpc, self._reader,
                    self._info.get('configured_session_id') if type(self._info) is dict else None,
                    self._info.get('cwd') if type(self._info) is dict else None)
                if self._state != 'ready' or current != scope or not self._rpc.running:
                    raise _error('models_changed', 'Claude连接已变化，目录未替换，请重新核对。', unsent=False)
            catalogue = normalize_claude_catalogue(raw)
            if remember and callable(self._catalogue_observer):
                self._catalogue_observer(self.identity, catalogue)
            return catalogue

    def send(self, client_id, message, material_refs=_MISSING,
             memory_profile_id=_MISSING, memory_profile_revision=_MISSING,
             ai_profile_id=_MISSING, ai_profile_revision=_MISSING):
        client_id, message = _uuid(client_id), _message(message)
        body = {'client_id': client_id, 'message': message}
        refs = []
        if material_refs is not _MISSING:
            refs = validate_refs(material_refs)
            if not refs:
                raise _error('invalid_materials', '没有所选材料时请使用普通文字入口。', status=400)
            body['material_refs'] = refs
        if memory_profile_id is not _MISSING: body['memory_profile_id'] = memory_profile_id
        if memory_profile_revision is not _MISSING: body['memory_profile_revision'] = memory_profile_revision
        if ai_profile_id is not _MISSING: body['ai_profile_id'] = ai_profile_id
        if ai_profile_revision is not _MISSING: body['ai_profile_revision'] = ai_profile_revision
        NativeProfileIntents._request_identity(body, 'claude')
        selected = validate_selection(body) is not None
        with self._action:
            replay = _recover_send(self.store, self.material_intents, self.memory_intents, self.identity, body, self.profile_intents)
            if replay is not None: return replay
            self._barrier()
            if self._state != 'ready' or self._rpc is None or not self._rpc.running:
                raise _error('not_ready', '请先明确连接这段原生对话，消息尚未发送。')
            with self._cache:
                if self._active is not None or any(row['state'] in ('pending', 'unknown') for row in self._requests.values()):
                    raise _error('busy', '原生任务或审批尚未结束。')
            if self.store.active_for_chat(self.identity) is not None:
                raise _error('busy', 'Claude还有待确认的原发送回执，尚未读取或发送新材料。')
            profile_intent = None
            legacy_material = (refs and 'ai_profile_id' not in body and self.material_intents is not None
                and self.material_intents.lookup(self.identity, client_id) is not None
                and (self.profile_intents is None or self.profile_intents.lookup(self.identity, body) is None))
            if self.profile_intents is not None and not legacy_material:
                def resolve(profile):
                    return resolve_claude_profile(profile, self.models() if profile['model_id'] else None)
                profile_intent = self.profile_intents.freeze(self.identity, body, resolve)
            elif 'ai_profile_id' in body:
                raise _error('profiles_unavailable', '这条Claude入口尚未启用原生模型配置，消息尚未发送。')
            memory = None
            if selected:
                if self.memory_intents is None or not callable(self.memory_recall):
                    raise _error('memory_unavailable', '这条Claude入口尚未启用只读共享记忆，草稿保留。')
                memory = self.memory_intents.prepare(self.identity, body, self.memory_recall)
                if not memory.get('created'): _unsubmitted_memory(memory)
            prepared, intent = None, None
            if refs:
                if self.materials is None or self.material_intents is None:
                    raise _error('materials_unavailable', '这条Claude原生连接没有材料读取与独立意图保护。')
                intent = self.material_intents.freeze(self.identity, body)
                prepared = self.materials.prepare_claude(message, refs)
                try:
                    valid = (type(prepared) is dict and prepared.get('material_refs') == refs and prepared.get('message') == message
                        and request_sha256(prepared.get('request')) == prepared.get('request_sha256')
                        and type(prepared.get('material_input')) is dict
                        and prepared['material_input'].get('request_sha256') == prepared['request_sha256'])
                    if valid: user_content(message, prepared['material_input'])
                except (ValueError, TypeError, UnicodeError, KeyError):
                    valid = False
                if not valid:
                    raise _error('material_prepare_invalid', 'Claude原生材料没有完整核对，尚未提交。')
            if selected:
                prepared = _with_memory(message, prepared, memory)
                memory = self.memory_intents.bind_claude_request(self.identity, body, prepared['request'])
            if intent is not None:
                intent = self.material_intents.bind_request(self.identity, client_id, intent['raw_request_sha256'], prepared['request_sha256'])
            try:
                if prepared is None and profile_intent is None and self.memory_selections is not None:
                    with self.memory_selections.none_fence('claude', self.identity) as assert_none:
                        replay = _recover_send(self.store, self.material_intents, self.memory_intents, self.identity, body, self.profile_intents)
                        if replay is not None:
                            return replay
                        assert_none()
                        receipt = self.store.begin(self.identity, client_id, message, self._epoch)
                else:
                    receipt = self.store.begin(self.identity, client_id, message, self._epoch,
                        **({'request': prepared['request']} if prepared is not None else {}))
            except BaseException:
                try:
                    reserved = self.store.lookup(client_id)
                except Exception:
                    reserved = True
                if reserved is not None:
                    raise _error('submission_unknown', '原请求已登记，提交尚未确认；请核对原回执，不自动重发。', unsent=False) from None
                raise
            metadata = {**({'profile_intent': profile_intent} if profile_intent is not None else {}),
                **({'material_intent': intent} if intent is not None else {}),
                **({'memory_intent': self.memory_intents.public(memory)} if memory is not None else {})}
            if not receipt['created']: return {'operation': receipt, **metadata}
            with self._cache: self._active = client_id
            try:
                value = self._rpc.request('prompt.submit', {'session_id': receipt['session_id'],
                    'client_id': client_id, 'message': message,
                    **(profile_intent['native_options'] if profile_intent is not None else {}),
                    **({'material_input': prepared['material_input']} if prepared is not None else {})}, timeout=20)
                if type(value) is not dict or value.get('submitted') is not True or value.get('client_id') != client_id or value.get('session_id') != receipt['session_id']:
                    raise ValueError('Invalid prompt acknowledgement.')
                self._barrier()
                return {'operation': self.store.lookup(client_id), **metadata}
            except BaseException as error:
                self._barrier()
                if isinstance(error, ClaudeRemoteError) and error.code == 'model_apply_unconfirmed' and error.result_unknown is False:
                    try:
                        rejected = self.store.rejected(client_id, receipt['session_id'], self._epoch)
                    except Exception:
                        pass  # Contradictory native evidence keeps the original unknown barrier.
                    else:
                        with self._cache:
                            if self._active == client_id:
                                self._active = None
                        return {'operation': rejected, **metadata}
                current = self.store.lookup(client_id)
                if current['status'] in ('accepted', 'rejected', 'completed', 'failed', 'interrupted'):
                    return {'operation': current, **metadata}
                raise _error('submission_unknown', '这次原生提交结果不明；原请求已保留，只核对，不自动重发。', unsent=False) from None

    def _history_ready(self):
        if self._state != 'ready' or self._rpc is None or not self._rpc.running:
            raise _error('history_not_open', '请先明确连接这段Claude原生对话，再读取SDK显示记录。', unsent=False)
        self._barrier()
        with self._cache:
            busy = self._active is not None or any(row['state'] in ('pending', 'unknown') for row in self._requests.values())
        if busy or self.store.active_for_chat(self.identity) is not None:
            raise _error('history_busy', 'Claude原生任务或交互尚未结束，完整历史读取暂缓。', unsent=False)
        return self._binding()

    def _history_scope(self, result, binding):
        if (type(result) is not dict or result.get('session_id') != binding['session_id']
                or result.get('cwd') != binding['cwd'] or type(result.get('native_file')) is not dict):
            raise _error('history_unverified', 'Claude SDK记录与原生身份不符，未投影到当前对话。', unsent=False)
        self.store.attach_file(self.identity, result['native_file'])
        return {'workspace_id': self.workspace_id, 'chat_id': self.identity,
                'epoch': self._epoch, 'session_id': binding['session_id'], 'cwd': binding['cwd']}

    def _history_request(self, method, params):
        try:
            return self._rpc.request(method, params, timeout=20)
        except ClaudeRemoteError as error:
            changed = type(error.code) is str and error.code in ('history_record_changed', 'history_changed')
            raise _error('record_changed' if changed else 'history_unverified',
                'Claude SDK记录版本已变化，请刷新后重新读取。' if changed else 'Claude SDK完整记录暂未核实，原显示保留。',
                status=409 if changed else 503, unsent=False) from None
        except ClaudeRpcError:
            raise _error('history_unverified', 'Claude SDK完整记录通路暂不可用，原显示保留。', status=503, unsent=False) from None

    def read_history(self, offset=0):
        with self._action:
            binding = self._history_ready()
            result = self._history_request('session/history.page',
                {'session_id': binding['session_id'], 'offset': offset, 'limit': 100})
            scope = self._history_scope(result, binding)
            expected = {'session_id', 'cwd', 'native_file', 'native_records', 'native_record_refs', 'native_records_info'}
            info = result.get('native_records_info')
            rows, refs = result.get('native_records'), result.get('native_record_refs')
            if (set(result) != expected or type(rows) is not list or type(refs) is not list
                    or len(rows) != len(refs) or not 0 <= len(rows) <= 100 or type(info) is not dict
                    or set(info) != {'offset', 'end_offset', 'has_more', 'next_offset', 'projected'}
                    or type(info.get('offset')) is not int or info['offset'] != offset
                    or type(info.get('end_offset')) is not int or info['end_offset'] != offset + len(rows)
                    or type(info.get('has_more')) is not bool or type(info.get('projected')) is not bool
                    or info['has_more'] and type(info.get('next_offset')) is not int
                    or not info['has_more'] and info.get('next_offset') is not None
                    or info['next_offset'] != (offset + len(rows) if info['has_more'] else None)
                    or info['has_more'] and not rows
                    or any(type(row) is not dict or set(row) != {'sdk_type', 'data'}
                        or row['sdk_type'] != 'SessionMessage' or type(row['data']) is not dict
                        or row['data'].get('session_id') != binding['session_id']
                        or row['data'].get('uuid') != refs[index].get('uuid')
                        for index, row in enumerate(rows) if type(refs[index]) is dict)
                    or any(type(ref) is not dict or set(ref) != {'offset', 'uuid', 'sha256', 'projected'}
                        or type(ref['offset']) is not int or ref['offset'] != offset + index
                        or type(ref['projected']) is not bool
                        or type(ref['sha256']) is not str or len(ref['sha256']) != 64
                        or any(c not in '0123456789abcdef' for c in ref['sha256'])
                        or ref['uuid'] is not None and (type(ref['uuid']) is not str or not 1 <= len(ref['uuid']) <= 256
                            or any(ord(c) < 32 or ord(c) == 127 for c in ref['uuid']))
                        for index, ref in enumerate(refs))):
                raise _error('history_unverified', 'Claude SDK分页身份或完整范围无法核对。', unsent=False)
            page = {key: result[key] for key in ('native_records', 'native_record_refs', 'native_records_info')}
            canonical_metadata(page, max_bytes=HISTORY_VIEW_BYTES)
            return {**scope, **page}

    def read_history_record(self, offset, record_uuid, sha256, *, part_index=None):
        with self._action:
            binding = self._history_ready()
            result = self._history_request('session/history.record', {'session_id': binding['session_id'],
                'offset': offset, 'uuid': record_uuid, 'sha256': sha256})
            scope = self._history_scope(result, binding)
            expected = {'session_id', 'cwd', 'native_file', 'record', 'reference'}
            reference = {'offset': offset, 'uuid': record_uuid, 'sha256': sha256, 'projected': True}
            if set(result) != expected or result.get('reference') != reference:
                raise _error('history_unverified', 'Claude SDK完整记录回读引用不匹配。', unsent=False)
            record = original_record(result['record'], reference)
            if record['sdk_type'] != 'SessionMessage' or record['data'].get('session_id') != binding['session_id']:
                raise _error('history_unverified', 'Claude SDK完整记录不属于当前会话。', unsent=False)
            if part_index is not None:
                return {**scope, 'reference': reference, 'part_index': part_index,
                        'image': original_image(record, reference, part_index)}
            return {**scope, 'reference': reference, 'record_json': json.dumps(record,
                ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)}

    def answer(self, token, epoch, result):
        if type(result) is not dict or set(result) != {'behavior'} or result['behavior'] not in ('allow', 'deny'):
            raise _error('invalid_answer', '只支持本次允许或拒绝，不修改长期权限。', status=400)
        with self._action:
            self._barrier()
            with self._cache:
                rows = [row for row in self._requests.values() if row['token'] == token]
                if epoch != self._epoch or len(rows) != 1 or rows[0]['state'] != 'pending' or not rows[0]['answerable'] or self._state != 'ready':
                    raise _error('answer_expired', '这份原生审批已变化或不能在本页回答，未重复提交。')
                row = rows[0]; row.update(state='unknown', answerable=False)
                params = {'session_id': row['session_id'], 'request_id': row['request_id'],
                          'decision': 'allow_once' if result['behavior'] == 'allow' else 'deny'}
            try:
                response = self._rpc.request('permission.respond', params, timeout=15)
                if (type(response) is not dict or response.get('responded') is not True
                        or response.get('request_id') != row['request_id']
                        or response.get('session_id') != row['session_id']):
                    raise ValueError('Unverified native permission acknowledgement.')
                self._barrier()
                return {'answered': True, 'token': token, 'epoch': self._epoch}
            except BaseException:
                raise _error('answer_unknown', '审批答复结果不明，未重复提交。', unsent=False) from None

    def interrupt(self, client_id):
        client_id = _uuid(client_id)
        with self._action:
            old = self.store.interrupt_receipt(client_id)
            if old is not None:
                if old['chat_id'] != self.identity:
                    raise _error('request_conflict', '原停止请求不属于当前原生对话。')
                return {'operation': old}
            self._barrier()
            if self._state != 'ready' or self._active is None:
                raise _error('not_running', '当前没有已确认属于本连接的运行轮次。')
            receipt = self.store.interrupt(self.identity, client_id, self._epoch, self._active)
            if not receipt['created']: return {'operation': receipt}
            try:
                response = self._rpc.request('interrupt', {'session_id': receipt['session_id']}, timeout=15)
                if (type(response) is not dict or response.get('interrupt_requested') is not True
                        or response.get('session_id') != receipt['session_id']):
                    raise ValueError('Unverified native interrupt acknowledgement.')
                return {'operation': self.store.finish_interrupt(client_id, receipt['session_id'], self._epoch)}
            except BaseException:
                raise _error('interrupt_unknown', '停止请求结果不明，只核对原请求，不自动再次发送。', unsent=False) from None

    def view(self, client_id=None):
        memories = []
        if self.memory_intents is not None:
            clients = list(dict.fromkeys([row['client_id'] for row in self.store.operations(self.identity)]
                + ([client_id] if client_id is not None else [])))
            for row in self.memory_intents.list_for_chat(self.identity, clients):
                operation = self.store.lookup(row['client_id'])
                intent = self.material_intents.lookup(self.identity, row['client_id']) if self.material_intents is not None else None
                if (operation is not None and (operation['chat_id'] != self.identity or row['state'] != 'ready'
                        or row['derived_inputs_sha256'] != operation['request_sha256'])
                        or intent is not None and (intent['raw_request_sha256'] != row['raw_request_sha256']
                            or (operation is not None or intent['request_sha256'] is not None)
                                and intent['request_sha256'] != row['derived_inputs_sha256'])):
                    raise _error('memory_metadata_unavailable', 'Claude记忆、材料与原回执元数据不一致，请保留待核对状态。', status=503, unsent=False)
                memories.append(self.memory_intents.public(row))
        self._barrier()
        # Only an idle explicit poll reads original metadata. This never sends
        # a prompt, changes a setting, starts an Agent or resolves an approval.
        with self._action:
            creation = self.store.creation(self.identity)
            if (self._state == 'ready' and self._rpc is not None and self._active is None
                    and (self._binding().get('native_file') is None
                         or creation is not None and creation.get('status') != 'confirmed')):
                try: self._refresh_info()
                except BaseException:
                    self._problem = '原生持久身份暂未核实，原回执保留。'
            binding = self._binding()
            operations = self.store.operations(self.identity)
            with self._cache:
                value = {'schema_version': 1, 'agent': 'claude', 'workspace_id': self.workspace_id,
                    'chat_id': self.identity, 'epoch': self._epoch,
                    'connection': {'state': self._state, 'problem': self._problem},
                    'binding': dict(binding, persisted=binding.get('native_file') is not None),
                    'activity': {'state': 'running' if self._active else 'unknown' if self.store.active_for_chat(self.identity) else 'idle',
                                 'client_id': self._active},
                    'native_records': [deepcopy(row) for row, _ in self._records],
                    'native_records_omitted': self._omitted, 'history': deepcopy(self._history),
                    'requests': [deepcopy(row) for row in self._requests.values()],
                    'operations': operations, 'operation': None}
            if client_id is not None:
                operation = self.store.lookup(client_id)
                if operation is not None and operation['chat_id'] != self.identity:
                    raise _error('request_conflict', '这次请求不属于当前原生对话。')
                value['operation'] = operation
                value['operation_lookup'] = {'client_id': client_id, 'checked': True, 'found': operation is not None}
            if memories or self.memory_intents is not None and callable(self.memory_recall):
                value['memory_intents'] = memories
            if self.profile_intents is not None:
                clients = list(dict.fromkeys([row['client_id'] for row in operations] +
                    ([client_id] if client_id is not None else [])))
                value['profile_intents'] = self.profile_intents.list_for_chat(self.identity, clients)
            return value

    def disconnect(self):
        with self._action:
            self._barrier()
            if (self._active is not None or self.store.active_for_chat(self.identity)
                    or any(row['state'] in ('pending', 'unknown') for row in self._requests.values())):
                raise _error('disconnect_pending', '原生任务、审批或回执尚未结束，请先核对。')
            self.close()
            return {'disconnected': True, 'background_tools_exit_verified': False}

    def close(self):
        with self._action:
            self._stop.set()
            if self._rpc is not None: self._rpc.close()
            if self._reader is not None and self._reader is not threading.current_thread():
                self._reader.join(timeout=3)
                if self._reader.is_alive(): raise RuntimeError('Claude native reader did not stop.')
            self._state = 'closed'


class ClaudeNativeChats:
    def __init__(self, data_dir, catalogue_path, connection, *, workspace_id, admission=None, materials=None,
                 memory_recall=None, memory_query_filter=None):
        self.root = Path(data_dir) / 'claude-native-workspaces'
        self.catalogue = NativeChatCatalogue(catalogue_path, agent='claude')
        self.store = ClaudeNativeStore(Path(data_dir) / 'claude-native.sqlite')
        self.connection, self.workspace_id = connection, workspace_id
        self.materials = materials
        self.material_intents = ClaudeMaterialIntents(catalogue_path, workspace_id=workspace_id) if materials is not None else None
        if self.material_intents is None:
            with sqlite3.connect('file:' + quote(str(catalogue_path), safe='/') + '?mode=ro', uri=True) as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='claude_material_intents'").fetchone()
            if exists: self.material_intents = ClaudeMaterialIntents(catalogue_path, workspace_id=workspace_id)
        self.memory_recall = memory_recall
        # Legacy isolated holders accepted shorter workspace identities. They
        # retain the old no-memory path; real Service workspaces are 64 hex.
        self.memory_intents = NativeMemoryIntents(catalogue_path, workspace_id=workspace_id,
            request_kind='claude', query_filter=memory_query_filter) if len(workspace_id) == 64 or callable(memory_recall) else None
        self.memory_selections = NativeMemorySelections(catalogue_path,
            workspace_id=workspace_id) if len(workspace_id) == 64 else None
        self.profiles_enabled = len(workspace_id) == 64
        self.profiles = AIProfiles(catalogue_path) if self.profiles_enabled else None
        self.profile_intents = NativeProfileIntents(catalogue_path, workspace_id=workspace_id,
            request_kind='claude') if self.profiles_enabled else None
        self._catalogue_lock, self._model_catalogue = threading.Lock(), None
        self.admission = admission
        self.adopted_open_checker = None
        self._lock, self._owners = threading.RLock(), {}
        self._closing = self._closed = False

    def _available(self):
        if self._closing or self._closed:
            raise _error('closing', 'Claude原生入口正在收尾，请稍后核对原回执。', status=503)

    def _prepare(self, identity):
        if self.store.binding(identity) is not None:
            return  # Preserve an adopted original cwd and file anchor.
        cwd = self.root / chat_id(identity)
        if self.root.is_symlink() or cwd.is_symlink(): raise ValueError('Claude workspace is not an owned directory.')
        self.root.mkdir(mode=0o700, exist_ok=True); cwd.mkdir(mode=0o700, exist_ok=True)
        self.store.bind(identity, cwd)

    def _owner(self, identity):
        with self._lock:
            self._available(); self.catalogue.get(identity)
            if identity not in self._owners:
                self._prepare(identity)
                self._owners[identity] = ClaudeNativeHolder(self.store, identity, self.connection,
                    workspace_id=self.workspace_id, materials=self.materials, material_intents=self.material_intents,
                    memory_intents=self.memory_intents, memory_recall=self.memory_recall,
                    memory_selections=self.memory_selections, profile_intents=self.profile_intents,
                    catalogue_observer=self._remember_catalogue)
            return self._owners[identity]

    def catalogue_snapshot(self):
        with self._catalogue_lock:
            return deepcopy(self._model_catalogue)

    def _remember_catalogue(self, identity, catalogue):
        snapshot = {'workspace_id': self.workspace_id, 'source_chat_id': identity,
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'catalogue': normalize_claude_catalogue(catalogue)}
        with self._catalogue_lock:
            self._model_catalogue = snapshot
        return True

    def sources(self):
        return [('claude', row['session_id']) for row in self.store.bindings() if row.get('native_file') is not None]

    def maintenance_state(self):
        return observe_maintenance(self, 'claude')

    @admitted_native
    def dispatch(self, method, path, *, query='', body=None):
        try:
            return self._dispatch(method, path, query=query, body=body)
        except (SpecialistHTTPError, ClaudeMaterialIntentError, NativeMaterialError, ClaudeMessageViewError,
                ClaudeMaterialProtocolError, NativeMemoryIntentError, NativeProfileIntentError,
                NativeProfileUnavailable) as error:
            if isinstance(error, SpecialistHTTPError):
                failure = error
            elif isinstance(error, NativeMemoryIntentError):
                failure = _error('memory_' + error.code, str(error),
                    status=503 if error.code == 'invalid_record' else 409,
                    unsent=method != 'GET' and error.code != 'invalid_record')
            elif isinstance(error, (NativeProfileIntentError, NativeProfileUnavailable)):
                code = getattr(error, 'code', 'unavailable')
                failure = _error('profile_' + code, str(error), status=503 if code == 'invalid_record' else 400 if code == 'invalid_request' else 409,
                    unsent=method != 'GET' and code != 'invalid_record')
            elif isinstance(error, ClaudeMaterialIntentError) and error.code == 'invalid_record':
                failure = _error('material_metadata_unavailable', str(error), status=503, unsent=False)
            else:
                status = 400 if getattr(error, 'code', '') in ('invalid_request', 'invalid_reference', 'invalid_cursor') else 409
                failure = _error('material_' + getattr(error, 'code', 'unverified'), str(error), status=status, unsent=method != 'GET')
            # A malformed replay cannot turn an existing durable submission into
            # accepted:false. Reading this metadata never opens an owner/source.
            if method == 'POST' and path.endswith('/send') and type(body) is dict:
                try:
                    client = _uuid(body.get('client_id'))
                except SpecialistHTTPError:
                    client = None
                    old = None  # Invalid UUIDs cannot identify a durable row.
                else:
                    try: old = self.store.lookup(client)
                    except Exception: old = True
                if old is not None:
                    raise SpecialistHTTPError(failure.status, failure.code, str(failure), accepted=None) from None
                if self.memory_intents is not None and client is not None:
                    try:
                        memory = self.memory_intents.read(path.split('/')[-2], client)
                    except Exception:
                        memory = True
                    if memory is True or memory is not None and memory['state'] != 'failed':
                        raise SpecialistHTTPError(failure.status, failure.code, str(failure),
                            result_unknown=failure.result_unknown, accepted=None) from None
            raise failure from None

    def _dispatch(self, method, path, *, query='', body=None):
        with self._lock: self._available()
        if path == ROOT:
            if query: raise _error('invalid_query', '原生目录不接受原生参数。', status=400)
            if method == 'GET':
                return {'workspace_id': self.workspace_id, 'chats': self.catalogue.list(), 'max_connections': MAX_CONNECTIONS,
                        'material_inputs': {'enabled': self.materials is not None, 'max_images': 4, 'max_files': 4}}
            if method != 'POST': raise _error('method', '请使用原生对话页面的固定操作。', status=405)
            fields = _fields(body, ('client_id', 'title'))
            return self.catalogue.create(fields['client_id'], fields['title'], self._prepare)
        parts = path.removeprefix(ROOT + '/').split('/') if path.startswith(ROOT + '/') else []
        if len(parts) not in (1, 2): raise _error('route', '没有这个Claude原生操作。', status=404)
        identity = chat_id(parts[0]); action = parts[1] if len(parts) == 2 else None
        client = None
        if action in ('receipt', 'history', 'message', 'image'):
            if method != 'GET' or body is not None:
                raise _error('receipt_invalid', 'Claude精确读取只接受无请求体的GET。', status=400, unsent=False)
            if action == 'receipt':
                args = _read_query(query, ('client_id',))
                try: client = _uuid(args['client_id'])
                except SpecialistHTTPError:
                    raise _error('receipt_invalid', '请提供一个完整Claude发送编号。', status=400, unsent=False) from None
                self.catalogue.get(identity)
                binding = self.store.binding(identity)
                operation = self.store.lookup(client)
                if operation is not None and operation['chat_id'] != identity:
                    raise _error('request_conflict', '原回执不属于这段Claude对话。', unsent=False)
                intent = self.material_intents.lookup(identity, client) if self.material_intents is not None else None
                if intent is not None and operation is not None and intent['request_sha256'] != operation['request_sha256']:
                    raise _error('request_conflict', 'Claude材料意图与原回执不一致。', unsent=False)
                memory = self.memory_intents.public(self.memory_intents.read(identity, client)) if self.memory_intents is not None else None
                if memory is not None and operation is not None and memory['derived_inputs_sha256'] != operation['request_sha256']:
                    raise _error('memory_request_conflict', 'Claude共享记忆与原回执不一致。', unsent=False)
                return {'workspace_id': self.workspace_id, 'chat_id': identity,
                        'session_id': binding['session_id'], 'operation': operation, 'material_intent': intent,
                        **({'profile_intent': next(iter(self.profile_intents.list_for_chat(identity, [client])), None)}
                            if self.profile_intents is not None else {}),
                        **({'memory_intent': memory} if memory is not None else {})}
            fields = ('offset',) if action == 'history' else ('offset', 'uuid', 'sha256') + (('part_index',) if action == 'image' else ())
            args = _read_query(query, fields, defaults={'offset': '0'} if action == 'history' else None)
            offset = _index(args['offset'])
            if action != 'history':
                if (len(args['sha256']) != 64 or any(c not in '0123456789abcdef' for c in args['sha256'])
                        or args['uuid'] != '' and (len(args['uuid']) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in args['uuid']))):
                    raise _error('record_reference_invalid', 'Claude SDK完整记录引用无效。', status=400, unsent=False)
            part_index = _index(args['part_index']) if action == 'image' else None
            if part_index is not None and part_index > 1023:
                raise _error('record_reference_invalid', 'Claude原图块位置无效。', status=400, unsent=False)
            with self._lock:
                self._available(); self.catalogue.get(identity)
                owner = self._owners.get(identity)
            if owner is None:
                raise _error('history_not_open', '请先明确连接这段Claude原生对话。', unsent=False)
            with owner._action:
                with self._lock:
                    self._available()
                    if self._owners.get(identity) is not owner:
                        raise _error('owner_changed', 'Claude连接已变化，请刷新后读取。', unsent=False)
                if action == 'history': return owner.read_history(offset)
                return owner.read_history_record(offset, args['uuid'] or None, args['sha256'], part_index=part_index)
        if action == 'profiles':
            if method != 'GET' or body is not None or query:
                raise _error('profiles_invalid', 'Claude模型目录只接受无参数GET。', status=400)
            if not self.profiles_enabled:
                raise _error('profiles_unavailable', '这条Claude入口尚未启用原生模型配置。')
            with self._lock:
                self._available(); self.catalogue.get(identity)
                owner = self._owners.get(identity)
            if owner is None:
                raise _error('models_not_open', '请先明确连接这段Claude原生对话。')
            with owner._action:
                with self._lock:
                    self._available()
                    if self._owners.get(identity) is not owner:
                        raise _error('owner_changed', 'Claude连接已变化，请重新核对。')
                catalogue = owner.models()
                return {'workspace_id': self.workspace_id, 'chat_id': identity,
                    'native_catalog_ready': True,
                    'profiles': project_claude_profiles(self.profiles.list()['profiles'], catalogue)}
        if action is None:
            if method != 'GET' or body is not None: raise _error('method', '请使用原生记录读取入口。', status=405)
            parsed = parse_qs(query, keep_blank_values=True, strict_parsing=True) if query else {}
            if set(parsed) - {'client_id'} or any(len(v) != 1 for v in parsed.values()):
                raise _error('invalid_query', '只可核对一个原生请求编号。', status=400)
            if parsed: client = _uuid(parsed['client_id'][0])
        else:
            required = {'open': (), 'send': ('client_id', 'message'), 'answer': ('token', 'epoch', 'result'),
                        'interrupt': ('client_id',), 'disconnect': ()}
            if action not in required: raise _error('route', '没有这个Claude原生操作。', status=404)
            if method != 'POST' or query: raise _error('method', '请使用原生对话页面的固定操作。', status=405)
            fields = _fields(body, required[action], ('material_refs', 'memory_profile_id', 'memory_profile_revision',
                'ai_profile_id', 'ai_profile_revision') if action == 'send' else ())
            if action == 'send':
                _uuid(fields['client_id']); _message(fields['message'])
                validate_selection(fields)
                NativeProfileIntents._request_identity(fields, 'claude')
                if 'material_refs' in fields:
                    if not validate_refs(fields['material_refs']):
                        raise _error('invalid_materials', '没有所选材料时请使用普通文字入口。', status=400)
                self.catalogue.get(identity)
                replay = _recover_send(self.store, self.material_intents, self.memory_intents, identity, fields, self.profile_intents)
                if replay is not None: return replay
            if action == 'interrupt': _uuid(fields['client_id'])
        owner = self._owner(identity)
        with owner._action:
            if action == 'open' and owner._rpc is None:
                binding = owner._binding()
                from claude_history_adoption import is_adopted_binding
                if is_adopted_binding(self.workspace_id, identity, binding['session_id']):
                    if not callable(self.adopted_open_checker):
                        raise _error('history_unverified', '原对话来源尚未核实，未启动原生连接。')
                    # Slow file/SDK path checks hold only this owner's lock,
                    # before reserving a connection or launching a process.
                    self.adopted_open_checker(identity)
            with self._lock:
                self._available()
                if self._owners.get(identity) is not owner: raise _error('owner_changed', '连接已变化，请刷新后核对。')
                if action == 'open' and not owner._slot_reserved:
                    if sum(o._slot_reserved for o in self._owners.values()) >= MAX_CONNECTIONS:
                        raise _error('connection_limit', '当前最多连接一段Claude原生对话，请先断开空闲对话。')
                    # Reserve before releasing the registry lock. Slow process
                    # startup holds only its owner lock; no second chat slips
                    # through the interval before _rpc has been assigned.
                    owner._slot_reserved = True
            if action is None: return owner.view(client)
            if action == 'open': return owner.open()
            if action == 'send': return owner.send(fields['client_id'], fields['message'],
                **{key: fields[key] for key in ('material_refs', 'memory_profile_id', 'memory_profile_revision',
                    'ai_profile_id', 'ai_profile_revision') if key in fields})
            if action == 'answer': return owner.answer(fields['token'], fields['epoch'], fields['result'])
            if action == 'interrupt': return owner.interrupt(fields['client_id'])
            result = owner.disconnect()
            with self._lock: self._owners.pop(identity)
            return result

    def close(self):
        with self._lock:
            if self._closed: return
            self._closing = True; owners = list(self._owners.items())
        for identity, owner in owners:
            owner.close()
            with self._lock: self._owners.pop(identity, None)
        self.store.close()
        if self.material_intents is not None: self.material_intents.close()
        with self._lock: self._closed = True
