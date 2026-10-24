"""Ordinary Codex chat identities and lazy native owners.

The catalogue owns names only, in the application's existing private database.
Codex owns transcripts; NativeSessionStore owns bindings and submission receipts.
HTTP can select an app-created ID, never a cwd, role, session key or RPC method.
Switching pages never disconnects an owner. There is no automatic eviction.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import threading
from urllib.parse import parse_qs

from rabbit_runtime.native_session_store import NativeSessionStore, NativeSessionStoreError, request_sha256
from rabbit_runtime.codex_native_session import build_turn_params, NativeSessionError
from native_profile_intents import NativeProfileIntents, NativeProfileIntentError
from native_memory_intents import NativeMemoryIntents, NativeMemoryIntentError, validate_selection
from native_profile_catalog import (NativeProfileUnavailable, resolve_native_profile,
                                    project_native_profiles, require_image_support)
from native_materials import NativeMaterialError, validate_refs
from profiles import AIProfiles, validate_profile_id, validate_profile_revision
from memory_context import selected_memory_block
from specialist_http import (SpecialistHTTP, SpecialistHTTPError, ROOT as HOLDER_ROOT,
                             validate_fields, validate_request)

from native_catalogue import NativeChatCatalogue, chat_id, _title, MAX_CHATS
from native_maintenance import maintenance_state as observe_maintenance
from service_admission import admitted_native

ROOT = '/api/native-chats'
MAX_CONNECTIONS = 2
_ACTIONS = frozenset(('open', 'send', 'answer', 'control', 'disconnect', 'profiles', 'receipt'))
_ANY_CATALOGUE = object()


def _error(status, code, message, *, unsent=False):
    return SpecialistHTTPError(status, code, message, accepted=False if unsent else None)


def _unsubmitted_memory(record):
    if record['state'] == 'failed':
        raise _error(409, 'native_chat_memory_prepare_failed',
            '原共享记忆准备未完成，未进入原生提交；草稿保留，没有重试。', unsent=True)
    raise SpecialistHTTPError(409, 'native_chat_memory_submission_unconfirmed',
        '这个发送编号已有共享记忆准备记录，尚未查到原生回执；请核对原发送，没有重新检索或发送。',
        result_unknown=True)


def session_key(identity):
    return 'chat/codex/' + chat_id(identity)


@dataclass
class _Owner:
    holder: object
    lock: object = field(default_factory=threading.RLock)
    reserved: bool = False


class NativeChats:
    """One lazy holder/reader per logical chat; the Service owns shutdown."""
    def __init__(self, data_dir, catalogue_path, factory, *, workspace_id, admission=None, materials=None,
                 memory_recall=None, memory_query_filter=None, adopted_open_checker=None):
        if not callable(factory):
            raise ValueError('原生对话构造入口无效。')
        self.root = Path(data_dir) / 'native-workspaces'
        self.catalogue = NativeChatCatalogue(catalogue_path)
        self.profiles = AIProfiles(catalogue_path)
        self.profile_intents = NativeProfileIntents(catalogue_path, workspace_id=workspace_id)
        self.memory_intents = NativeMemoryIntents(catalogue_path, workspace_id=workspace_id,
                                                 query_filter=memory_query_filter)
        self.store = NativeSessionStore(Path(data_dir) / 'native-chats.sqlite')
        self.factory, self.workspace_id = factory, workspace_id
        self.admission = admission
        self.materials = materials
        self.memory_recall = memory_recall
        self.adopted_open_checker = adopted_open_checker
        self._lock = threading.RLock()
        self._owners = {}
        # Profile validation can read this RAM snapshot while owning the shared
        # SQLite writer. The registry lock can itself wait for that writer, so
        # cache copy/replace must use an independent lock with no I/O inside.
        self._catalogue_lock = threading.Lock()
        self._model_catalogue = None
        self._closing = False
        self._closed = False

    def _available(self):
        if self._closing or self._closed:
            raise _error(503, 'native_chats_closing', '原生对话入口正在收尾，请稍后核对原回执。', unsent=True)

    def _prepare_workspace(self, identity):
        key, cwd = session_key(identity), self.root / identity
        binding = self.store.read_binding(key)
        if binding is not None:
            # Adopted histories keep their verified original cwd. A directory
            # that vanished or changed is never repaired or moved on reopen.
            return Path(self.store.verify_scope(key)['cwd'])
        if self.root.is_symlink() or cwd.is_symlink():
            raise ValueError('原生对话工作目录不可用。')
        self.root.mkdir(mode=0o700, exist_ok=True)
        cwd.mkdir(mode=0o700, exist_ok=True)
        self.store.bind(key, cwd)
        return cwd

    def sources(self):
        # Metadata reads only; no holder construction, open, polling or RPC.
        with self._lock:
            result = []
            for row in self.catalogue.list():
                binding = self.store.read_binding(session_key(row['id']))
                if binding is not None and binding['thread_id'] is not None:
                    result.append(('codex', binding['thread_id']))
            return result

    def catalogue_snapshot(self):
        """Last complete discovery only; no owner construction or native RPC.

        This RAM observation may outlive its source connection. It describes
        candidates observed from a native connection, never current entitlement or
        the options actually used by a later turn. New sends still rediscover.
        """
        with self._catalogue_lock:
            return deepcopy(self._model_catalogue)

    def _remember_catalogue(self, identity, catalogue, *, expected=_ANY_CATALOGUE):
        snapshot = {'workspace_id': self.workspace_id, 'catalogue': deepcopy(catalogue), 'source_chat_id': identity,
                    'observed_at': datetime.now(timezone.utc).isoformat()}
        with self._catalogue_lock:
            # An independent reader must not replace an observation published
            # by an ordinary chat while that reader's RPC was in flight.
            if expected is not _ANY_CATALOGUE and self._model_catalogue != expected:
                return False
            self._model_catalogue = snapshot
            return True

    def _owner(self, identity):
        with self._lock:
            self._available()
            row = self.catalogue.get(identity)
            owner = self._owners.get(row['id'])
            if owner is None:
                cwd = self._prepare_workspace(row['id'])
                holder = self.factory(self.store, cwd=cwd, session_key=session_key(row['id']))
                if holder is None:
                    raise ValueError('原生对话构造入口未返回持有者。')
                owner = self._owners[row['id']] = _Owner(holder)
            return owner

    def maintenance_state(self):
        return observe_maintenance(self, 'codex')

    @admitted_native
    def dispatch(self, method, path, *, query='', body=None):
        with self._lock:
            self._available()
        if path == ROOT:
            if query:
                raise _error(400, 'native_chat_invalid_query', '对话目录不接受分页或原生参数。', unsent=True)
            if method == 'GET':
                return {'workspace_id': self.workspace_id, 'chats': self.catalogue.list(),
                        'max_connections': MAX_CONNECTIONS,
                        'material_inputs': {'enabled': self.materials is not None,
                                            'max_images': 4, 'max_files': 4}}
            if method != 'POST':
                raise _error(405, 'native_chat_method', '请使用原生对话页面的操作。', unsent=True)
            fields = {k: v for k, v in body.items() if k != '_one_ai_csrf'} if type(body) is dict else body
            validate_fields(fields, ('client_id', 'title'))
            with self._lock:
                self._available()
                return self.catalogue.create(fields['client_id'], fields['title'], self._prepare_workspace)
        parts = path.removeprefix(ROOT + '/').split('/') if path.startswith(ROOT + '/') else []
        if len(parts) not in (1, 2) or len(parts) == 2 and parts[1] not in _ACTIONS:
            raise _error(404, 'native_chat_route', '找不到这个原生对话操作。', unsent=True)
        identity = chat_id(parts[0])
        action = parts[1] if len(parts) == 2 else None
        if action == 'receipt':
            # Absence is not an unsent proof: another HTTP handler may still be
            # preparing this client. This read never reconciles or creates an
            # owner, and returns only an exact app-created chat's receipt.
            if method != 'GET' or body is not None or type(query) is not str or len(query) > 1024:
                raise _error(400, 'native_chat_receipt_invalid', '请使用原发送编号查询回执。')
            values = parse_qs(query, keep_blank_values=True)
            if set(values) != {'client_id'} or len(values['client_id']) != 1:
                raise _error(400, 'native_chat_receipt_invalid', '请使用一个完整的原发送编号。')
            client = values['client_id'][0]
            # Reuse the finite client-ID codec without calling any holder.
            validate_request('POST', HOLDER_ROOT + '/send', body={
                'client_id': client, 'inputs': [{'type': 'text', 'text': 'receipt identity validation'}]})
            self.catalogue.get(identity)
            receipt = self.store.read_operation(client, session_key=session_key(identity))
            intents = self.profile_intents.list_for_chat(identity, [client])
            return {'workspace_id': self.workspace_id, 'chat_id': identity,
                    'receipt': receipt, 'profile_intent': intents[0] if intents else None,
                    'memory_intent': self.memory_intents.public(self.memory_intents.read(identity, client))}
        # Reuse the original validator before constructing or selecting an
        # owner; no malformed method/body may start or reserve a process.
        mapped = HOLDER_ROOT + ('/' + action if action else '')
        send_fields = None
        if action == 'profiles':
            if method != 'GET' or query or body is not None:
                raise _error(405, 'native_chat_profile_method', '请使用原生对话的配置列表。', unsent=True)
        elif action == 'disconnect':
            if method != 'POST' or query:
                raise _error(405, 'native_chat_method', '请使用断开连接操作。', unsent=True)
            fields = {k: v for k, v in body.items() if k != '_one_ai_csrf'} if type(body) is dict else body
            validate_fields(fields, ())
        elif action == 'send':
            send_fields = {k: v for k, v in body.items() if k != '_one_ai_csrf'} if type(body) is dict else body
            validate_fields(send_fields, ('client_id', 'inputs'),
                            ('ai_profile_id', 'ai_profile_revision', 'material_refs',
                             'memory_profile_id', 'memory_profile_revision'))
            try:
                memory_selected = validate_selection(send_fields) is not None
            except (NativeMemoryIntentError, ValueError):
                raise _error(400, 'native_chat_memory_invalid', '共享记忆选择或配置版本无效，尚未发送。', unsent=True) from None
            try:
                refs = validate_refs(send_fields.get('material_refs', []))
            except NativeMaterialError as error:
                raise _error(400, 'native_chat_material_invalid', str(error), unsent=True) from None
            if refs and self.materials is None:
                raise _error(409, 'native_chat_material_disabled', '本机原生材料接力尚未启用；草稿保留。', unsent=True)
            selected = 'ai_profile_id' in send_fields
            if selected != ('ai_profile_revision' in send_fields):
                raise _error(400, 'native_chat_profile_invalid', 'AI 配置编号与版本需要一起提交。', unsent=True)
            if selected:
                try:
                    validate_profile_id(send_fields['ai_profile_id'])
                    validate_profile_revision(send_fields['ai_profile_revision'])
                except ValueError:
                    raise _error(400, 'native_chat_profile_invalid', 'AI 配置编号或版本无效。', unsent=True) from None
            body = {key: send_fields[key] for key in ('client_id', 'inputs')}
            validate_request(method, mapped, query=query, body=body)
            if any(part.get('type') in ('image', 'localImage') for part in send_fields['inputs']):
                raise _error(400, 'native_chat_raw_image_rejected',
                    '原图请通过材料选择随附，不能从网页提交图片路径或链接。', unsent=True)
            if refs and any(part.get('type') != 'text' or type(part.get('text')) is not str
                            or part.keys() - {'type', 'text', 'text_elements'} for part in send_fields['inputs']):
                raise _error(400, 'native_chat_material_base_invalid',
                    '材料请求的草稿需要完整文字；没有交接额外的未知输入。', unsent=True)
            if memory_selected and any(part.get('type') != 'text' or type(part.get('text')) is not str
                                       or part.keys() - {'type', 'text', 'text_elements'} for part in send_fields['inputs']):
                raise _error(400, 'native_chat_memory_input_invalid',
                    '共享记忆只检索本轮文字问题；其他材料请用材料选择随附，尚未发送。', unsent=True)
        else:
            validate_request(method, mapped, query=query, body=body)
        owner = self._owner(identity)
        verified_binding = None
        if action == 'open':
            from codex_history_adoption import is_adopted_binding
            binding = self.store.read_binding(session_key(identity))
            if binding and is_adopted_binding(self.workspace_id, identity, binding['thread_id']):
                if not callable(self.adopted_open_checker):
                    raise _error(409, 'native_history_unverified',
                        '原会话恢复前还需要核对来源与扩展工具，尚未连接。', unsent=True)
                self.adopted_open_checker(identity)
                verified_binding = binding
        with owner.lock:
            if verified_binding is not None:
                current = self.store.verify_scope(session_key(identity))
                if any(current[key] != verified_binding[key] for key in (
                        'thread_id','session_id','cwd','cwd_device','cwd_inode')):
                    raise _error(409, 'native_history_binding_changed',
                        '原会话绑定已变化，请重新核对；尚未连接。', unsent=True)
            with self._lock:
                self._available()
                # A concurrent completed disconnect may have removed this
                # owner while this request waited. Never operate its old slot.
                if self._owners.get(identity) is not owner:
                    raise _error(409, 'native_chat_owner_changed', '连接已变化，请刷新后核对。', unsent=True)
                if action == 'open' and not owner.reserved:
                    if sum(item.reserved for item in self._owners.values()) >= MAX_CONNECTIONS:
                        raise _error(409, 'native_chat_connection_limit', '当前最多连接两段原生对话，请先断开一段已结束的对话。', unsent=True)
                    owner.reserved = True
            if action == 'disconnect':
                return self._disconnect(identity, owner)
            if action == 'profiles':
                catalogue = SpecialistHTTP(owner.holder)._call(owner.holder.models)
                self._remember_catalogue(identity, catalogue)
                return {'workspace_id': self.workspace_id, 'chat_id': identity,
                        'native_catalog_ready': True,
                        'profiles': project_native_profiles(self.profiles.list()['profiles'], catalogue)}
            intent = memory_intent = None
            if action == 'send':
                # The raw application request (including reference hashes) is
                # separate from the derived native input. Once a native receipt
                # exists, matching the frozen raw intent is sufficient for a
                # read-only replay: never re-read, rediscover or resend it.
                prior = self.store.read_operation(send_fields['client_id'], session_key=session_key(identity))
                previous_memory = self.memory_intents.read(identity, send_fields['client_id'])
                if previous_memory is not None and previous_memory['raw_request_sha256'] != request_sha256(send_fields):
                    raise _error(409, 'native_chat_memory_receipt_conflict',
                        '这个发送编号已用于不同的共享记忆请求，请核对原回执；没有重新发送。')
                if prior is not None:
                    try:
                        intent = self.profile_intents.lookup(identity, send_fields)
                        memory_intent = self.memory_intents.lookup(identity, send_fields) if memory_selected else None
                    except NativeProfileIntentError as error:
                        raise _error(409, error.code, str(error)) from None
                    except NativeMemoryIntentError:
                        raise _error(409, 'native_chat_memory_receipt_conflict',
                            '原记忆准备记录与发送编号不一致，请核对原回执；没有重新检索或发送。') from None
                    if prior['kind'] != 'turn_start' or intent is None and (refs or memory_selected or previous_memory is not None):
                        raise _error(409, 'native_chat_material_receipt_conflict',
                            '原回执缺少匹配的请求身份，请核对原消息；没有重新发送。')
                    if memory_selected and (memory_intent is None or memory_intent['state'] != 'ready'
                                            or not memory_intent['derived_inputs_sha256']):
                        raise _error(409, 'native_chat_memory_receipt_conflict',
                            '原回执缺少完整的记忆准备证据，请核对原消息；没有重新检索或发送。')
                    if intent is not None:
                        return {'receipt': prior, 'reused': True, 'profile_intent': intent,
                            'memory_intent': self.memory_intents.public(memory_intent),
                            'background_tools_exit_verified': False}
                    # Early plain-native receipts predate profile intents. Do
                    # not mint missing configuration evidence or consult a new
                    # next-turn choice while inspecting an existing operation.
                    if selected:
                        raise _error(409, 'native_chat_profile_receipt_conflict',
                            '原回执没有这份配置意图，请只读核对原发送；没有重新发送。')
                    try:
                        binding = self.store.read_binding(session_key(identity))
                        matching = (binding is not None and type(binding['thread_id']) is str
                            and request_sha256(build_turn_params(binding['thread_id'],
                                send_fields['client_id'], send_fields['inputs'])) == prior['request_sha256'])
                    except (NativeSessionStoreError, NativeSessionError, ValueError, TypeError):
                        matching = False
                    if not matching:
                        raise _error(409, 'native_chat_receipt_conflict',
                            '原回执与这份输入无法匹配，请只读核对原发送；没有重新发送。')
                    return {'receipt': prior, 'reused': True, 'profile_intent': None,
                            'memory_intent': None, 'background_tools_exit_verified': False}
                if previous_memory is not None:
                    _unsubmitted_memory(previous_memory)
                catalogue = None
                def resolve(profile):
                    nonlocal catalogue
                    catalogue = SpecialistHTTP(owner.holder)._call(owner.holder.models)
                    self._remember_catalogue(identity, catalogue)
                    return resolve_native_profile(profile, catalogue)
                try:
                    if memory_selected:
                        if not callable(self.memory_recall):
                            raise _error(409, 'native_chat_memory_disabled',
                                '这台入口尚未启用原生共享记忆，尚未发送。', unsent=True)
                        # Do not recall sensitive sources or reserve a new
                        # preparation before the user has opened this owner.
                        # Recovery above remains available while disconnected.
                        SpecialistHTTP(owner.holder)._call(owner.holder.require_ready)
                    intent = self.profile_intents.freeze(identity, send_fields, resolve)
                    if memory_selected:
                        memory_intent = self.memory_intents.prepare(identity, send_fields, self.memory_recall)
                        if not memory_intent.get('created'):
                            _unsubmitted_memory(memory_intent)
                        block = selected_memory_block(memory_intent['memory_inputs'], memory_intent['memory_profile'])
                        body = {'client_id': send_fields['client_id'],
                                'inputs': [{'type': 'text', 'text': block}] + deepcopy(send_fields['inputs'])}
                    if any(ref['kind'] == 'image' for ref in refs):
                        if catalogue is None:
                            catalogue = SpecialistHTTP(owner.holder)._call(owner.holder.models)
                            self._remember_catalogue(identity, catalogue)
                        require_image_support(catalogue, intent['native_options'])
                    if refs:
                        prepared = self.materials.prepare(body['inputs'], refs)
                        body = {'client_id': send_fields['client_id'], 'inputs': prepared['inputs']}
                    if memory_selected:
                        # The raw body, frozen recall and derived native input
                        # have separate identities. No input/body is re-created
                        # on an existing native receipt's recovery path.
                        validate_request(method, mapped, query=query, body=body)
                        memory_intent = self.memory_intents.bind_inputs(identity, send_fields, body['inputs'])
                except NativeProfileIntentError as error:
                    raise _error(409, error.code, str(error), unsent=True) from None
                except NativeProfileUnavailable as error:
                    raise _error(409, 'native_chat_profile_unavailable', str(error), unsent=True) from None
                except NativeMaterialError as error:
                    raise _error(409, 'native_chat_material_unavailable', str(error), unsent=True) from None
                except NativeMemoryIntentError:
                    raise _error(409, 'native_chat_memory_prepare_failed',
                        '共享记忆准备失败或配置已变化，尚未发送；原草稿保留。', unsent=True) from None
                except SpecialistHTTPError as error:
                    # Discovery/preparation precedes this call's turn/start.
                    # An older preparation without a receipt stays unknown.
                    if not error.result_unknown:
                        error.accepted = False
                    raise
            result = SpecialistHTTP(owner.holder, send_options=intent['native_options'] if intent else None).dispatch(
                method, mapped, query=query, body=body)
            if intent is not None:
                result = dict(result, profile_intent=intent)
                if memory_selected:
                    result['memory_intent'] = self.memory_intents.public(memory_intent)
            elif action is None:
                result = dict(result, profile_intents=self.profile_intents.list_for_chat(
                    identity, [row['client_id'] for row in result.get('receipts', [])]))
                result['memory_intents'] = [self.memory_intents.public(record) for record in
                    self.memory_intents.list_for_chat(identity, [row['client_id'] for row in result.get('receipts', [])])]
            return result

    def _disconnect(self, identity, owner):
        # An explicit disconnect does not cancel an unknown submission, grant
        # approval, archive a thread, or claim background tools have exited.
        view = SpecialistHTTP(owner.holder).dispatch('GET', HOLDER_ROOT, query='include_items=0')
        active = view.get('turn_state') or {}
        pending = self.store.pending_operations(session_key(identity))
        ready = view.get('connection', {}).get('state') == 'ready'
        native_status = ((view.get('native') or {}).get('thread') or {}).get('status') or {}
        bound = (view.get('binding') or {}).get('thread_id') is not None
        if (view.get('requests') or active.get('status') not in (None, 'completed', 'failed', 'interrupted')
                or pending or ready and (view.get('requests_available') is not True
                                         or bound and native_status.get('type') != 'idle')):
            raise _error(409, 'native_chat_disconnect_pending', '本轮或回执仍待确认，请先结束并核对再断开。', unsent=True)
        try:
            owner.holder.close()
        except BaseException:
            raise _error(503, 'native_chat_close_unconfirmed', '连接退出尚未确认，仍保留记录库与连接名额。') from None
        with self._lock:
            self._owners.pop(identity)
        return {'disconnected': True, 'background_tools_exit_verified': False}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closing = True
            entries = list(self._owners.items())
        errors = []
        for identity, owner in entries:
            with owner.lock:
                try:
                    owner.holder.close()
                except BaseException as error:
                    errors.append(error)
                else:
                    with self._lock:
                        self._owners.pop(identity, None)
        if errors:
            raise RuntimeError('原生对话执行器仍在收尾，记录库与服务独占锁保留。') from None
        self.store.close()
        with self._lock:
            self._closed = True
