"""Thin local envelope; official Hermes owns sessions, tools and execution.

The worker's event_seq is a transport barrier, separate from native event.seq.
No reader is added: exactly one owner consumes the existing bounded JSONL queue.
"""
import threading

from ..process import observe_process_group as _live_group

from ..codex_rpc import (CodexRpc, CodexRpcError as HermesRpcError,
                       TransportError as HermesTransportError,
                       SubmittedTimeout as HermesSubmittedTimeout,
                       KnownUnsentTimeout as HermesKnownUnsentTimeout, validate_timeout as _timeout)


class HermesRemoteError(HermesRpcError):
    def __init__(self, code, message, data):
        super().__init__('Native Hermes rejected the request.')
        self.code, self.native_message, self.data = code, message, data
        self.result_unknown = False


class HermesRpc:
    def __init__(self, command, *, cwd=None, env=None, max_events=1024):
        self._wire = CodexRpc(command, cwd=cwd, env=env, max_events=max_events)
        self._lock = threading.RLock()
        self._ready = False
        self._native_close_confirmed = False
        self._group_empty = None
        self._events_received = self._events_consumed = 0
        self._consumer = None

    @property
    def started(self):
        return self._wire.started

    @property
    def running(self):
        return self._ready and self._wire.running

    @property
    def stats(self):
        with self._lock:
            return {**self._wire.stats, 'events_received': self._events_received,
                    'graceful_close': self._native_close_confirmed,
                    'owned_group_empty': self._group_empty}

    def _result(self, result):
        if (type(result) is not dict or type(result.get('event_seq')) is not int
                or not 0 <= result['event_seq'] <= (1 << 53) - 1
                or type(result.get('ok')) is not bool):
            raise HermesTransportError('invalid_hermes_envelope', result_unknown=True)
        with self._lock:
            self._events_received = max(self._events_received, result['event_seq'])
        if result['ok']:
            if 'value' not in result:
                raise HermesTransportError('invalid_hermes_envelope', result_unknown=True)
            return result['value']
        if result.get('kind') == 'remote' and type(result.get('error')) is dict:
            error = result['error']
            raise HermesRemoteError(error.get('code'), error.get('message'), error.get('data'))
        raise HermesTransportError('native_request_failed', result_unknown=result.get('result_unknown') is True)

    def start(self):
        with self._lock:
            if self._ready:
                return self
            self._wire.start()
            try:
                ready = self._result(self._wire.request('bridge/ready', timeout=35))
                if ready != {'ready': True}:
                    raise HermesTransportError('native_not_ready')
                self._ready = True
            except Exception:
                self._wire.close()
                raise
        return self

    def request(self, method, params=None, timeout=20):
        seconds = _timeout(timeout)
        if type(method) is not str or not method or len(method) > 256 or '\0' in method:
            raise ValueError('A bounded native method is required.')
        if seconds > 3600:
            raise ValueError('Native timeout exceeds one hour.')
        # Leave a small local-envelope margin; neither layer retries the request.
        result = self._wire.request('bridge/request', {'method': method,
            'params': {} if params is None else params, 'timeout_ms': max(1, int(seconds * 1000) - 25)}, timeout=seconds)
        return self._result(result)

    def next_event(self, timeout=None):
        with self._lock:
            ident = threading.get_ident()
            if self._consumer is not None and self._consumer != ident:
                raise ValueError('Only one native event consumer is allowed.')
            self._consumer = ident
        frame = self._wire.next_event(timeout=timeout)
        p = frame.get('params')
        if (frame.get('method') != 'hermes/event' or 'id' in frame or type(p) is not dict
                or type(p.get('event_seq')) is not int or type(p.get('event')) is not dict):
            raise HermesTransportError('invalid_hermes_event')
        with self._lock:
            if p['event_seq'] != self._events_consumed + 1:
                raise HermesTransportError('invalid_hermes_event_order')
            self._events_consumed = p['event_seq']
            self._events_received = max(self._events_received, p['event_seq'])
        return p['event']

    def close(self):
        if self._wire.running:
            try:
                self._native_close_confirmed = self._result(self._wire.request('bridge/close', timeout=6.5)) == {'closed': True}
            except HermesRpcError:
                pass  # Existing transport still verifies and closes its owned group.
        self._wire.close()
        self._ready = False
        state = self._wire.stats
        group = state['owned_group_id']
        # Only this POSIX group is observed; detached tools are outside this proof.
        live = False if group is None and not self._wire.started else _live_group(group) if group is not None else None
        self._group_empty = None if live is None else not live
        if not state['process_exited'] or not state['io_threads_stopped'] or not self._group_empty:
            raise HermesTransportError('native_cleanup_unconfirmed')
