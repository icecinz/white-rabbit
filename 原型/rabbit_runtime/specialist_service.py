"""Holder for one native Codex specialist; HTTP/UI adapters live separately.

The caller owns the existing NativeSessionStore. This module owns one lazy
transport/session and exactly one event consumer. Codex remains the transcript
store; RAM is a bounded display projection, never a replacement prompt.

Public methods: observe_work(), open(), view(cursor=None, turn_id=None, after_seq=None, include_items=True),
models(), send(client_id, inputs, options=None), answer(token, result),
control(action, client_id, turn_id=None), disconnect(), close(). Only explicit open starts or
resumes a connection. Opening an unbound session never creates an empty thread.
View returns connection, binding, native, history_page, turns_page, live_items,
requests, answers, events, last_seq, reset_required, receipts and turn_state.
The holder explicitly enables the experimental paginated-history contract for
new threads. Existing bindings only resume; unsupported history is reported
separately from execution, without conversion or a replacement thread.
Ordinary holders inherit native settings. A trusted specialist connection can
fix model/effort on every new turn, including after resuming its existing thread.
It may also supply application reply guidance when creating or resuming that
same thread, without changing user input or any turn receipt.
This request policy is not actual model-execution telemetry.
An interrupted turn never proves background-tool exit.
"""
from __future__ import annotations

from collections import OrderedDict, deque
import hashlib
import json
from pathlib import Path
import secrets
import threading
import unicodedata

from .codex_rpc import CodexRpc, RemoteError
from .codex_native_session import CodexNativeSession, native_request_sha256, _clone, _request_key
from .native_session_store import NativeSessionStoreError
from .specialist_tool_status import (read_tool_status, validate_scope as validate_tool_scope,
    SpecialistToolStatusError)


MAX_EVENTS = 128
MAX_EVENT_BYTES = 512 * 1024
MAX_LIVE_ITEMS = 64
MAX_LIVE_BYTES = 512 * 1024
MAX_ITEM_BYTES = 128 * 1024
MAX_REQUESTS = 64
MAX_REQUEST_BYTES = 512 * 1024
MAX_RECEIPTS = 64
MAX_ANSWERS = 64
MAX_CHUNKS = 2048
MAX_MODEL_PAGES = 8
MODEL_PAGE_SIZE = 100
MAX_MODEL_OPTION_CHARS = 256
MAX_MODEL_EFFORTS = 64


class SpecialistServiceError(RuntimeError):
    """Bounded local code; native text, paths and payloads are never included."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class SpecialistSendNotSubmitted(SpecialistServiceError):
    """This new user turn was not submitted because its thread was rejected.

    Created only after an exact rejected thread-start receipt is checked. An
    older user operation, unknown thread creation or rejected turn is excluded.
    """

    def __init__(self):
        super().__init__('thread_creation_rejected_before_send')


class SpecialistSendNotReady(SpecialistServiceError):
    """A new client turn has no native operation and failed before any RPC."""

    def __init__(self):
        super().__init__('connection_not_ready_before_send')


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':')).encode('utf-8'))


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _safe_failure(error):
    code = getattr(error, 'reason', None) or getattr(error, 'code', None)
    if type(code) is not str or not 1 <= len(code) <= 80 or not all(
            c.isascii() and (c.isalnum() or c == '_') for c in code):
        code = type(error).__name__
    return code


def _model_string(value, *, limit=MAX_MODEL_OPTION_CHARS):
    """A bounded native identifier, without inventing a fixed model catalog."""
    if (type(value) is not str or not 1 <= len(value) <= limit or not value.strip()
            or any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs') for char in value)):
        return False
    return True


def validate_model_options(options):
    if options is None:
        return {}
    if (type(options) is not dict or options.keys() - {'model', 'effort'}
            or any(not _model_string(value) for value in options.values())):
        raise SpecialistServiceError('invalid_native_model_options')
    return dict(options)


def _model_row(row):
    """Only picker data crosses the boundary; unknown native fields stay local."""
    invalid = SpecialistServiceError('invalid_native_model_catalog')
    if (type(row) is not dict or not _model_string(row.get('model'))
            or not _model_string(row.get('displayName'), limit=512)
            or not _model_string(row.get('defaultReasoningEffort'))
            or type(row.get('isDefault')) is not bool):
        raise invalid
    efforts = row.get('supportedReasoningEfforts')
    if type(efforts) is not list or len(efforts) > MAX_MODEL_EFFORTS:
        raise invalid
    public_efforts = []
    for effort in efforts:
        if type(effort) is not dict or not _model_string(effort.get('reasoningEffort')):
            raise invalid
        public = {'reasoningEffort': effort['reasoningEffort']}
        if 'description' in effort:
            description = effort['description']
            if type(description) is not str or len(description) > 8192:
                raise invalid
            try:
                description.encode('utf-8')
            except UnicodeError:
                raise invalid from None
            public['description'] = description
        public_efforts.append(public)
    result = {'model': row['model'], 'displayName': row['displayName'],
        'defaultReasoningEffort': row['defaultReasoningEffort'],
        'supportedReasoningEfforts': public_efforts, 'isDefault': row['isDefault']}
    if 'inputModalities' in row:
        modalities = row['inputModalities']
        # Original InputModality enum. The optional field has neither minItems
        # nor uniqueItems: preserve absence, empty arrays, order and duplicates.
        # A schema default is not evidence that this observed row declares it.
        if (type(modalities) is not list or any(type(value) is not str
                or value not in ('text', 'image', 'audio') for value in modalities)):
            raise invalid
        result['inputModalities'] = list(modalities)
    return result


class SpecialistService:
    def __init__(self, store, *, cwd, rpc_factory=None, session_key='specialist/codex', fixed_options=None,
                 developer_instructions=None):
        if rpc_factory is not None and not callable(rpc_factory):
            raise SpecialistServiceError('invalid_rpc_factory')
        self._fixed_options = None if fixed_options is None else validate_model_options(fixed_options)
        if self._fixed_options is not None and self._fixed_options.keys() != {'model', 'effort'}:
            raise SpecialistServiceError('invalid_fixed_model_options')
        if developer_instructions is not None and not _model_string(developer_instructions, limit=4096):
            raise SpecialistServiceError('invalid_developer_instructions')
        # Trusted application prose belongs to native thread instructions, not
        # the user's input or its turn receipt. Ordinary holders leave it unset.
        self._developer_instructions = developer_instructions
        self.store = store
        self.cwd = str(Path(cwd).resolve(strict=True))
        # The application supplies this trusted scope; the existing ledger
        # validates its identity and cwd without introducing another ID format.
        self.session_key = session_key
        self.store.bind(self.session_key, self.cwd)
        self._create_id = 'specialist/thread/' + hashlib.sha256(
            self.session_key.encode()).hexdigest()[:32]
        # Neither constructing this lambda nor viewing the holder starts a CLI.
        self._factory = rpc_factory or (lambda: CodexRpc(
            ['codex', 'app-server', '--listen', 'stdio://'], cwd=self.cwd))
        self._actions = threading.RLock()
        self._cache = threading.RLock()
        self._session = self._rpc = self._reader = self._stop = None
        self._epoch = None
        self._state = 'not_open'
        self._error = None
        self._closed = False
        self._disconnect_unconfirmed = False
        self._seq = 0
        self._events = deque()
        self._event_bytes = 0
        self._live = OrderedDict()
        self._live_bytes = 0
        self._live_incomplete = False
        self._tokens = {}
        self._keys = {}
        self._request_revision = 0
        self._answers = OrderedDict()
        self._submitted_keys = {}
        self._clients = OrderedDict()
        self._turn_state = None
        self._turn_client_id = None
        self._turn_observed_client_id = None

    def maintenance_state(self, *, include_ledger=True):
        from .maintenance import codex_holder_state
        result = codex_holder_state(self, include_ledger=include_ledger)
        if self._disconnect_unconfirmed:
            result['complete'] = False
        return result

    def observe_work(self):
        """Observe this holder's metadata, never a native process or transcript.

        This is not admission or proof of background-tool exit. Locks are
        nonblocking; only a prospective idle result reads bounded ledger counts.
        Missing identity, stale turn projection or an unresolved operation stays
        unknown. No RPC, session recovery, approval capability or model claim is
        produced here.
        """
        from .maintenance import count_rows, try_locks
        result = {'complete': False, 'reason': 'lock_busy', 'status': 'unknown',
            'connection_state': 'unknown', 'epoch': None,
            'native_session_id': None, 'pending_requests': None}
        with try_locks(self._actions) as acquired:
            if not acquired:
                return result
            with try_locks(self._cache) as acquired:
                if not acquired:
                    return result
                state = self._state
                if state in ('not_open', 'opening', 'ready', 'failed', 'closed'):
                    result['connection_state'] = state
                epoch = self._epoch
                if (type(epoch) is str and len(epoch) == 32
                        and all(c in '0123456789abcdef' for c in epoch)):
                    result['epoch'] = epoch
                session = self._session
                if state != 'ready' or self._closed or self._disconnect_unconfirmed or self._error:
                    result['reason'] = 'connection_unavailable'
                    return result
                if (self._rpc is None or session is None or self._reader is None
                        or not self._reader.is_alive() or self._stop is None or self._stop.is_set()):
                    result['reason'] = 'reader_unavailable'
                    return result
                with try_locks(session._lock) as acquired:
                    if not acquired:
                        return result
                    binding = session._bound
                    if (result['epoch'] is None or type(binding) is not dict
                            or not _model_string(binding.get('thread_id'))
                            or (binding.get('session_id') is not None
                                and not _model_string(binding['session_id']))
                            or session.session_key != self.session_key or not session._initialized
                            or session.rpc is not self._rpc):
                        result['reason'] = 'identity_unavailable'
                        return result
                    # thread_id is Codex's authoritative source identity. Its
                    # separate sessionId is optional in valid native replies.
                    result['native_session_id'] = binding['thread_id']
                    count = len(session._requests)
                    if count > MAX_REQUESTS:
                        result['reason'] = 'requests_unavailable'
                        return result
                    result['pending_requests'] = count
                    unknown_answer = any(not row.get('resolved_by_server', False)
                        and (token in self._tokens or token in self._submitted_keys.values())
                        for token, row in self._answers.items())
                    if unknown_answer or any(not owner.get('confirmed', False)
                            for owner in session._request_owners.values()):
                        result['reason'] = 'interaction_unconfirmed'
                        return result
                    if count:
                        return dict(result, complete=True, reason=None, status='needs_user')
                    turn = self._turn_state
                    if (type(turn) is not dict or not self._turn_client_id
                            or self._turn_observed_client_id != self._turn_client_id):
                        result['reason'] = 'turn_unconfirmed'
                        return result
                    if turn.get('status') in ('accepted', 'inProgress'):
                        return dict(result, complete=True, reason=None, status='running')
                    if turn.get('status') not in ('completed', 'failed', 'interrupted'):
                        result['reason'] = 'turn_unconfirmed'
                        return result
                    observed = (session, epoch, self._turn_client_id, turn)
            # Keep actions serialized, but never hold the reader/cache/session
            # locks during this existing 25ms-budget metadata-only SQL helper.
            counts = count_rows(self.store.path, {'unfinished': ('native_operations',
                "session_key=? AND (status='submitted_unknown' OR (kind='turn_start' AND status='accepted'))",
                (self.session_key,))}, lock=self.store.lock,
                expected_identity=self.store._file_identity, check_path=self.store._check_path,
                flock_fd=self.store._lock_fd)
            if not counts['complete']:
                return dict(result, reason='metadata_unavailable')
            if counts['counts']['unfinished']:
                return dict(result, reason='operation_unconfirmed')
            with try_locks(self._cache, session._lock) as acquired:
                if not acquired:
                    return dict(result, reason='lock_busy')
                if (self._session is not observed[0] or self._epoch != observed[1]
                        or self._turn_client_id != observed[2] or self._turn_state != observed[3]
                        or self._state != 'ready' or self._error or self._reader is None
                        or not self._reader.is_alive() or self._stop.is_set()
                        or session._requests or session._request_owners
                        or any(not row.get('resolved_by_server', False)
                            and (token in self._tokens or token in self._submitted_keys.values())
                            for token, row in self._answers.items())):
                    return dict(result, reason='observation_changed')
                return dict(result, complete=True, reason=None, status='idle')

    @property
    def fixed_model_options(self):
        """Trusted request policy only; no discovery, native I/O or model claim."""
        return None if self._fixed_options is None else dict(self._fixed_options)

    def require_ready(self):
        if self._closed:
            raise SpecialistServiceError('specialist_closed')
        if (self._state != 'ready' or self._session is None or
                not self._rpc.running or self._reader is None or not self._reader.is_alive()):
            raise SpecialistServiceError('specialist_not_ready')

    def _watch(self, client_id):
        # Validate via the existing ledger, without inventing another ID format.
        previous = self.store.read_operation(client_id, session_key=self.session_key)
        self._clients[client_id] = None
        self._clients.move_to_end(client_id)
        while len(self._clients) > MAX_RECEIPTS:
            self._clients.popitem(last=False)
        return previous

    def _thread_rejection_proves_unsent(self, receipt, *, client_id, previous):
        # A rejected thread start is not a rejected user turn. Only a new user
        # client with no durable operation can use this narrow pre-send proof.
        if previous is not None or type(receipt) is not dict:
            return False
        expected = (self._create_id, self.session_key, 'thread_start', 'rejected', None)
        keys = ('client_id', 'session_key', 'kind', 'status', 'thread_id')
        if tuple(receipt.get(key) for key in keys) != expected or type(receipt.get('rpc_error_code')) is not int:
            return False
        current = self.store.read_operation(self._create_id, session_key=self.session_key)
        return (current is not None and tuple(current.get(key) for key in keys) == expected
            and current.get('request_sha256') == receipt.get('request_sha256')
            and current.get('rpc_error_code') == receipt['rpc_error_code']
            and self.store.read_operation(client_id, session_key=self.session_key) is None
            and self.store.read_binding(self.session_key).get('thread_id') is None)

    def _restore_unsent_client(self, client_id, previous_turn_client):
        with self._cache:
            if self._turn_client_id == client_id:
                self._turn_client_id = previous_turn_client

    def _project_turn(self, binding, receipts):
        """RAM only: known own receipts, never idle or an unknown submission.

        Called under _cache. _actions serializes new sends with view/open;
        the reader may meanwhile confirm a terminal state, which must not be
        rolled back by an earlier accepted receipt snapshot.
        """
        rows = {row['client_id']: row for row in receipts
            if row['kind'] == 'turn_start' and row.get('thread_id') is not None
            and row['thread_id'] == binding.get('thread_id') and row.get('turn_id')}
        active = [row for row in rows.values() if row['status'] == 'accepted']
        if len(active) > 1:
            self._turn_state = None
            self._turn_observed_client_id = None
            return
        row = active[0] if active else rows.get(self._turn_client_id)
        current = self._turn_state
        if row is None and current is not None:
            row = next((value for value in rows.values()
                if value['turn_id'] == current['turn_id']), None)
        if row is None:
            return
        if current is not None and current['turn_id'] == row['turn_id']:
            if current['status'] in ('completed', 'failed', 'interrupted'):
                return
            if row['status'] == 'accepted':
                return
        if row['status'] not in ('accepted', 'completed', 'failed', 'interrupted'):
            return
        self._turn_client_id = row['client_id']
        self._turn_state = {'turn_id': row['turn_id'], 'status': row['status'],
                            'background_tools_exit_verified': False}
        self._turn_observed_client_id = row['client_id']

    def _dispose(self, *, retain_on_error=False):
        if self._stop is not None:
            self._stop.set()
        session, rpc, reader = self._session, self._rpc, self._reader
        # This reader may still touch the authoritative ledger after receiving
        # a queued native terminal event. Stop it before controller.close can
        # release the executor lease; failed joining retains the entire owner.
        if reader is not None and reader is not threading.current_thread() and reader.ident is not None:
            reader.join(timeout=1)
        if reader is not None and reader.is_alive():
            with self._cache:
                self._state, self._error = 'failed', 'native_shutdown_unconfirmed'
            raise SpecialistServiceError('native_shutdown_unconfirmed')
        error = None
        try:
            if session is not None:
                session.close()
            elif rpc is not None:
                rpc.close()
        except BaseException as caught:
            error = caught
        stopped = session is None and (rpc is None or not rpc.running)
        if session is not None:
            stopped = session._rpc_stopped()
        if not stopped:
            with self._cache:
                self._state, self._error = 'failed', 'native_shutdown_unconfirmed'
            if error is not None:
                raise error
            raise SpecialistServiceError('native_shutdown_unconfirmed')
        if error is not None and retain_on_error:
            # A stopped CLI alone does not prove its transport I/O joined.
            # Keep the exact objects so an explicit disconnect can retry that
            # cleanup; no replacement executor may hide the failed close.
            raise error
        self._session = self._rpc = self._reader = self._stop = None
        if error is not None:
            with self._cache:
                self._state = 'closed' if self._closed else 'failed'
                self._error = _safe_failure(error)
            raise error

    def open(self):
        with self._actions:
            if self._closed:
                raise SpecialistServiceError('specialist_closed')
            if self._disconnect_unconfirmed:
                raise SpecialistServiceError('native_shutdown_unconfirmed')
            if (self._state == 'ready' and self._rpc is not None and self._rpc.running
                    and self._reader is not None and self._reader.is_alive()):
                self.require_ready()
                return self._connection()
            self._dispose()
            epoch = secrets.token_hex(16)
            with self._cache:
                self._epoch, self._state, self._error = epoch, 'opening', None
                self._tokens.clear(); self._keys.clear(); self._answers.clear(); self._submitted_keys.clear()
                self._request_revision += 1
                self._events.clear(); self._event_bytes = 0
                self._live.clear(); self._live_bytes = 0; self._live_incomplete = False
                self._turn_state = self._turn_client_id = self._turn_observed_client_id = None
            try:
                self._rpc = self._factory()
                self._session = CodexNativeSession(self._rpc, self.store,
                    session_key=self.session_key, cwd=self.cwd, experimental_api=True)
                self._session.initialize()
                if self._session.binding().get('thread_id') is not None:
                    # This nonce belongs to this connection, never an accepted
                    # receipt from a different process/epoch.
                    resume_id = 'specialist/resume/' + epoch
                    self._watch(resume_id)
                    if self._developer_instructions is None:
                        self._session.resume(resume_id)
                    else:
                        self._session.resume(resume_id, developer_instructions=self._developer_instructions)
                binding = self.store.read_binding(self.session_key)
                pending_turns = [row for row in self.store.pending_operations(self.session_key)
                    if row['kind'] == 'turn_start' and row['status'] == 'accepted'
                    and row.get('thread_id') == binding.get('thread_id') and row.get('turn_id')]
                if len(pending_turns) == 1:
                    self._watch(pending_turns[0]['client_id'])
                with self._cache:
                    self._project_turn(binding, pending_turns)
                self._stop = threading.Event()
                self._reader = threading.Thread(target=self._read_events,
                    args=(self._session, epoch, self._stop), daemon=True,
                    name='rabbit-specialist-events')
                with self._cache:
                    self._state = 'ready'
                    self._push({'type': 'connection_opened', 'epoch': epoch})
                self._reader.start()
            except BaseException as error:
                with self._cache:
                    self._state, self._error = 'failed', _safe_failure(error)
                try:
                    self._dispose()
                except BaseException:
                    pass  # Never replace the original initialization failure.
                raise
            return self._connection()

    def _connection(self):
        with self._cache:
            connected = self._rpc is not None and self._rpc.running
            state = 'failed' if self._state == 'ready' and not connected else self._state
            return {'state': state, 'epoch': self._epoch,
                    'error': self._error or ('native_connection_lost' if state != self._state else None),
                    'experimental_api': True, 'native_history_contract': 'paginated',
                    'background_tools_exit_verified': False}

    def _push(self, value):
        self._seq += 1
        record = {'seq': self._seq, 'value': value}
        size = _bytes(record)
        if size > MAX_EVENT_BYTES:
            self._live_incomplete = True
            record = {'seq': self._seq, 'value': {'type': 'display_cache_overflow'}}
            size = _bytes(record)
        self._events.append((record, size)); self._event_bytes += size
        while len(self._events) > MAX_EVENTS or self._event_bytes > MAX_EVENT_BYTES:
            _, old_size = self._events.popleft(); self._event_bytes -= old_size

    def _replace_live_item(self, turn_id, item, *, completed=False):
        value = {'turn_id': turn_id, 'item': _clone(item), 'display_truncated': False,
                 'chunks': {}, 'chunk_count': 0, 'completed': completed}
        size = _bytes(value)
        if size > MAX_ITEM_BYTES:
            value['item'] = {key: item[key] for key in ('id', 'type', 'status') if key in item}
            value['display_truncated'] = True
            size = _bytes(value)
            self._live_incomplete = True
        key = (turn_id, item['id'])
        old = self._live.get(key)
        if old is not None:
            self._live_bytes -= old['cache_bytes']
        value['cache_bytes'] = size
        self._live[key] = value
        self._live_bytes += size

    def _project_item(self, params, method):
        turn_id, item = params.get('turnId'), params.get('item')
        if method in ('item/started', 'item/completed') and type(item) is dict:
            item_id = item.get('id')
            if type(turn_id) is str and type(item_id) is str:
                self._replace_live_item(turn_id, item, completed=method == 'item/completed')
        elif method == 'item/fileChange/patchUpdated':
            item_id, changes = params.get('itemId'), params.get('changes')
            if type(turn_id) is str and type(item_id) is str and type(changes) is list:
                current = self._live.get((turn_id, item_id))
                if current is not None and current['item'].get('type') != 'fileChange':
                    self._live_incomplete = True
                elif current is None or not current.get('completed', False):
                    # A patch may be the first observed event. Preserve only
                    # its actual fields; it supplies no execution status.
                    updated = _clone(current['item']) if current else {'id': item_id, 'type': 'fileChange'}
                    updated['changes'] = _clone(changes)
                    self._replace_live_item(turn_id, updated)
        elif method in ('item/agentMessage/delta', 'item/plan/delta', 'item/commandExecution/outputDelta'):
            key = (turn_id, params.get('itemId'))
            current = self._live.get(key)
            delta = params.get('delta')
            if current is not None and type(delta) is str and not current['display_truncated']:
                field = 'aggregatedOutput' if method == 'item/commandExecution/outputDelta' else 'text'
                old = current['item'].get(field, '')
                if type(old) is str:
                    # Count only this new chunk; no serialization/copy of the
                    # accumulated answer or whole cache for each token.
                    addition = len(delta.encode('utf-8')) + 32
                    if current['cache_bytes'] + addition <= MAX_ITEM_BYTES and current['chunk_count'] < MAX_CHUNKS:
                        current['chunks'].setdefault(field, []).append(delta)
                        current['cache_bytes'] += addition; self._live_bytes += addition
                        current['chunk_count'] += 1
                    else:
                        current['display_truncated'] = True; self._live_incomplete = True
        while len(self._live) > MAX_LIVE_ITEMS or self._live_bytes > MAX_LIVE_BYTES:
            _, old = self._live.popitem(last=False)
            self._live_bytes -= old['cache_bytes']; self._live_incomplete = True

    def _live_snapshot(self):
        result = []
        for value in self._live.values():
            item = _clone(value['item'])
            for field, chunks in value['chunks'].items():
                item[field] = item.get(field, '') + ''.join(chunks)
            result.append({'turn_id': value['turn_id'], 'item': item,
                           'display_truncated': value['display_truncated']})
        return result

    def _request_snapshot(self, session):
        pending = session.pending_request_snapshots()
        if len(pending) > MAX_REQUESTS or _bytes(pending) > MAX_REQUEST_BYTES:
            with self._cache:
                self._state, self._error = 'failed', 'interaction_unavailable'
            raise SpecialistServiceError('interaction_unavailable')
        return pending

    def _sync_tokens(self, pending):
        current = {_request_key(snapshot['request']['id']): snapshot for snapshot in pending}
        for key, token in list(self._keys.items()):
            if key not in current:
                self._keys.pop(key, None); self._tokens.pop(token, None)
        result = []
        for key, snapshot in current.items():
            request = snapshot['request']
            if snapshot.get('ownership_confirmed', True) is not True:
                # The event is real, but its turn has not yet been linked to
                # the exact unknown receipt by explicit reconciliation.
                result.append({'token': None, 'state': 'ownership_unconfirmed',
                               'request': _clone(request)})
                continue
            token = self._keys.get(key)
            record = self._tokens.get(token)
            matched = (record is not None and record['generation'] == snapshot['generation']
                       and record['request_sha256'] == native_request_sha256(request))
            result.append({'token': token if matched else None,
                'state': record.get('answer', {}).get('state', 'pending') if matched else 'awaiting_event_registration',
                'request': _clone(request)})
        return result

    def _register_request(self, request, generation):
        # Only the sole reader grants a capability for this actual event. A
        # stale GET snapshot cannot mint or pair a new token with old text.
        key = _request_key(request['id'])
        if type(generation) is not int or generation < 1:
            raise SpecialistServiceError('invalid_native_request_generation')
        if key in self._submitted_keys:
            raise SpecialistServiceError('native_request_identity_ambiguous')
        previous = self._keys.pop(key, None)
        if previous is not None:
            self._tokens.pop(previous, None)
        token = self._epoch + '.' + secrets.token_hex(16)
        self._keys[key] = token
        self._tokens[token] = {'key': key, 'epoch': self._epoch,
                              'generation': generation,
                              'turn_id': (request.get('params') or {}).get('turnId'),
                              'request_sha256': native_request_sha256(request)}
        self._request_revision += 1

    def _expire_turn_tokens(self, turn_id):
        # The controller has already expired this exact routed native turn.
        # Invalidate a GET snapshot taken before that event was consumed.
        for key, token in list(self._keys.items()):
            record = self._tokens.get(token)
            if record is not None and record.get('turn_id') == turn_id:
                self._keys.pop(key, None); self._tokens.pop(token, None)
                if self._submitted_keys.get(key) == token:
                    self._submitted_keys.pop(key, None)
        self._request_revision += 1

    def _read_events(self, session, epoch, stop):
        while not stop.is_set():
            try:
                value = session.next_event(timeout=.1)
            except TimeoutError:
                stop.wait(.01)
                continue
            except BaseException as error:
                with self._cache:
                    if not stop.is_set() and epoch == self._epoch:
                        self._state, self._error = 'failed', _safe_failure(error)
                return
            try:
                kind = value.get('type')
                pending = self._request_snapshot(session) if kind == 'interaction' else None
                terminal = None
                if kind == 'notification' and value['event'].get('method') == 'turn/completed':
                    params = value['event'].get('params', {})
                    turn = params.get('turn', {})
                    if turn.get('id'):
                        terminal = self.store.find_turn(self.session_key, turn['id'])
                        if terminal and terminal.get('thread_id') != params.get('threadId'):
                            terminal = None
                with self._cache:
                    if stop.is_set() or epoch != self._epoch:
                        return
                    if pending is not None:
                        self._register_request(value['request'], value['request_generation'])
                        self._sync_tokens(pending)
                        self._push({'type': 'requests_changed'})
                    elif kind == 'notification':
                        event = value['event']; params = event.get('params', {})
                        method = event.get('method')
                        self._project_item(params, method)
                        turn = params.get('turn') or {}
                        if (method == 'turn/completed' and type(turn.get('id')) is str
                                and turn.get('status') in ('completed', 'failed', 'interrupted')):
                            self._expire_turn_tokens(turn['id'])
                        if terminal and terminal['client_id'] == self._turn_client_id:
                            self._turn_state = {'turn_id': terminal['turn_id'], 'status': terminal['status'],
                                                'background_tools_exit_verified': False}
                            self._turn_observed_client_id = terminal['client_id']
                        if method == 'serverRequest/resolved':
                            key = _request_key(params.get('requestId'))
                            submitted = self._submitted_keys.pop(key, None)
                            pending_token = self._keys.pop(key, None)
                            for token in (submitted, pending_token):
                                if token is not None:
                                    self._tokens.pop(token, None)
                                    if token in self._answers:
                                        self._answers[token]['resolved_by_server'] = True
                            self._request_revision += 1
                        self._push(value)
                    elif kind in ('unsupported_interaction', 'out_of_scope_interaction'):
                        self._error = 'native_interaction_failed'
                        self._push(value)
                    else:
                        self._push(value)
            except BaseException as error:
                with self._cache:
                    if not stop.is_set() and epoch == self._epoch:
                        self._state, self._error = 'failed', _safe_failure(error)
                return

    def _receipts(self):
        rows = OrderedDict()
        for client_id in (self._create_id, *self._clients):
            row = self.store.read_operation(client_id, session_key=self.session_key)
            if row is not None:
                rows[client_id] = row
        for row in self.store.pending_operations(self.session_key):
            rows[row['client_id']] = row
        return list(rows.values())

    def view(self, *, cursor=None, turn_id=None, after_seq=None, include_items=True):
        with self._actions:
            if (type(include_items) is not bool
                    or (not include_items and (cursor is not None or turn_id is not None))):
                raise SpecialistServiceError('invalid_items_inclusion')
            if after_seq is not None and (type(after_seq) is not int or not 0 <= after_seq <= self._seq):
                raise SpecialistServiceError('invalid_display_cursor')
            binding = self.store.read_binding(self.session_key)
            history = turns = native = None
            history_state = {key: {'available': None, 'error': None}
                             for key in ('items', 'turns')}
            pending = []
            request_revision = None
            if self._state == 'ready' and self._session is not None and self._rpc.running:
                if binding.get('thread_id') is not None:
                    native = self._session.read_thread()
                    def page(key, method, read):
                        try:
                            result = read()
                        except RemoteError as error:
                            if error.code not in (-32601, -32600):
                                raise
                            history_state[key] = {'available': False,
                                'error': {'method': method, 'code': error.code}}
                            return None
                        history_state[key]['available'] = True
                        return result
                    # Each history contract can be unavailable independently.
                    # A known native pagination error never revokes execution,
                    # current approvals or the ability to interrupt an own turn.
                    if include_items:
                        history = page('items', 'thread/items/list', lambda:
                            self._session.items_page(cursor=cursor, turn_id=turn_id))
                    turns = page('turns', 'thread/turns/list', lambda:
                        self._session.turns_page(items_view='notLoaded'))
                with self._cache:
                    request_revision = self._request_revision
                try:
                    pending = self._request_snapshot(self._session)
                except SpecialistServiceError:
                    pass  # Return an explicit failed/unavailable snapshot.
            receipts = self._receipts()
            with self._cache:
                available = self._connection()['state'] == 'ready'
                if available:
                    self._project_turn(binding, receipts)
                changed = request_revision is not None and request_revision != self._request_revision
                if available and changed:
                    requests = [{'token': None, 'state': 'request_snapshot_changed',
                                 'request': _clone(snapshot['request'])} for snapshot in pending]
                else:
                    requests = self._sync_tokens(pending) if available else None
                first = self._events[0][0]['seq'] if self._events else self._seq + 1
                reset = changed or after_seq is None or after_seq < first - 1
                events = [] if reset else [record for record, _ in self._events if record['seq'] > after_seq]
                result = {'connection': self._connection(), 'binding': binding, 'native': native,
                    'items_included': include_items,
                    'history_page': history, 'turns_page': turns, 'history_state': history_state,
                    'live_items': self._live_snapshot(),
                    'live_cache_incomplete': self._live_incomplete, 'requests': requests,
                    'requests_available': available, 'answers': list(self._answers.values()),
                    'events': events, 'last_seq': self._seq, 'reset_required': reset,
                    'turn_state': self._turn_state,
                    'disconnect_unconfirmed': self._disconnect_unconfirmed}
                result = _clone(result)
            result['receipts'] = receipts
            if self._fixed_options is not None:
                result['requested_model_policy'] = dict(self._fixed_options,
                    applies_to='each_turn', execution_verified=False)
            return result

    def models(self):
        """Read the complete visible native catalog from this explicit connection.

        This never opens a connection, creates a thread or changes settings.
        An incomplete/invalid catalog is an error, not a truncated picker.
        """
        with self._actions:
            self.require_ready()
            rows, cursor, seen = [], None, set()
            for _ in range(MAX_MODEL_PAGES):
                params = {'includeHidden': False, 'limit': MODEL_PAGE_SIZE}
                if cursor is not None:
                    params['cursor'] = cursor
                page = self._rpc.request('model/list', params, timeout=15)
                if (type(page) is not dict or type(page.get('data')) is not list
                        or len(page['data']) > MODEL_PAGE_SIZE):
                    raise SpecialistServiceError('invalid_native_model_catalog')
                rows.extend(_model_row(row) for row in page['data'])
                cursor = page.get('nextCursor')
                if cursor is None:
                    return {'data': rows, 'nextCursor': None}
                if not _model_string(cursor, limit=4096) or cursor in seen:
                    raise SpecialistServiceError('invalid_native_model_catalog')
                seen.add(cursor)
            raise SpecialistServiceError('native_model_catalog_incomplete')

    def tools(self, *, thread_id, epoch):
        """Explicit MCP metadata read; never opens, creates, reloads or calls tools."""
        validate_tool_scope(thread_id, epoch)
        with self._actions:
            if self.session_key != 'specialist/codex':
                raise SpecialistToolStatusError('scope_changed')
            self.require_ready()

            def verify():
                self.require_ready()
                self._session._ready()
                binding = self.store.read_binding(self.session_key)
                if (self._epoch != epoch or binding.get('thread_id') != thread_id
                        or binding.get('cwd') != self.cwd):
                    raise SpecialistToolStatusError('scope_changed')

            verify()
            result = read_tool_status(self._rpc.request, thread_id=thread_id, epoch=epoch)
            verify()
            return result

    def send(self, client_id, inputs, *, options=None):
        with self._actions:
            # Reject options before any receipt, watch or native RPC. The
            # ordinary holder still inherits defaults. Only the trusted
            # specialist connection pins this exact pair on EVERY turn: resume
            # alone does not change an old thread's saved model/effort.
            options = validate_model_options(options)
            if self._fixed_options is not None:
                if any(value != self._fixed_options[key] for key, value in options.items()):
                    raise SpecialistServiceError('fixed_specialist_model_options')
                options = dict(self._fixed_options)
            try:
                self.require_ready()
            except SpecialistServiceError as error:
                # This failure precedes watch, thread creation and turn/start.
                # Only absence in the authoritative native ledger proves this
                # client is new. An older accepted/unknown operation, invalid
                # client, other owner or unreadable ledger never acquires the
                # stronger meaning "the original message was not sent".
                if (error.reason in ('specialist_not_ready', 'specialist_closed')
                        and self.store.read_operation(client_id, session_key=self.session_key) is None):
                    raise SpecialistSendNotReady() from None
                raise
            inputs = _clone(inputs)
            if type(inputs) is not list or not inputs or any(type(item) is not dict for item in inputs):
                raise SpecialistServiceError('invalid_native_input')
            previous = self._watch(client_id)
            previous_turn_client = self._turn_client_id
            if previous is None:
                with self._cache:
                    self._turn_client_id = client_id
            if self._session.binding().get('thread_id') is None:
                prior_creation = self.store.read_operation(self._create_id, session_key=self.session_key)
                try:
                    thread_options = {'historyMode': 'paginated'}
                    if self._fixed_options is not None:
                        thread_options['model'] = self._fixed_options['model']
                    if self._developer_instructions is not None:
                        thread_options['developerInstructions'] = self._developer_instructions
                    created = self._session.create_thread(self._create_id, options=thread_options)
                except RemoteError as error:
                    # The native controller persists its explicit rejection
                    # before raising. Verify only this creation attempt; other
                    # RemoteErrors must not acquire a generic unsent meaning.
                    rejected = self.store.read_operation(self._create_id, session_key=self.session_key)
                    if (prior_creation is None
                            and self._thread_rejection_proves_unsent(rejected, client_id=client_id, previous=previous)
                            and rejected['rpc_error_code'] == error.code):
                        self._restore_unsent_client(client_id, previous_turn_client)
                        raise SpecialistSendNotSubmitted() from None
                    raise
                if created['receipt']['status'] != 'accepted' or not created['receipt'].get('thread_id'):
                    if self._thread_rejection_proves_unsent(created['receipt'], client_id=client_id, previous=previous):
                        self._restore_unsent_client(client_id, previous_turn_client)
                        raise SpecialistSendNotSubmitted()
                    raise SpecialistServiceError('thread_creation_unconfirmed')
            try:
                result = (self._session.send(client_id, inputs, options=options)
                    if options else self._session.send(client_id, inputs))
            except NativeSessionStoreError as error:
                if previous is None and error.code == 'busy':
                    with self._cache:
                        if self._turn_client_id == client_id:
                            self._turn_client_id = previous_turn_client
                raise
            receipt = result['receipt']
            with self._cache:
                if receipt.get('turn_id') and not result.get('reused'):
                    terminal_seen = (self._turn_state is not None and
                        self._turn_state['turn_id'] == receipt['turn_id'] and
                        self._turn_state['status'] in ('completed', 'failed', 'interrupted'))
                    if not terminal_seen:
                        status = result.get('native', {}).get('turn', {}).get('status', receipt['status'])
                        self._turn_state = {'turn_id': receipt['turn_id'], 'status': status,
                                            'background_tools_exit_verified': False}
                        self._turn_observed_client_id = client_id
            return result

    def answer(self, token, result):
        with self._actions:
            self.require_ready()
            result = _clone(result)
            if type(result) is not dict or type(token) is not str:
                raise SpecialistServiceError('invalid_native_answer')
            digest = _fingerprint(result)
            with self._cache:
                record = self._tokens.get(token)
                previous = self._answers.get(token) or (record.get('answer') if record else None)
                if previous is not None:
                    if previous['result_sha256'] != digest:
                        raise SpecialistServiceError('native_answer_conflict')
                    return dict(previous, reused=True)
                if record is None or record['epoch'] != self._epoch:
                    raise SpecialistServiceError('native_request_expired')
                key = record['key']
            pending = self._request_snapshot(self._session)
            current = next((snapshot for snapshot in pending
                            if _request_key(snapshot['request']['id']) == key), None)
            if (current is None or current['generation'] != record['generation']
                    or native_request_sha256(current['request']) != record['request_sha256']):
                raise SpecialistServiceError('native_request_expired')
            if current.get('ownership_confirmed', True) is not True:
                # A guessed/previously observed RAM token must not burn a
                # decision before its original turn ownership is confirmed.
                raise SpecialistServiceError('native_request_ownership_unconfirmed')
            answer = {'token': token, 'state': 'submitted_unknown', 'result_sha256': digest,
                      'response_submitted': False, 'resolved_by_server': False}
            with self._cache:
                # The sole reader can revoke this request after its controller
                # snapshot was read. Do not resurrect a removed capability.
                if (self._tokens.get(token) is not record or self._keys.get(key) != token
                        or record['epoch'] != self._epoch):
                    raise SpecialistServiceError('native_request_expired')
                self._answers[token] = answer
                record['answer'] = answer
                self._submitted_keys[key] = token
                while len(self._answers) > MAX_ANSWERS:
                    old_token, _ = self._answers.popitem(last=False)
                    for old_key, mapped in list(self._submitted_keys.items()):
                        if mapped == old_token:
                            self._submitted_keys.pop(old_key, None)
            # The controller atomically checks native request identity and its
            # original scope through reply, using its public optional guard.
            # Burn before the one-way write. Failure never permits retry.
            response = self._session.reply(key[1], result,
                expected_request_sha256=record['request_sha256'],
                expected_request_generation=record['generation'])
            with self._cache:
                resolved = answer['resolved_by_server']
                answer.update(state='response_submitted', **response)
                answer['resolved_by_server'] = resolved or answer['resolved_by_server']
                return dict(answer, reused=False)

    def control(self, action, client_id, *, turn_id=None):
        with self._actions:
            self.require_ready()
            if action not in ('interrupt', 'reconcile'):
                raise SpecialistServiceError('invalid_specialist_control')
            self._watch(client_id)
            if action == 'interrupt':
                result = self._session.interrupt(client_id, turn_id)
            else:
                if turn_id is not None:
                    raise SpecialistServiceError('invalid_specialist_control')
                result = self._session.reconcile(client_id)
                proof = result.get('proof', {})
                if result.get('reconciled') is True:
                    with self._cache:
                        self._turn_client_id = result['receipt']['client_id']
                        self._turn_state = {'turn_id': proof.get('turn_id'),
                            'status': proof.get('native_status'), 'background_tools_exit_verified': False}
                        self._turn_observed_client_id = result['receipt']['client_id']
            return dict(result, background_tools_exit_verified=False)

    def close(self):
        with self._actions:
            self._closed = True
            with self._cache:
                self._state = 'closed'
                self._tokens.clear(); self._keys.clear()
            self._dispose()
            self._disconnect_unconfirmed = False

    def disconnect(self):
        """Release a proven-idle executor, keeping its native thread and policy.

        This is explicit user work, never a maintenance probe or auto-drain.
        A failed cleanup retains the owner and may only be retried explicitly;
        open cannot manufacture another executor behind an uncertain shutdown.
        """
        with self._actions:
            if self._closed:
                raise SpecialistServiceError('specialist_closed')
            if self.store.has_unfinished_operations(self.session_key):
                raise SpecialistServiceError('specialist_disconnect_pending')
            if not self._disconnect_unconfirmed:
                value = self.view(include_items=False)
                active = value.get('turn_state') or {}
                state = value['connection']['state']
                bound = value['binding'].get('thread_id') is not None
                native_status = ((value.get('native') or {}).get('thread') or {}).get('status') or {}
                with self._cache:
                    unanswered = any(not row.get('resolved_by_server', False)
                        and (token in self._tokens or token in self._submitted_keys.values())
                        for token, row in self._answers.items())
                if (state not in ('not_open', 'ready') or value.get('requests') or unanswered
                        or active.get('status') not in (None, 'completed', 'failed', 'interrupted')
                        or state == 'ready' and (value.get('requests_available') is not True
                            or bound and native_status.get('type') != 'idle')):
                    raise SpecialistServiceError('specialist_disconnect_pending')
            try:
                self._dispose(retain_on_error=True)
            except BaseException:
                with self._cache:
                    self._disconnect_unconfirmed = True
                    self._state, self._error = 'failed', 'native_shutdown_unconfirmed'
                raise
            with self._cache:
                self._disconnect_unconfirmed = False
                self._state, self._error = 'not_open', None
            return {'disconnected': True, 'background_tools_exit_verified': False}
