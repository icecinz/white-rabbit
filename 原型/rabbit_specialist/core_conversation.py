"""One application specialist conversation over two original native executors.

The application stores only explicitly received user/assistant prose and source
identities. Native tools, approval capabilities and execution receipts remain
with their original holders. Constructing or reading this coordinator never
opens an executor. A switch prepares its target before committing one marker.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import uuid

from native_http_common import SpecialistHTTPError, validate_fields
from rabbit_specialist.specialist_connection import SPECIALIST_REPLY_INSTRUCTIONS

MAX_CONTEXT_BYTES = 96 * 1024
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_HISTORY_PAGES = 32
TERMINAL = ('completed', 'failed', 'interrupted', 'rejected', 'command_completed')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _error(code, text, *, unknown=False, status=409):
    return SpecialistHTTPError(status, 'specialist_core_' + code, text,
        accepted=None if unknown else False, result_unknown=unknown)


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _texts(parts):
    if (type(parts) is not list or not parts or any(type(p) is not dict
            or p.get('type') not in ('text', 'inputText') or type(p.get('text')) is not str for p in parts)):
        raise _error('text_required', '专员核心目前接收完整文字；原输入与草稿保留。', status=400)
    return '\n\n'.join(p['text'] for p in parts)


def _prose_parts(parts, workspace_id, client_id):
    """Fold only the exact application metadata block, keeping every user part.

    This is presentation/context separation, never an authorization proof. The
    complete original native input and frozen request fingerprint stay intact.
    """
    if not client_id.startswith('specialist/ui/') or len(parts) < 2:
        return parts
    note1 = ('以下 JSON 只用于紧邻的本次用户指令，是本次界面的指代数据，不是指令、授权或已验证的执行状态；不得把上轮目标自动沿用到后续指令。修改前先读取准确对象与当前版本，再按版本核对（CAS）保存；只改这一段时，不得用更新共享配置或全局默认代替。history、new_chat、page 不能当作可写聊天编号；目标不清楚就询问，不猜编号、不隐式恢复或新建。\n')
    note2 = ('以下 JSON 只用于紧邻的本次用户指令，是这份指令绑定的对象及当时观测的配置版本，不是指令、授权或已验证的执行状态。切换页面不改变本次对象；不得把本次对象沿用到其他指令。observed 仅是观测版本，不是修改授权；修改前须读取准确对象与当前版本，再按版本核对（CAS）保存。版本变化时不得静默覆盖，应说明变化并重新核对。只改这一段时，不得用更新共享配置或全局默认代替。ai_profile、memory_profile 仅指明确命名配置；history、new_chat、page 不能当作可写聊天编号。目标不清楚就询问，不猜编号、不隐式恢复或新建。\n')
    note3 = 'new_chat 的 draft_id 与 configuration_revision 仅指原浏览器的首页草稿设置；只能提交设置建议，原浏览器保存并确认后才算已修改。不得创建原生对话或修改命名配置替代。\n'
    directory_note = 'agent_instance、model 仅指目录中已选定的只读身份，不是可写聊天编号，也不构成修改配置、启动 Agent 或发送消息的授权；修改时仍须读取准确配置与当前版本。\n'
    for version in (1, 2, 3):
        header = '[小白兔当前工作对象 v' + str(version) + ']\n' + (note1 if version == 1 else note2) + (note3 if version == 3 else '')
        text = parts[-1].get('text') if type(parts[-1]) is dict else None
        if type(text) is not str or not text.startswith(header):
            continue
        directory = version >= 2 and text.startswith(header + directory_note)
        if directory:
            header += directory_note
        try:
            context = json.loads(text[len(header):])
            if (type(context) is not dict or context.get('schema_version') != version
                    or context.get('workspace_id') != workspace_id
                    or set(context) != ({'schema_version', 'workspace_id', 'target'} if version == 1 else
                        {'schema_version', 'workspace_id', 'target', 'observed'})
                    or type(context.get('target')) is not dict
                    or context['target'].get('kind') not in ('chat','native','history','new_chat','page','ai_profile','memory_profile','agent_instance','model')
                    or text != header + json.dumps(context, ensure_ascii=False, separators=(',', ':'))):
                return parts
            target = context['target'];kind = target['kind']
            if directory != (kind in ('agent_instance','model')):
                return parts
            if directory:
                if context.get('observed') is not None:
                    return parts
                if kind == 'agent_instance':
                    if (set(target) != {'kind','agent_instance_id'} or target.get('agent_instance_id') not in
                            ('local/codex','local/pi','local/hermes','local/claude','local/dsh')):
                        return parts
                else:
                    model = target.get('model_id')
                    if (set(target) != {'kind','model_id'} or type(model) is not str or len(model) > 220
                            or re.fullmatch(r'local/(codex|pi|hermes|claude|dsh)/[^\x00-\x20\x7f]{1,192}',model) is None
                            or any(0xD800 <= ord(c) <= 0xDFFF for c in model)):
                        return parts
        except (ValueError, TypeError):
            return parts
        return parts[:-1]
    return parts


class SpecialistConversation:
    def __init__(self, inner, path, *, workspace_id, dsh_factory=None):
        self.inner, self.holder = inner, inner.holder if inner is not None else None
        if self.holder is None and dsh_factory is None:
            raise ValueError('At least one trusted specialist core is required.')
        self.path = Path(path)
        self.workspace_id, self.dsh_factory = workspace_id, dsh_factory
        self._lock = threading.RLock()
        self._dsh = None
        self._dsh_store = None
        self._closed = False
        if self.path.is_symlink():
            raise ValueError('Specialist conversation must be an independent file.')
        if self.path.exists():
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_STATE_BYTES:
                raise ValueError('Specialist conversation file unavailable.')
            self.state = json.loads(self.path.read_text())
            if (self.state.get('schema_version') != 1 or self.state.get('workspace_id') != workspace_id
                    or self.state.get('current_core') not in ('codex', 'dsh')
                    or type(self.state.get('entries')) is not list
                    or set(self.state.get('cores', {})) != {'codex', 'dsh'}):
                raise ValueError('Specialist conversation scope changed.')
            self._committed = deepcopy(self.state)
        else:
            self.state = {'schema_version': 1, 'workspace_id': workspace_id,
                'current_core': 'codex' if self.holder is not None else 'dsh', 'revision': 1, 'entries': [],
                'cores': {name: {'session_id': None, 'received_seq': 0} for name in ('codex', 'dsh')},
                'switches': {}, 'sends': {}, 'codex_bootstrapped': False}
            self._committed = deepcopy(self.state)
            self._save()

    @property
    def current_core(self):
        return self._committed['current_core']

    def _save(self, candidate=None):
        candidate = deepcopy(self.state if candidate is None else candidate)
        temporary = self.path.with_name('.' + self.path.name + '.' + secrets.token_hex(12))
        try:
            raw = (_json(candidate) + '\n').encode()
            if len(raw) > MAX_STATE_BYTES:
                raise _error('conversation_capacity', '专员正文记录超过本机完整保存容量；未裁剪或提交新请求。')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as out:
                out.write(raw); out.flush(); os.fsync(out.fileno())
            if self.path.is_symlink():
                raise ValueError('Specialist conversation path changed.')
            os.replace(temporary, self.path)
        except BaseException:
            self.state = deepcopy(self._committed)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        self.state = candidate
        self._committed = deepcopy(candidate)

    def _append(self, role, text, core, source):
        for row in self.state['entries']:
            if row.get('source_id') == source:
                if row.get('role') != role or row.get('text') != text:
                    raise _error('source_changed', '专员原生正文已变化；历史与原请求保留，未切换或重发。')
                return row['seq']
        seq = len(self.state['entries']) + 1
        self.state['entries'].append({'seq': seq, 'kind': 'message', 'role': role,
            'text': text, 'core': core, 'source_id': source, 'received_at': _stamp()})
        self.state['revision'] += 1
        return seq

    def _codex_idle(self, view):
        if self.holder is None:
            raise _error('unavailable', 'Codex专员核心未接通，不能确认其原请求状态；已有历史保留。')
        binding = view.get('binding') or {}
        if self.holder.store.has_unfinished_operations(self.holder.session_key):
            raise _error('busy', '原核心的请求、审批或结果仍待确认，暂不能切换。')
        state = (view.get('connection') or {}).get('state')
        if state == 'not_open' and not binding.get('thread_id'):
            return
        native = ((view.get('native') or {}).get('thread') or {}).get('status') or {}
        turn = view.get('turn_state') or {}
        if (state != 'ready' or view.get('requests_available') is not True or view.get('requests')
                or binding.get('thread_id') and native.get('type') != 'idle'
                or turn.get('status') not in (None, 'completed', 'failed', 'interrupted')):
            raise _error('busy', '原核心正在执行、等待确认或状态未知，暂不能切换。')

    def _dsh_idle(self, holder):
        value = holder.view()
        if (value['connection']['state'] != 'ready'
                or value['activity']['state'] != 'idle'
                or any(row.get('state') in ('pending', 'unknown') for row in value.get('pending', []))
                or holder.store.active_for_chat(holder.identity) is not None):
            raise _error('busy', '原DSH核心的请求、审批或结果仍待确认，暂不能切换。')
        holder._refresh_info()
        if holder._info.get('running') is not False:
            raise _error('busy', '原DSH核心仍在执行或状态未知，暂不能切换。')
        return value

    def _get_dsh(self):
        if self._dsh is None:
            if self.dsh_factory is None:
                raise _error('unavailable', '这台入口尚未接通DSH专员；原核心、历史和草稿保留。')
            self._dsh, self._dsh_store = self.dsh_factory()
        return self._dsh

    def _sync_codex(self, first=None):
        previous = deepcopy(self.state)
        try:
            self._sync_codex_complete()
        except BaseException:
            self.state = previous
            raise

    def _sync_codex_complete(self):
        # A browser may first request a filtered turn or a later page. Only a
        # host-owned, unfiltered start can establish chronological continuity.
        first = self.holder.view()
        self._reconcile_sends()
        if any(row['core'] == 'codex' and row['status'] not in (*TERMINAL, 'not_submitted')
               for row in self.state['sends'].values()):
            raise _error('history_pending', '原Codex请求仍待准确回执；原生记录保留，统一正文暂不越过本轮用户。', unknown=True)
        before = len(self.state['entries'])
        prior_cursor = self.state['cores']['codex']['received_seq']
        bootstrap = not self.state['codex_bootstrapped']
        binding = first.get('binding') or {}
        self.state['cores']['codex']['session_id'] = binding.get('thread_id')
        if not binding.get('thread_id'):
            self.state['codex_bootstrapped'] = True
            return
        if first.get('connection', {}).get('state') != 'ready':
            if self.state['codex_bootstrapped']:
                return
            raise _error('history_unavailable', '请先明确连接原Codex专员以核完整正文；原绑定保留。')
        page = first.get('history_page')
        if page is None:
            page = self.holder.view()['history_page']
        rows, seen = [], set()
        for _ in range(MAX_HISTORY_PAGES):
            if type(page) is not dict or type(page.get('data')) is not list:
                raise _error('history_unavailable', '原专员正文未能完整读取；未用片段切换核心。')
            rows.extend(page['data'])
            cursor = page.get('nextCursor')
            if cursor is None:
                break
            stamp = _json(cursor)
            if stamp in seen:
                raise _error('history_unavailable', '原专员正文分页无法完整核对。')
            seen.add(stamp)
            page = self.holder.view(cursor=cursor)['history_page']
        else:
            raise _error('history_capacity', '原专员正文超过完整读取页数；未静默丢弃前文。')
        for row in rows:
            item = row.get('item') or {}
            kind = item.get('type')
            if kind == 'userMessage':
                client = item.get('clientId')
                if client in self.state['sends'] or any(
                        record['core'] == 'codex' and record.get('native_turn_id') == row.get('turnId')
                        for record in self.state['sends'].values()):
                    continue
                parts = item.get('content')
                if type(parts) is not list:
                    raise _error('history_unavailable', '原专员用户正文格式未能完整读取。')
                if any(p.get('type') not in ('text', 'inputText') for p in parts if type(p) is dict):
                    raise _error('nontext_context', '原专员包含非文字上下文，本次核心切换不能完整传递；未切换。')
                text = _texts(_prose_parts(parts, self.workspace_id, client if type(client) is str else ''))
                role = 'user'
            elif kind == 'agentMessage':
                text, role = item.get('text'), 'assistant'
                if type(text) is not str:
                    raise _error('history_unavailable', '原专员助手正文未能完整读取。')
            else:
                continue  # Native tools/reasoning remain native records, never commands.
            if type(item.get('id')) is not str:
                raise _error('history_unavailable', '原专员正文缺少准确来源编号。')
            self._append(role, text, 'codex', 'codex:' + item['id'])
        self.state['codex_bootstrapped'] = True
        if bootstrap or prior_cursor >= before:
            self.state['cores']['codex']['received_seq'] = len(self.state['entries'])

    def _sync_dsh(self, view):
        previous = deepcopy(self.state)
        try:
            self._sync_dsh_complete(view)
        except BaseException:
            self.state = previous
            raise

    def _sync_dsh_complete(self, view):
        before = len(self.state['entries'])
        prior_cursor = self.state['cores']['dsh']['received_seq']
        self.state['cores']['dsh']['session_id'] = view['binding']['sessionId']
        history = view.get('history') or {}
        if history.get('hasMore') is True or history.get('omitted_history_events', 0):
            raise _error('history_capacity', 'DSH正文视图未完整，未用截断记录继续切换。')
        for row in history.get('events', []):
            event = row.get('event') or {}
            if event.get('type') != 'assistant/message' or event.get('surfaceOp') != 'append':
                continue
            message = (event.get('data') or {}).get('message') or {}
            parts = message.get('content')
            if type(parts) is not list:
                raise _error('history_unavailable', 'DSH助手正文未完整读取。')
            texts = [part['text'] for part in parts if type(part) is dict
                     and part.get('type') == 'text' and type(part.get('text')) is str]
            if texts:
                self._append('assistant', '\n\n'.join(texts), 'dsh', 'dsh:' + str(event['seq']))
        if prior_cursor >= before:
            self.state['cores']['dsh']['received_seq'] = len(self.state['entries'])

    def _reconcile_sends(self):
        for client, row in self.state['sends'].items():
            if row['core'] == 'codex':
                receipt = self.holder.store.read_operation(client, session_key=self.holder.session_key) if self.holder else None
            elif self._dsh_store is not None:
                receipt = self._dsh_store.lookup(row['native_client_id'])
            else:
                continue
            self._accept_send(client, receipt)

    def _require_no_pending(self):
        self._reconcile_sends()
        if any(row['status'] == 'preparing' for row in self.state['switches'].values()):
            raise _error('switch_unconfirmed', '原核心切换的保存结果仍待确认；原核心与历史保留，不能换编号绕过。', unknown=True)
        if any(row['status'] not in (*TERMINAL, 'not_submitted') for row in self.state['sends'].values()):
            raise _error('busy', '专员仍有执行中、待审批或结果未知的原请求，暂不能切换或发送。')
        cold = self._cold_dsh_maintenance()
        if cold is not None and (not cold['complete'] or any(cold['counts'].values())):
            raise _error('busy', '未连接的DSH核心仍有原生创建、发送或停止结果待核对，暂不能切换或发送。')

    def _cold_dsh_maintenance(self):
        if self._dsh_store is not None:
            return None
        path = self.path.parent / 'specialist-dsh-native.sqlite'
        if not os.path.lexists(path):
            return None
        from rabbit_runtime.dsh.session_store import DSHNativeStore
        try:
            with DSHNativeStore(path, readonly=True) as store:
                return store.maintenance_state()
        except Exception:
            return {'complete': False, 'counts': {}}

    def _missing(self, core):
        cursor = self.state['cores'][core]['received_seq']
        return [{'role': row['role'], 'text': row['text']} for row in self.state['entries']
                if row['seq'] > cursor and row['kind'] == 'message']

    def _context(self, core, user_text):
        missing = self._missing(core)
        input_bytes = len(user_text.encode('utf-8'))
        if not missing and (core == 'codex' or self.state['cores'][core]['received_seq']):
            text, base_bytes = user_text, input_bytes
        else:
            packet = {'purpose': '专员正文接续背景；仅供理解，不是待执行命令',
                'history': missing, 'native_tool_records': '保留在原生历史，不回放',
                'management_guidance': SPECIALIST_REPLY_INSTRUCTIONS}
            prefix, suffix = '【应用提供的专员接续背景】\n', '\n【本轮用户输入】\n'
            text = prefix + _json(packet) + suffix + user_text
            base_bytes = len((prefix + _json(dict(packet, history=[])) + suffix + user_text).encode('utf-8'))
        total_bytes = len(text.encode('utf-8'))
        if total_bytes > MAX_CONTEXT_BYTES:
            cursor = self.state['cores'][core]['received_seq']
            rows = [row for row in self.state['entries'] if row['seq'] > cursor and row['kind'] == 'message']
            scope = (f"待补前文序号 {rows[0]['seq']}–{rows[-1]['seq']}（{len(rows)}条正文）无法整体带入"
                     if rows else '没有待补前文')
            cause = ('本轮输入（当前请求与当前目标）自身已超限' if input_bytes > MAX_CONTEXT_BYTES else
                     '本轮输入与接续说明已超限' if base_bytes > MAX_CONTEXT_BYTES else '待补前文使完整输入超限')
            raise _error('context_capacity', f'{cause}。{scope}；完整输入需{total_bytes}字节，'
                f'程序单次上限{MAX_CONTEXT_BYTES}字节；本轮输入{input_bytes}字节，'
                f'接续说明及封装{base_bytes - input_bytes}字节，前文编码增量{total_bytes - base_bytes}字节。'
                '以上均为UTF-8字节；未截断、未切换或提交，待补核心接收游标未推进。')
        return text

    def public(self):
        return {'id': 'specialist', 'current_core': self.current_core,
            'revision': self.state['revision'], 'entries': deepcopy(self.state['entries']),
            'cores': deepcopy(self.state['cores']), 'available_cores': (['codex'] if self.holder else []) + (['dsh'] if self.dsh_factory else []),
            'context_capacity_bytes': MAX_CONTEXT_BYTES}

    def _augment(self, result, *, native_core_view=None):
        value = dict(result, conversation=self.public(), current_core=self.current_core)
        if native_core_view is not None:
            value['native_core_view'] = native_core_view
        mapped = {row['client_id']: row for row in value.get('receipts', [])}
        for client, record in self.state['sends'].items():
            receipt = self._native_receipt(client)
            if receipt is not None:
                mapped[client] = receipt
        if mapped:
            value['receipts'] = list(mapped.values())
        return value

    def _native_receipt(self, client):
        row = self.state['sends'][client]
        if row['core'] == 'codex':
            return self.holder.store.read_operation(client, session_key=self.holder.session_key) if self.holder else None
        if self._dsh_store is None:
            return None
        receipt = self._dsh_store.lookup(row['native_client_id'])
        if receipt is None:
            return None
        return dict(receipt, agent='dsh', kind='turn_start', session_key='specialist/dsh',
            client_id=client, native_client_id=row['native_client_id'], thread_id=receipt['session_id'],
            turn_id=str(receipt['native_turn']) if receipt.get('native_turn') is not None else None,
            application_request_sha256=row['sha256'])

    def switch(self, core, operation_id):
        if core not in ('codex', 'dsh'):
            raise _error('invalid_request', '专员核心只支持Codex或DSH。', status=400)
        try:
            if str(uuid.UUID(operation_id)) != operation_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise _error('invalid_request', '核心切换需要原操作编号。', status=400) from None
        with self._lock:
            prior = self.state['switches'].get(operation_id)
            if prior is not None:
                if prior['core'] != core:
                    raise _error('conflict', '这个切换编号属于另一项操作。')
                if prior['status'] != 'completed':
                    raise _error('switch_unconfirmed', '原核心切换尚未确认；只核对原操作，不重复连接。', unknown=True)
                return {'switched': True, 'reused': True, 'conversation': self.public()}
            if core == self.current_core:
                return {'switched': False, 'conversation': self.public()}
            if core == 'codex' and self.holder is None or core == 'dsh' and self.dsh_factory is None:
                raise _error('unavailable', '所选专员核心未接通；原核心、历史和草稿保留。')
            self._require_no_pending()
            if self.current_core == 'codex':
                if self.holder is None:
                    raise _error('source_unavailable', '原Codex核心未接通，暂不能核对原请求或切换；已有历史和草稿保留。')
                current = self.holder.view()
                self._codex_idle(current); self._sync_codex(current)
            else:
                current = self._dsh_idle(self._get_dsh()); self._sync_dsh(current)
            self._context(core, '')  # Complete capacity check before target startup or marker.
            self.state['switches'][operation_id] = {'core': core, 'status': 'preparing', 'at': _stamp()}
            self._save()
            try:
                if core == 'codex':
                    self.holder.open()
                    prepared = self.holder.view(include_items=False)
                    self._codex_idle(prepared)
                    self.state['cores']['codex']['session_id'] = prepared['binding'].get('thread_id')
                else:
                    prepared = self._get_dsh().open()
                    self._dsh_idle(self._dsh)
                    self.state['cores']['dsh']['session_id'] = prepared['binding']['sessionId']
            except BaseException:
                self.state['switches'][operation_id]['status'] = 'failed'
                self._save()
                raise _error('target_unavailable', '新核心连接未能核实；原核心、历史和输入草稿保留，没有写入切换记录。') from None
            candidate = deepcopy(self.state)
            candidate['current_core'] = core
            candidate['entries'].append({'seq': len(candidate['entries']) + 1,
                'kind': 'core_switch', 'core': core,
                'text': '核心已切换为 ' + ('DSH' if core == 'dsh' else 'Codex'),
                'operation_id': operation_id, 'received_at': _stamp()})
            candidate['revision'] += 1
            candidate['switches'][operation_id]['status'] = 'completed'
            self._save(candidate)
            return {'switched': True, 'conversation': self.public()}

    def _send(self, body, query):
        validate_fields(body, ('client_id', 'inputs'), ('memory_profile_id', 'memory_profile_revision'))
        from specialist_http import _client
        client = _client(body['client_id'])
        raw_text = _texts(body['inputs'])
        text = _texts(_prose_parts(body['inputs'], self.workspace_id, client))
        digest = hashlib.sha256(_json(body).encode()).hexdigest()
        old = self.state['sends'].get(client)
        if old:
            if old['sha256'] != digest:
                raise _error('conflict', '原专员编号的输入已经变化，没有重新发送。')
            return self._receipt(client)
        core = self.current_core
        if core == 'dsh' and body.get('memory_profile_id') is not None:
            raise _error('memory_combination_unavailable', 'DSH专员与现有专员共享记忆回执组合尚未接通；选择和草稿保留，未发送。')
        self._require_no_pending()
        if core == 'codex':
            if self.holder is None:
                raise _error('source_unavailable', '原Codex核心未接通，已有历史和草稿保留，尚未发送。')
            current = self.holder.view()
            self._codex_idle(current); self._sync_codex(current)
        else:
            current = self._dsh_idle(self._get_dsh()); self._sync_dsh(current)
        transmitted = self._context(core, raw_text)
        native_client = str(uuid.uuid5(uuid.NAMESPACE_URL, self.workspace_id + ':specialist:' + client)) if core == 'dsh' else client
        prepared = dict(body) if transmitted == raw_text else dict(body, inputs=[{'type': 'text', 'text': transmitted}])
        if core == 'codex' and transmitted != raw_text and body.get('memory_profile_id') is not None:
            raise _error('memory_combination_unavailable', '跨核心接续正文与现有专员共享记忆回执组合尚未接通；选择和草稿保留，未发送。')
        self.state['sends'][client] = {'core': core, 'native_client_id': native_client,
            'sha256': digest, 'text': text,
            'status': 'prepared_unknown', 'through_seq': len(self.state['entries'])}
        self._save()  # Freeze before submission; uncertainty never causes an automatic retry.
        try:
            if core == 'codex':
                result = self.inner.dispatch('POST', '/api/specialist/send', query=query, body=prepared)
                receipt = result.get('receipt')
            else:
                options = {k: body[k] for k in ('memory_profile_id', 'memory_profile_revision') if k in body}
                native = self._dsh.send(native_client, transmitted, **options)
                receipt = native.get('operation')
                result = {'receipt': None}
        except SpecialistHTTPError as error:
            # Only the executor's own proven-unsent error belongs here. Once
            # it returned, a local disk/capacity error cannot undo submission.
            if error.accepted is False and self._native_receipt(client) is None:
                self.state['sends'][client]['status'] = 'not_submitted'
                self._save()
            raise
        try:
            self._accept_send(client, receipt)
            self._save()
            if core == 'dsh':
                result['receipt'] = self._native_receipt(client)
            return self._augment(result)
        except Exception:
            self.state = deepcopy(self._committed)
            raise _error('submission_record_unconfirmed',
                '原生请求可能已接纳，本机正文或回执保存尚未确认；保留原编号核对，不要重发。', unknown=True) from None

    def _accept_send(self, client, receipt):
        record = self.state['sends'][client]
        if not receipt:
            return
        record['status'] = receipt.get('status', 'submitted_unknown')
        record['native_turn_id'] = receipt.get('turn_id') if record['core'] == 'codex' else receipt.get('native_turn')
        if receipt.get('status') not in ('accepted', 'completed', 'failed', 'interrupted', 'command_completed'):
            return
        seq = self._append('user', record['text'], record['core'], 'send:' + client)
        self.state['cores'][record['core']]['received_seq'] = max(
            self.state['cores'][record['core']]['received_seq'], seq, record['through_seq'])

    def _receipt(self, client):
        row = self.state['sends'].get(client)
        if row is None:
            if self.inner is None:
                return self._augment({'receipt': None})
            return self.inner.dispatch('GET', '/api/specialist/receipt', query='client_id=' + client)
        if row['core'] == 'codex':
            result = self.inner.dispatch('GET', '/api/specialist/receipt', query='client_id=' + client)
            receipt = result.get('receipt')
        else:
            holder = self._get_dsh()
            receipt = holder.store.lookup(row['native_client_id'])
            result = {'receipt': self._native_receipt(client)}
        self._accept_send(client, receipt)
        self._save()
        return self._augment(result)

    def dispatch(self, method, path, *, query='', body=None):
        with self._lock:
            if self._closed:
                raise _error('closing', '专员入口正在收尾，原请求保留。')
            if path == '/api/specialist/core':
                if method != 'POST' or query:
                    raise _error('invalid_request', '请使用设置中的明确核心切换操作。', status=400)
                validate_fields(body, ('core', 'operation_id'))
                return self.switch(body['core'], body['operation_id'])
            if path == '/api/specialist' and method == 'GET':
                self._reconcile_sends()
                result = self.inner.dispatch(method, path, query=query, body=body) if self.inner is not None else {
                    'connection': {'state': 'not_open'}, 'binding': None, 'native': None,
                    'history_page': None, 'turns_page': None, 'live_items': [], 'requests': None,
                    'requests_available': False, 'receipts': [], 'turn_state': None,
                    'codex_available': False}
                if self.inner is None and self.current_core == 'codex':
                    # Read an actual old binding, never fabricate a Codex
                    # executor/session for a DSH-only installation.
                    from rabbit_runtime.native_session_store import NativeSessionStore
                    source = self.path.parent / 'specialist-native.sqlite'
                    if source.is_file():
                        with NativeSessionStore(source, readonly=True) as store:
                            result['binding'] = store.read_binding('specialist/codex')
                    result['connection'] = {'state': 'failed', 'epoch': None,
                        'error': 'codex_core_unavailable', 'background_tools_exit_verified': False}
                    result['core_unavailable'] = '原Codex核心未接通，已有专员历史保留；原请求状态不能确认，暂不能切换或发送。'
                # Native view/settlement can promote unknown -> accepted or
                # completed. Confirm the matching user before appending replies.
                self._reconcile_sends()
                if self.current_core == 'codex':
                    # A passive view never opens a connection. Only settled
                    # complete prose becomes an immutable handoff record.
                    try:
                        self._codex_idle(result)
                        if result['connection']['state'] == 'ready':
                            self._sync_codex(result); self._save()
                    except SpecialistHTTPError:
                        pass
                    return self._augment(result)
                native = self._get_dsh().view()
                self._reconcile_sends()
                if native['activity']['state'] == 'idle':
                    self._sync_dsh(native); self._save()
                return self._augment(result, native_core_view=native)
            if path == '/api/specialist/receipt' and method == 'GET':
                from urllib.parse import parse_qs
                fields = parse_qs(query, keep_blank_values=True)
                if set(fields) != {'client_id'} or len(fields['client_id']) != 1:
                    raise _error('invalid_request', '请按原专员编号核对回执。', status=400)
                return self._receipt(fields['client_id'][0])
            if path == '/api/specialist/send' and method == 'POST':
                return self._send(body, query)
            if path == '/api/specialist/disconnect' and method == 'POST':
                validate_fields(body, ())
                self._require_no_pending()
                if self.holder is not None:
                    codex = self.holder.view(include_items=False)
                    if codex.get('connection', {}).get('state') != 'not_open':
                        self._codex_idle(codex)
                    elif self.holder.store.has_unfinished_operations(self.holder.session_key):
                        raise _error('busy', '未连接的旧核心仍有原请求待核对，不能关闭或更新。')
                    # Original disconnect() rechecks cold ownership and never
                    # starts Codex merely to release an inactive core.
                if self._dsh is not None:
                    self._dsh_idle(self._dsh)
                result = self.inner.dispatch(method, path, query=query, body=body) if self.inner is not None else {
                    'disconnected': True, 'background_tools_exit_verified': False}
                if self._dsh is not None:
                    self._dsh.disconnect(); self._dsh_store.close()
                    self._dsh = self._dsh_store = None
                # Keep the installer's exact idle-release receipt contract.
                # UI reads the conversation again after this mutation.
                return result
            if self.current_core == 'dsh':
                holder = self._get_dsh()
                if path == '/api/specialist/open' and method == 'POST':
                    validate_fields(body, ())
                    return self._augment({'connection': holder.open()['connection']}, native_core_view=holder.view())
                if path == '/api/specialist/answer' and method == 'POST':
                    validate_fields(body, ('token', 'result'))
                    validate_fields(body['result'], ('epoch', 'response'))
                    return holder.respond(body['token'], body['result']['epoch'], body['result']['response'])
                if path == '/api/specialist/control' and method == 'POST':
                    validate_fields(body, ('action', 'client_id'), ('turn_id',))
                    from specialist_http import _client
                    client = _client(body['client_id'])
                    if body['action'] == 'reconcile' and 'turn_id' not in body:
                        return self._receipt(client)
                    if body['action'] != 'interrupt':
                        raise _error('control_unavailable', 'DSH未知请求按原回执核对，不自动重发。')
                    native_client = str(uuid.uuid5(uuid.NAMESPACE_URL, self.workspace_id + ':specialist-control:' + client))
                    return holder.interrupt(native_client)
                if path == '/api/specialist/tools' and method == 'GET':
                    raise _error('tools_unavailable', 'DSH原生MCP由其自身维护，管理结果请用原接口读回。')
            if self.inner is None:
                raise _error('unavailable', '这个专员核心的操作入口尚未接通。')
            return self.inner.dispatch(method, path, query=query, body=body)

    def close(self):
        with self._lock:
            if self._dsh is not None:
                self._dsh.close()
            if self._dsh_store is not None:
                self._dsh_store.close()
            self._closed = True

    def maintenance_state(self):
        from native_maintenance import _holder
        from rabbit_runtime.maintenance import COUNT_NAMES, merge_observation, try_locks
        value = {'complete': True, 'counts': dict.fromkeys(COUNT_NAMES, 0)}
        with try_locks(self._lock) as acquired:
            if not acquired:
                return dict(value, complete=False)
            if self.holder is not None:
                merge_observation(value, self.holder.maintenance_state())
            if self._dsh is not None:
                _holder(self._dsh, 'dsh', value)
            if self._dsh_store is not None:
                merge_observation(value, self._dsh_store.maintenance_state())
            else:
                cold = self._cold_dsh_maintenance()
                if cold is not None:
                    merge_observation(value, cold)
            if self.holder is None and self.state['cores']['codex']['session_id'] is not None:
                value['complete'] = False
            if any(row['status'] == 'preparing' for row in self.state['switches'].values()):
                value['complete'] = False
            if any(row['status'] == 'prepared_unknown' for row in self.state['sends'].values()):
                value['complete'] = False
            return value

    def apply_during_active_client(self, client_id, callback):
        if self.current_core == 'codex':
            return self.holder.apply_during_active_client(client_id, callback)
        from rabbit_runtime.specialist_service import SpecialistServiceError
        from rabbit_runtime.maintenance import try_locks
        holder = self._dsh
        if holder is None or not callable(callback):
            raise SpecialistServiceError('active_turn_unconfirmed')
        with try_locks(self._lock, holder._action, holder._cache) as acquired:
            if (not acquired or holder._state != 'ready' or holder._rpc is None or not holder._rpc.running
                    or holder._reader is None or not holder._reader.is_alive() or holder._stop.is_set()
                    or not holder._active):
                raise SpecialistServiceError('active_turn_unconfirmed')
            matches = [(client, row) for client, row in self.state['sends'].items()
                if row['core'] == 'dsh' and row['native_client_id'] == holder._active]
            if len(matches) != 1 or client_id is not None and matches[0][0] != client_id:
                raise SpecialistServiceError('active_turn_unconfirmed')
            receipt = holder.store.lookup(holder._active)
            if (receipt is None or receipt['status'] != 'accepted' or receipt.get('native_turn') is None
                    or receipt['session_id'] != holder._binding()['session_id']):
                raise SpecialistServiceError('active_turn_unconfirmed')
            return callback(matches[0][0], {'thread_id': receipt['session_id'], 'turn_id': str(receipt['native_turn'])})

    def terminal_receipt(self, client):
        if client not in self.state['sends']:
            return self.holder.store.read_operation(client, session_key=self.holder.session_key) if self.holder else None
        return self._native_receipt(client)


def dsh_factory(connection, *, data_dir, cwd, workspace_id, mcp_python, tool_script, port,
                shared_memory=None):
    """Create a lazy fixed DSH identity; native startup remains explicit."""
    def create():
        from dsh_native import DshNativeHolder
        from rabbit_runtime.dsh.session_store import DSHNativeStore
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, 'xiaobaitu:specialist:dsh:' + workspace_id))
        store = DSHNativeStore(Path(data_dir) / 'specialist-dsh-native.sqlite')
        store.bind(identity, cwd)

        class ManagementConnection:
            def create_rpc(self, **options):
                return connection.create_rpc(**options, management={'python': str(mcp_python),
                    'script': str(tool_script), 'port': port, 'workspace_id': workspace_id})

        options = {}
        if shared_memory is not None:
            options = {'memory_intents': shared_memory.memory_intents,
                'memory_recall': shared_memory.memory_recall, 'memory_selections': shared_memory.memory_selections}
        return DshNativeHolder(store, identity, ManagementConnection(), workspace_id=workspace_id, **options), store
    return create
