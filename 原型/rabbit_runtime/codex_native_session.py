"""Native Codex session ownership, receipts and scoped protocol interaction.

This module is opt-in and is not wired to the formal chat service. It neither
rebuilds prompts from shared history nor disables native tools/configuration.
Codex owns thread history; this client owns only its binding and operation
receipts. A transport failure never means that an accepted operation was undone.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import threading

from .codex_rpc import RemoteError
from .native_session_lease import NativeSessionLease, DIRECTORY_NAME


TERMINAL = frozenset(('completed', 'failed', 'interrupted'))
_REQUEST_SHA = re.compile(r'[0-9a-f]{64}\Z')
_REUSED = object()
INTERACTIONS = frozenset((
    'item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
    'item/tool/requestUserInput', 'item/permissions/requestApproval',
    'mcpServer/elicitation/request',
))


class NativeSessionError(RuntimeError):
    """A local ownership/protocol issue; the message contains no private data."""


def _clone(value):
    def check(item):
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError
            for child in item.values():
                check(child)
        elif type(item) is list:
            for child in item:
                check(child)
        elif item is not None and type(item) not in (str, bool, int, float):
            raise ValueError
    try:
        check(value)
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise NativeSessionError('invalid_json') from None


def _opaque(value):
    if type(value) is not str or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise NativeSessionError('invalid_native_id')
    return value


def _request_key(value):
    if type(value) not in (int, str):
        raise NativeSessionError('invalid_request_id')
    return (type(value).__name__, value)


def native_request_sha256(request):
    """Fingerprint the complete native request, including its typed ID.

    A UI token may outlive a request ID. Its original request fingerprint lets
    reply verify the current request atomically without the caller taking the
    controller's private lock. No request text is persisted by this helper.
    """
    if type(request) is not dict:
        raise NativeSessionError('invalid_native_request_snapshot')
    try:
        raw = json.dumps(_clone(request), ensure_ascii=False, allow_nan=False,
                         sort_keys=True, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise NativeSessionError('invalid_native_request_snapshot') from None
    return hashlib.sha256(raw).hexdigest()


def build_turn_params(thread_id, client_id, inputs, *, options=None):
    """Pure native turn/start parameters, shared by send and receipt matching.

    Ownership/identity validation remains with the session and its ledger.
    This helper preserves the existing input and option validation exactly;
    it performs no native operation and stores no prompt or configuration.
    """
    inputs, options = _clone(inputs), _clone({} if options is None else options)
    if type(inputs) is not list or not inputs or any(type(item) is not dict for item in inputs):
        raise NativeSessionError('invalid_native_input')
    if type(options) is not dict or any(key in options for key in ('threadId', 'input', 'clientUserMessageId')):
        raise NativeSessionError('invalid_turn_options')
    return dict(options, threadId=thread_id, input=inputs, clientUserMessageId=client_id)


class CodexNativeSession:
    def __init__(self, rpc, store, *, session_key, cwd, experimental_api=False):
        if type(experimental_api) is not bool:
            raise NativeSessionError('invalid_native_capabilities')
        self.rpc, self.store = rpc, store
        self._experimental_api = experimental_api
        self.session_key = session_key
        self.cwd = str(Path(cwd).resolve(strict=True))
        self._bound = self.store.bind(session_key, self.cwd)
        self._lease = NativeSessionLease(store, session_key,
            lease_dir=store.path.parent / DIRECTORY_NAME)
        self._lock = threading.RLock()
        self._initialized = False
        self._requests = {}
        self._request_generations = {}
        # Metadata shares each actual request's lifetime; it is neither a
        # second event queue nor a persistent approval reconstructed on reopen.
        self._request_owners = {}
        self._request_generation = 0
        self._native_info = None

    def initialize(self, *, timeout=15):
        with self._lock:
            if self._initialized:
                self._ready()
                return _clone(self._native_info)
            # A contender cannot start a native executor before it owns the
            # authoritative ledger/session lease. A busy acquire leaves its
            # injected transport untouched and may be explicitly retried later.
            self._lease.acquire()
            try:
                self.rpc.start()
                info = self.rpc.request('initialize', {
                    'clientInfo': {'name': 'xiaobaitu', 'title': '小白兔', 'version': '0.1.0'},
                    'capabilities': {'experimentalApi': self._experimental_api},
                }, timeout=timeout)
                if type(info) is not dict:
                    raise NativeSessionError('invalid_initialize_response')
                info = _clone(info)
                self.rpc.notify('initialized', {})
            except BaseException:
                # An initialize timeout cannot be retried on the same connection.
                # Cleanup failure must not replace the original startup error.
                try:
                    self.rpc.close()
                except BaseException:
                    pass
                if self._rpc_stopped():
                    try:
                        self._lease.close()
                    except BaseException:
                        pass
                raise
            self._native_info = info
            self._initialized = True
            return _clone(info)

    def _ready(self, *, check_scope=True):
        if not self._initialized or not self.rpc.running:
            raise NativeSessionError('native_connection_not_ready')
        if not self._lease.held:
            raise NativeSessionError('native_executor_not_owned')
        if check_scope:
            binding = self._lease.verify()
            if (self._bound.get('thread_id') is not None and
                    (binding.get('thread_id'), binding.get('session_id')) !=
                    (self._bound.get('thread_id'), self._bound.get('session_id'))):
                raise NativeSessionError('native_binding_changed')
            self._bound = binding

    def binding(self):
        return self.store.read_binding(self.session_key)

    def _thread_id(self):
        binding = self._bound
        if not binding or not binding.get('thread_id'):
            raise NativeSessionError('native_thread_not_bound')
        return binding['thread_id']

    def _thread(self, value, *, require_bound=True):
        if type(value) is not dict or type(value.get('thread')) is not dict:
            raise NativeSessionError('invalid_thread_response')
        thread = value['thread']
        thread_id = _opaque(thread.get('id'))
        if require_bound and thread_id != self._thread_id():
            raise NativeSessionError('native_thread_mismatch')
        if thread.get('ephemeral') is not False:
            raise NativeSessionError('persistent_thread_required')
        if thread.get('cwd') != self.cwd:
            raise NativeSessionError('native_working_directory_mismatch')
        if require_bound and self.binding().get('session_id') is not None:
            if thread.get('sessionId') != self.binding()['session_id']:
                raise NativeSessionError('native_session_mismatch')
        if thread.get('sessionId') is not None:
            _opaque(thread['sessionId'])
        return thread

    def _mutate(self, client_id, kind, method, params, *, timeout):
        self._ready()
        params = _clone(params)
        receipt = self.store.begin(self.session_key, client_id, kind, params)
        if not receipt['created']:
            return receipt, _REUSED
        try:
            response = self.rpc.request(method, params, timeout=timeout)
        except RemoteError as error:
            self.store.reject_operation(client_id, rpc_error_code=error.code)
            raise
        # Every other failure stays submitted_unknown. In particular a lost
        # response, local parse failure or client crash is not a native rejection.
        return receipt, response

    def create_thread(self, client_id, *, options=None, timeout=30):
        with self._lock:
            options = _clone({} if options is None else options)
            if type(options) is not dict or any(key in options for key in ('cwd', 'ephemeral')):
                raise NativeSessionError('invalid_thread_options')
            params = dict(options, cwd=self.cwd, serviceName='xiaobaitu', ephemeral=False)
            receipt, response = self._mutate(client_id, 'thread_start', 'thread/start', params, timeout=timeout)
            if response is _REUSED:
                return {'receipt': receipt, 'reused': True}
            thread = self._thread(response, require_bound=False)
            receipt = self.store.accept_operation(client_id, thread_id=thread['id'], session_id=thread.get('sessionId'))
            self._bound = self._lease.verify()
            return {'receipt': receipt, 'native': _clone(response), 'reused': False}

    def resume(self, client_id, *, developer_instructions=None, timeout=30):
        with self._lock:
            if (developer_instructions is not None and
                    (type(developer_instructions) is not str or not developer_instructions.strip()
                     or len(developer_instructions) > 4096 or '\x00' in developer_instructions)):
                raise NativeSessionError('invalid_developer_instructions')
            thread_id = self._thread_id()
            params = {'threadId': thread_id, 'excludeTurns': True}
            if developer_instructions is not None:
                params['developerInstructions'] = developer_instructions
            receipt, response = self._mutate(client_id, 'thread_resume', 'thread/resume',
                params, timeout=timeout)
            if response is _REUSED:
                return {'receipt': receipt, 'reused': True}
            thread = self._thread(response)
            receipt = self.store.accept_operation(client_id, thread_id=thread['id'], session_id=thread.get('sessionId'))
            return {'receipt': receipt, 'native': _clone(response), 'reused': False}

    def read_thread(self, *, timeout=15):
        with self._lock:
            self._ready()
            value = self.rpc.request('thread/read', {'threadId': self._thread_id()}, timeout=timeout)
            self._thread(value)
            return _clone(value)

    def send(self, client_id, inputs, *, options=None, timeout=30):
        with self._lock:
            # Resolve the target after validating JSON/input/options, retaining
            # the original failure order even for an unbound session.
            params = build_turn_params(None, client_id, inputs, options=options)
            params['threadId'] = self._thread_id()
            receipt, response = self._mutate(client_id, 'turn_start', 'turn/start', params, timeout=timeout)
            if response is _REUSED:
                return {'receipt': receipt, 'reused': True}
            if type(response) is not dict or type(response.get('turn')) is not dict:
                raise NativeSessionError('invalid_turn_response')
            turn = response['turn']
            turn_id = _opaque(turn.get('id'))
            if turn.get('status') not in TERMINAL | {'inProgress'}:
                raise NativeSessionError('invalid_turn_status')
            receipt = self.store.accept_operation(client_id, thread_id=params['threadId'], turn_id=turn_id)
            if turn['status'] in TERMINAL:
                receipt = self.store.finish_turn(client_id, thread_id=params['threadId'], turn_id=turn_id, status=turn['status'])
            return {'receipt': receipt, 'native': _clone(response), 'reused': False}

    def interrupt(self, client_id, turn_id, *, timeout=15):
        with self._lock:
            params = {'threadId': self._thread_id(), 'turnId': _opaque(turn_id)}
            receipt, response = self._mutate(client_id, 'turn_interrupt', 'turn/interrupt', params, timeout=timeout)
            if response is _REUSED:
                return {'receipt': receipt, 'reused': True}
            if type(response) is not dict:
                raise NativeSessionError('invalid_interrupt_response')
            receipt = self.store.accept_operation(client_id, thread_id=params['threadId'], turn_id=turn_id)
            # The acknowledgment does not finish a turn. Its native terminal
            # event/history proves turn state, not OS background tool exit.
            return {'receipt': receipt, 'reused': False}

    def turns_page(self, *, cursor=None, limit=50, direction='desc', items_view='full', timeout=15):
        with self._lock:
            self._ready()
            if type(limit) is not int or not 1 <= limit <= 100 or direction not in ('asc', 'desc') or items_view not in ('full', 'summary', 'notLoaded'):
                raise NativeSessionError('invalid_history_page')
            if cursor is not None and type(cursor) is not str:
                raise NativeSessionError('invalid_history_cursor')
            params = {'threadId': self._thread_id(), 'limit': limit, 'sortDirection': direction, 'itemsView': items_view}
            if cursor is not None:
                params['cursor'] = cursor
            page = self.rpc.request('thread/turns/list', params, timeout=timeout)
            if type(page) is not dict or type(page.get('data')) is not list:
                raise NativeSessionError('invalid_history_response')
            # A page is never represented as the entire history. Return the
            # opaque cursors unchanged so the UI can request the next page.
            for turn in page['data']:
                if type(turn) is not dict:
                    raise NativeSessionError('invalid_history_turn')
                self._finish_known_turn(turn)
            return _clone(page)

    def _finish_known_turn(self, turn):
        if turn.get('status') not in TERMINAL:
            return
        turn_id = _opaque(turn.get('id'))
        receipt = self.store.find_turn(self.session_key, turn_id)
        if receipt:
            self.store.finish_turn(receipt['client_id'], thread_id=self._thread_id(),
                turn_id=turn_id, status=turn['status'])
        # A terminal native turn revokes its actual requests even when its
        # unknown receipt has no turn ID. It does not identify that receipt.
        self._expire_turn_requests(turn_id)

    def items_page(self, *, turn_id=None, cursor=None, limit=50, direction='asc', timeout=15):
        with self._lock:
            self._ready()
            if type(limit) is not int or not 1 <= limit <= 100 or direction not in ('asc', 'desc'):
                raise NativeSessionError('invalid_history_page')
            params = {'threadId': self._thread_id(), 'limit': limit, 'sortDirection': direction}
            if turn_id is not None:
                params['turnId'] = _opaque(turn_id)
            if cursor is not None:
                cursor = _clone(cursor)
                if type(cursor) is dict:
                    if set(cursor) != {'type', 'itemId'} or cursor.get('type') != 'item' or turn_id is None:
                        raise NativeSessionError('invalid_history_cursor')
                    _opaque(cursor['itemId'])
                elif type(cursor) is not str:
                    raise NativeSessionError('invalid_history_cursor')
                params['cursor'] = cursor
            page = self.rpc.request('thread/items/list', params, timeout=timeout)
            if type(page) is not dict or type(page.get('data')) is not list:
                raise NativeSessionError('invalid_history_response')
            for entry in page['data']:
                if type(entry) is not dict or type(entry.get('item')) is not dict:
                    raise NativeSessionError('invalid_history_item')
                entry_turn = _opaque(entry.get('turnId'))
                if turn_id is not None and entry_turn != turn_id:
                    raise NativeSessionError('history_turn_mismatch')
            return _clone(page)

    def reconcile(self, client_id, *, expected_request_sha256=None, max_pages=32, timeout=15):
        """Explicitly recover an unknown turn from its native client identity.

        Only the bound thread is read. Absence, ambiguous identity, incomplete
        pagination or an inconsistent active turn never proves non-submission
        and never authorizes a resend. The native clientId is an identity link,
        not native deduplication or proof that every payload/option took effect.
        The original request fingerprint remains in the existing receipt.
        """
        with self._lock:
            self._ready()
            receipt = self.store.read_operation(client_id, session_key=self.session_key)
            if receipt is None or receipt['kind'] != 'turn_start':
                raise NativeSessionError('turn_receipt_required')
            if expected_request_sha256 is not None and (
                    type(expected_request_sha256) is not str or
                    not _REQUEST_SHA.fullmatch(expected_request_sha256) or
                    expected_request_sha256 != receipt['request_sha256']):
                raise NativeSessionError('request_fingerprint_mismatch')
            if type(max_pages) is not int or not 1 <= max_pages <= 100:
                raise NativeSessionError('invalid_reconciliation_bound')

            def uncertain(reason):
                return {'receipt': _clone(receipt), 'reconciled': False, 'reason': reason}

            if receipt['status'] != 'submitted_unknown':
                return uncertain('already_known')
            thread_id = self._thread_id()
            initial = self.read_thread()['thread']

            def runtime(thread):
                status = thread.get('status')
                if (type(status) is not dict or status.get('type') not in
                        ('idle', 'active', 'notLoaded', 'systemError') or
                        type(thread.get('updatedAt')) is not int):
                    raise NativeSessionError('invalid_reconciliation_thread_state')
                return _clone(status), thread['updatedAt']

            initial_runtime = runtime(initial)
            if initial_runtime[0]['type'] == 'systemError':
                return uncertain('native_state_unavailable')

            def pages(method, fields):
                # No turns_page call here: its normal reconciliation of already
                # known terminal turns must not mutate unrelated receipts while
                # this explicit proof is incomplete. Retain only identities.
                cursor, seen = None, set()
                for _ in range(max_pages):
                    self._ready()
                    params = dict(fields, threadId=thread_id, limit=100)
                    if cursor is not None:
                        params['cursor'] = cursor
                    page = self.rpc.request(method, params, timeout=timeout)
                    if (type(page) is not dict or type(page.get('data')) is not list
                            or len(page['data']) > 100 or 'nextCursor' not in page):
                        raise NativeSessionError('invalid_reconciliation_page')
                    yield page['data']
                    next_cursor = page['nextCursor']
                    if next_cursor is None:
                        return
                    if type(next_cursor) is not str or not next_cursor or next_cursor in seen:
                        raise NativeSessionError('incomplete_reconciliation_history')
                    seen.add(next_cursor)
                    cursor = next_cursor
                raise NativeSessionError('incomplete_reconciliation_history')

            turns = {}
            for rows in pages('thread/turns/list', {'sortDirection': 'asc', 'itemsView': 'notLoaded'}):
                for turn in rows:
                    if type(turn) is not dict or turn.get('status') not in TERMINAL | {'inProgress'}:
                        raise NativeSessionError('invalid_reconciliation_turn')
                    turn_id = _opaque(turn.get('id'))
                    if turn_id in turns:
                        raise NativeSessionError('inconsistent_reconciliation_history')
                    turns[turn_id] = turn['status']

            matches, item_ids = [], set()
            for rows in pages('thread/items/list', {'sortDirection': 'asc'}):
                for entry in rows:
                    if type(entry) is not dict or type(entry.get('item')) is not dict:
                        raise NativeSessionError('invalid_reconciliation_item')
                    turn_id, item = _opaque(entry.get('turnId')), entry['item']
                    item_id = _opaque(item.get('id'))
                    item_key = (turn_id, item_id)
                    if turn_id not in turns or item_key in item_ids or type(item.get('type')) is not str:
                        raise NativeSessionError('inconsistent_reconciliation_history')
                    item_ids.add(item_key)
                    if item['type'] == 'userMessage' and item.get('clientId') == client_id:
                        matches.append((turn_id, item_id))
            if len(matches) != 1:
                return uncertain('native_client_id_missing' if not matches else 'native_client_id_ambiguous')

            current = self.read_thread()['thread']
            current_runtime = runtime(current)
            if current_runtime != initial_runtime:
                return uncertain('native_snapshot_changed')
            active = [turn for turn, status in turns.items() if status == 'inProgress']
            if ((current_runtime[0]['type'] == 'active' and len(active) != 1) or
                    (current_runtime[0]['type'] != 'active' and active)):
                return uncertain('native_active_turn_unidentified')
            turn_id, item_id = matches[0]
            # Revalidate the immutable scope and receipt immediately before the
            # only writes. A history lookup cannot overwrite a newer decision.
            self._ready()
            latest = self.store.read_operation(client_id, session_key=self.session_key)
            if latest['request_sha256'] != receipt['request_sha256']:
                raise NativeSessionError('request_fingerprint_mismatch')
            if latest['status'] != 'submitted_unknown':
                receipt = latest
                return uncertain('already_known')
            receipt = self.store.accept_operation(client_id, thread_id=thread_id, turn_id=turn_id)
            status = turns[turn_id]
            if status in TERMINAL:
                receipt = self.store.finish_turn(client_id, thread_id=thread_id, turn_id=turn_id, status=status)
                self._expire_turn_requests(turn_id)
            else:
                self._confirm_request_ownership(receipt)
            return {'receipt': _clone(receipt), 'reconciled': True,
                    'proof': {'source': 'native_user_message_client_id', 'thread_id': thread_id,
                              'turn_id': turn_id, 'item_id': item_id, 'complete_history': True,
                              'native_status': status, 'payload_options_independently_verified': False}}

    def _confirm_request_ownership(self, receipt):
        # Only the complete explicit reconcile path calls this after accepting
        # the original receipt. A matching current turn alone is not proof.
        for key, owner in self._request_owners.items():
            request = self._requests.get(key)
            if (request is not None and receipt['status'] == 'accepted'
                    and owner['client_id'] == receipt['client_id']
                    and owner['receipt_sha256'] == receipt['request_sha256']
                    and owner['turn_id'] == receipt['turn_id']
                    and owner['thread_id'] == receipt['thread_id']
                    and request['params'].get('turnId') == owner['turn_id']
                    and owner['generation'] == self._request_generations.get(key)
                    and owner['request_sha256'] == native_request_sha256(request)):
                owner['confirmed'] = True

    def _expire_turn_requests(self, turn_id):
        for key, request in list(self._requests.items()):
            if request['params'].get('turnId') == turn_id:
                self._requests.pop(key)
                self._request_generations.pop(key, None)
                self._request_owners.pop(key, None)
                self.rpc.forget_request(request['id'])

    def next_event(self, *, timeout=15):
        # Streaming display is not an execution action. Use the immutable bound
        # identity instead of reopening SQLite and stat'ing paths for every
        # token; actual sends and approvals still revalidate the live scope.
        self._ready(check_scope=False)
        event = self.rpc.next_event(timeout=timeout)
        with self._lock:
            event = _clone(event)
            params = event.get('params', {})
            if type(params) is not dict:
                raise NativeSessionError('invalid_event_params')
            method = event.get('method')
            binding = self._bound
            thread_id = binding.get('thread_id') if binding else None
            event_thread = params.get('threadId')
            if event_thread is None and type(params.get('thread')) is dict:
                event_thread = params['thread'].get('id')
            if 'id' in event:
                if not thread_id or event_thread != thread_id:
                    self.rpc.respond(event['id'], error={'code': -32600, 'message': 'Request is outside this native session.'})
                    return {'type': 'out_of_scope_interaction', 'method': method}
                if method not in INTERACTIONS:
                    self.rpc.respond(event['id'], error={'code': -32601, 'message': 'This client does not implement this native interaction.'})
                    return {'type': 'unsupported_interaction', 'method': method}
                turn_id = params.get('turnId')
                owner = None
                if turn_id is not None:
                    turn_id = _opaque(turn_id)
                    turn = self.store.find_turn(self.session_key, turn_id)
                    if not turn or turn['status'] != 'accepted':
                        unknown = [row for row in self.store.pending_operations(self.session_key)
                                   if row['kind'] == 'turn_start' and row['status'] == 'submitted_unknown']
                        if turn is not None or len(unknown) != 1:
                            self.rpc.respond(event['id'], error={'code': -32600, 'message': 'Native turn is not active in this client.'})
                            return {'type': 'out_of_scope_interaction', 'method': method}
                        owner = {'client_id': unknown[0]['client_id'],
                                 'receipt_sha256': unknown[0]['request_sha256'],
                                 'thread_id': thread_id, 'turn_id': turn_id,
                                 'request_sha256': native_request_sha256(event),
                                 'confirmed': False}
                elif method != 'mcpServer/elicitation/request':
                    self.rpc.respond(event['id'], error={'code': -32600, 'message': 'Native turn is missing from the interaction.'})
                    return {'type': 'out_of_scope_interaction', 'method': method}
                key = _request_key(event['id'])
                if key in self._requests:
                    raise NativeSessionError('native_request_identity_ambiguous')
                self._request_generation += 1
                self._requests[key] = event
                self._request_generations[key] = self._request_generation
                if owner is not None:
                    owner['generation'] = self._request_generation
                    self._request_owners[key] = owner
                return {'type': 'interaction', 'request': event,
                        'request_generation': self._request_generation}
            if event_thread is not None and event_thread != thread_id:
                return {'type': 'outside_session_event', 'method': method}
            if event_thread is None and method not in ('configWarning', 'warning'):
                return {'type': 'outside_session_event', 'method': method}
            if method == 'turn/completed' and type(params.get('turn')) is dict:
                self._finish_known_turn(params['turn'])
            if method == 'serverRequest/resolved' and event_thread == thread_id:
                request_id = params.get('requestId')
                self._requests.pop(_request_key(request_id), None)
                self._request_generations.pop(_request_key(request_id), None)
                self._request_owners.pop(_request_key(request_id), None)
                self.rpc.forget_request(request_id)
            return {'type': 'notification', 'event': event}

    def pending_requests(self):
        with self._lock:
            return _clone(list(self._requests.values()))

    def pending_request_snapshots(self):
        """Native schema plus this controller's occurrence ID, in one snapshot.

        Valid JSON-RPC IDs can be reused after resolution, including with an
        identical request body. Generations belong to this connection; callers
        must also bind their UI tokens to their own connection epoch. An actual
        request retained during unknown submission is not an approval capability
        until explicit reconciliation confirms its original receipt and turn.
        """
        with self._lock:
            result = []
            for key, request in self._requests.items():
                snapshot = {'request': _clone(request), 'generation': self._request_generations[key]}
                if key in self._request_owners and not self._request_owners[key]['confirmed']:
                    snapshot['ownership_confirmed'] = False
                result.append(snapshot)
            return result

    def reply(self, request_id, result, *, expected_request_sha256=None,
              expected_request_generation=None, timeout=10):
        with self._lock:
            self._ready()
            key = _request_key(request_id)
            request = self._requests.get(key)
            if request is None:
                raise NativeSessionError('native_request_not_pending')
            if expected_request_generation is not None:
                if type(expected_request_generation) is not int or expected_request_generation < 1:
                    raise NativeSessionError('invalid_request_generation')
                if self._request_generations.get(key) != expected_request_generation:
                    raise NativeSessionError('native_request_instance_changed')
            if expected_request_sha256 is not None:
                if (type(expected_request_sha256) is not str or
                        not _REQUEST_SHA.fullmatch(expected_request_sha256)):
                    raise NativeSessionError('invalid_request_fingerprint')
                if native_request_sha256(request) != expected_request_sha256:
                    raise NativeSessionError('native_request_changed')
            owner = self._request_owners.get(key)
            if owner is not None:
                if not owner['confirmed']:
                    raise NativeSessionError('native_request_ownership_unconfirmed')
                if (owner['generation'] != self._request_generations.get(key)
                        or owner['request_sha256'] != native_request_sha256(request)):
                    raise NativeSessionError('native_request_instance_changed')
            turn_id = request['params'].get('turnId')
            if turn_id is not None:
                turn = self.store.find_turn(self.session_key, turn_id)
                if (not turn or turn['status'] != 'accepted' or owner is not None and
                        (turn['client_id'] != owner['client_id']
                         or turn['request_sha256'] != owner['receipt_sha256']
                         or turn_id != owner['turn_id'])):
                    self._expire_turn_requests(turn_id)
                    raise NativeSessionError('native_request_expired')
            result = _clone(result)
            if type(result) is not dict:
                raise NativeSessionError('invalid_interaction_answer')
            # The native protocol validates the response's full schema. The UI
            # must present this exact request and obtain a decision, never infer
            # acceptance from a session setting or from an old approval.
            self.rpc.respond(request_id, result=result, timeout=timeout)
            self._requests.pop(key)
            self._request_generations.pop(key, None)
            self._request_owners.pop(key, None)
            return {'response_submitted': True, 'resolved_by_server': False}

    def _rpc_stopped(self):
        # CodexRpc.running also becomes false on protocol failure, before the
        # child has necessarily exited. Prefer its exact owned process handle;
        # only process-free injected test transports use their running flag.
        process = getattr(self.rpc, '_process', None)
        if process is not None:
            try:
                return process.poll() is not None
            except Exception:
                return False
        return not self.rpc.running

    def close(self):
        with self._lock:
            self._requests.clear()
            self._request_generations.clear()
            self._request_owners.clear()
            self._initialized = False
            try:
                self.rpc.close()
            finally:
                # Retain exclusivity when cleanup failed and the old server is
                # still live. Owner exit is not proof all background tools ended.
                if self._rpc_stopped():
                    self._lease.close()
            if not self._rpc_stopped():
                raise NativeSessionError('native_process_not_stopped')
