"""One claimed task -> one existing specialist send; explicit read-only sync.

The host supplies the exact ledger, fixed specialist, desktop, pure renderer,
and fixed model/effort send options. A new claim freezes the full native request
SHA using the existing native ledger codec; same client/thread alone is not
proof that a receipt belongs to the rendered task. Only an already bound thread
may be used; an unbound connection is explicitly not ready.

The host also supplies an admission context-manager factory and must bind those objects to the
same application workspace and share admission with ordinary specialist sends
and maintenance. Admission is a reservation, not a mutex held across native
tools/MCP callbacks. This module neither provides system desktop exclusion nor
proves background-tool shutdown; a ledger terminal state is not permission.

No holder construction/open, RPC reader, retry loop, model selection, external
application adapter, HTTP or MCP lives here. The renderer returns text only;
materials are explicitly unsupported. Human-only closing and not_sent are not
exposed. ``sync`` reads native metadata, never native history or application UI,
and never changes the ledger or attaches a receipt behind the caller's back.
"""
from copy import deepcopy
import re

from .interface_tasks import InterfaceTasks, InterfaceTaskError
from rabbit_runtime.native_session_store import NativeSessionStore, NativeSessionStoreError, validate_native_id as _native_id, request_sha256
from rabbit_runtime.specialist_service import validate_model_options as _model_options, SpecialistServiceError


SESSION_KEY = 'specialist/codex'
MAX_RENDERED_BYTES = 256 * 1024
_SHA = re.compile(r'[a-f0-9]{64}\Z')
_DESKTOP = re.compile(r'[A-Za-z0-9_.:/-]{1,160}\Z')
_NATIVE_TERMINAL = frozenset(('completed', 'failed', 'interrupted'))
_NATIVE_STATUSES = _NATIVE_TERMINAL | {'submitted_unknown', 'accepted', 'rejected'}


class InterfaceTaskCoordinatorError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__('Interface task coordinator: ' + code)


def _fail(code):
    raise InterfaceTaskCoordinatorError(code)


class InterfaceTaskCoordinator:
    def __init__(self, tasks, holder, *, desktop_id, renderer, admission, send_options):
        if (not isinstance(tasks, InterfaceTasks) or not isinstance(getattr(holder, 'store', None), NativeSessionStore)
                or type(desktop_id) is not str or _DESKTOP.fullmatch(desktop_id) is None
                or not callable(renderer) or not callable(admission) or not callable(getattr(holder, 'send', None))):
            _fail('invalid_configuration')
        try:
            self._send_options = _model_options(send_options)
        except SpecialistServiceError:
            _fail('invalid_configuration')
        if self._send_options.keys() != {'model', 'effort'}: _fail('invalid_configuration')
        self._tasks, self._holder, self._native = tasks, holder, holder.store
        self._workspace, self._desktop, self._cwd = tasks.workspace_id, desktop_id, getattr(holder, 'cwd', None)
        self._renderer, self._admission = renderer, admission
        self._scope_identity = self._initial_thread = None
        binding = self._scope()
        self._scope_identity = tuple(binding[key] for key in ('session_key', 'cwd', 'cwd_device', 'cwd_inode'))
        if binding['thread_id'] is not None:
            self._initial_thread = binding['thread_id'], binding['session_id']

    def _scope(self):
        if (getattr(self._holder, 'store', None) is not self._native or getattr(self._holder, 'session_key', None) != SESSION_KEY
                or getattr(self._holder, 'cwd', None) != self._cwd or self._tasks.workspace_id != self._workspace):
            _fail('scope_changed')
        try:
            binding = self._native.verify_scope(SESSION_KEY)
            if (binding['session_key'] != SESSION_KEY or binding['cwd'] != self._cwd
                    or self._scope_identity is not None and self._scope_identity != tuple(
                        binding[key] for key in ('session_key', 'cwd', 'cwd_device', 'cwd_inode'))
                    or self._initial_thread is not None
                        and self._initial_thread != (binding['thread_id'], binding['session_id'])):
                _fail('scope_changed')
            return binding
        except (NativeSessionStoreError, OSError, KeyError, TypeError):
            _fail('native_unavailable')

    def _task(self, task_id):
        task = self._tasks.get(task_id)
        if task is None: _fail('not_found')
        if task['workspace_id'] != self._workspace or task['desktop_id'] != self._desktop:
            _fail('scope_changed')
        return task

    def _existing(self, task_id, operation_id, expected_revision):
        value = self._tasks.claim_dispatch(task_id, operation_id, expected_revision, existing_only=True)
        if value is not None:
            self._task(task_id)  # An exact old ack still belongs to this fixed desktop.
        return value

    @staticmethod
    def _dispatch_result(claim, *, attempted=False, call=None, release_unconfirmed=False):
        return dict(claim, send_attempted=attempted, send_call=call,
            admission_release_unconfirmed=release_unconfirmed)

    def dispatch(self, task_id, operation_id, expected_revision):
        """Attempt one send only for a newly committed claim; never fill gaps.

        A known replay skips rendering, admission, native scope reads and send.
        A candidate which loses a race rechecks the exact old operation even if
        its new rendering/admission fails. An absent replay cannot create an
        intent in that failure path. send_call describes the local call only;
        callers explicitly sync the authoritative native receipt afterwards.
        """
        previous = self._existing(task_id, operation_id, expected_revision)
        if previous is not None: return self._dispatch_result(previous)
        claim = None
        attempted, call = False, None
        try:
            task = self._task(task_id)
            if task['phase'] != 'prepared': raise InterfaceTaskError('state_conflict')
            if task['revision'] != expected_revision: raise InterfaceTaskError('revision_conflict')
            if task['request']['material_refs']: _fail('materials_unsupported')
            if self._scope()['thread_id'] is None: _fail('native_not_ready')
            try:
                text = self._renderer(deepcopy(task))
                if type(text) is not str or not text.strip() or len(text.encode('utf-8', 'strict')) > MAX_RENDERED_BYTES:
                    _fail('invalid_render')
            except InterfaceTaskCoordinatorError:
                raise
            except Exception:
                _fail('invalid_render')
            inputs = [{'type': 'text', 'text': text}]
            with self._admission(deepcopy(task)):
                claim = self._existing(task_id, operation_id, expected_revision)
                if claim is None:
                    binding = self._scope()
                    if binding['thread_id'] is None: _fail('native_not_ready')
                    if self._native.read_operation(task['native_client_id'], session_key=SESSION_KEY) is not None:
                        _fail('native_client_in_use')
                    intent = request_sha256(dict(self._send_options, threadId=binding['thread_id'],
                        input=inputs, clientUserMessageId=task['native_client_id']))
                    claim = self._tasks.claim_dispatch(task_id, operation_id, expected_revision, native_intent_sha256=intent)
                if claim['created']:
                    attempted = True
                    try:
                        self._holder.send(task['native_client_id'], inputs, options=dict(self._send_options))
                        call = 'returned'  # Its body is deliberately not evidence.
                    except Exception:
                        call = 'unconfirmed'
            return self._dispatch_result(claim, attempted=attempted, call=call)
        except Exception as error:
            if claim is not None:
                # Admission cleanup can fail after submission. Keep the claim
                # and exact native client; do not attempt cleanup or another send.
                return self._dispatch_result(claim, attempted=attempted, call=call, release_unconfirmed=True)
            previous = self._existing(task_id, operation_id, expected_revision)
            if previous is not None: return self._dispatch_result(previous)
            if isinstance(error, (InterfaceTaskError, InterfaceTaskCoordinatorError, NativeSessionStoreError)):
                raise
            _fail('admission_failed')

    def sync(self, task_id):
        """Read this task's native operation only; never recover, attach or send."""
        task = self._task(task_id)
        before = self._scope()
        row = self._native.read_operation(task['native_client_id'], session_key=SESSION_KEY)
        after = self._scope()
        if tuple(before[key] for key in ('thread_id', 'session_id')) != tuple(after[key] for key in ('thread_id', 'session_id')):
            _fail('scope_changed')
        receipt = self._receipt(task, row, after)
        state = ('not_dispatched' if not task['dispatch_started'] else 'unconfirmed') if receipt is None else (
            'unconfirmed' if receipt['status'] == 'submitted_unknown' else 'native_' + receipt['status'])
        return {'schema_version': 1, 'task': task, 'native_receipt': receipt, 'handoff_state': state,
            'semantics': {'native_source': 'native_session_store', 'external_outcome_verified': False,
                'native_stop_proven': False, 'desktop_execution_authorized': False}}

    @staticmethod
    def _receipt(task, row, binding):
        prior = task['native_receipt']
        if row is None:
            if prior is not None: _fail('native_conflict')
            return None
        try:
            if (type(row) is not dict or not task['dispatch_started']
                    or row['client_id'] != task['native_client_id'] or row['session_key'] != SESSION_KEY
                    or row['kind'] != 'turn_start' or row['target_turn_id'] is not None
                    or type(row['status']) is not str or row['status'] not in _NATIVE_STATUSES
                    or type(row['request_sha256']) is not str or _SHA.fullmatch(row['request_sha256']) is None):
                _fail('native_conflict')
            if task['native_intent_sha256'] is None: _fail('native_intent_missing')
            if row['request_sha256'] != task['native_intent_sha256']: _fail('native_conflict')
            for key in ('thread_id', 'session_id', 'turn_id'):
                _native_id(row[key], optional=True)
            if row['status'] in ('submitted_unknown', 'rejected'):
                if any(row[key] is not None for key in ('thread_id', 'session_id', 'turn_id')): _fail('native_conflict')
            elif (row['thread_id'] is None or row['turn_id'] is None
                    or (row['thread_id'], row['session_id']) != (binding['thread_id'], binding['session_id'])):
                _fail('native_conflict')
            receipt = {'native_client_id': row['client_id'], **{key: row[key] for key in
                ('session_key', 'kind', 'thread_id', 'session_id', 'turn_id', 'request_sha256', 'status')}}
            if prior is not None and (any(prior[key] is not None and prior[key] != receipt[key] for key in
                    ('native_client_id', 'thread_id', 'turn_id', 'request_sha256'))
                    or prior['status'] in _NATIVE_TERMINAL | {'rejected'} and prior['status'] != receipt['status']
                    or prior['status'] == 'accepted' and receipt['status'] in ('submitted_unknown', 'rejected')):
                _fail('native_conflict')
            return receipt
        except (KeyError, TypeError, NativeSessionStoreError):
            _fail('native_conflict')
