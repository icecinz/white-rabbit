"""Bounded parent envelope for the official SDK worker; exactly one event reader.

This layer never chooses a model, reads history, retries a query or approves a
tool. The mature JSONL process transport also owns physical group cleanup.
"""
import threading

from ..process import observe_process_group as _live_group
from ..codex_rpc import (CodexRpcError as ClaudeRpcError,
    TransportError as ClaudeTransportError, validate_timeout as _timeout,
    SubmittedTimeout as ClaudeSubmittedTimeout, KnownUnsentTimeout as ClaudeKnownUnsentTimeout)
from .material_protocol import MAX_RPC_FRAME_BYTES, MAX_RPC_EVENT_BYTES
from ..native_rpc_transport import BoundedRpc as _BoundedWire


class ClaudeRemoteError(ClaudeRpcError):
    def __init__(self, code, *, result_unknown=False):
        super().__init__('Native Claude rejected the request.')
        self.code, self.result_unknown = code, result_unknown


class ClaudeRpc:
    def __init__(self, command, *, cwd=None, env=None, max_events=512,
                 max_frame_bytes=MAX_RPC_FRAME_BYTES, max_event_bytes=MAX_RPC_EVENT_BYTES):
        if type(max_event_bytes) is not int or max_event_bytes <= 0:
            raise ValueError('Event bytes must have a positive bound.')
        self._wire = _BoundedWire(command, cwd=cwd, env=env, max_events=max_events,
                                 max_frame_bytes=max_frame_bytes, max_event_bytes=max_event_bytes)
        self._lock, self._consumer = threading.RLock(), None
        self._ready, self._graceful, self._group_empty = False, False, None
        self._received = self._consumed = 0
        self.ready_info = None

    @property
    def started(self):
        return self._wire.started

    @property
    def running(self):
        return self._ready and self._wire.running

    @property
    def stats(self):
        with self._lock:
            return {**self._wire.stats, 'events_received': self._received,
                'graceful_close': self._graceful, 'owned_group_empty': self._group_empty}

    def _result(self, result):
        if (type(result) is not dict or type(result.get('event_seq')) is not int
                or not 0 <= result['event_seq'] <= (1 << 53) - 1 or type(result.get('ok')) is not bool):
            raise ClaudeTransportError('invalid_claude_envelope', result_unknown=True)
        with self._lock:
            self._received = max(self._received, result['event_seq'])
        if result['ok'] and 'value' in result:
            return result['value']
        if not result['ok'] and type(result.get('error')) is dict:
            error = result['error']
            raise ClaudeRemoteError(error.get('code'), result_unknown=error.get('result_unknown') is True)
        raise ClaudeTransportError('invalid_claude_envelope', result_unknown=True)

    def start(self):
        with self._lock:
            if self._ready:
                return self
            self._wire.start()
            try:
                value = self._result(self._wire.request('bridge/ready', timeout=60))
                if (type(value) is not dict or value.get('ready') is not True
                        or type(value.get('schema_version')) is not int or value['schema_version'] != 1):
                    raise ClaudeTransportError('native_not_ready')
                self.ready_info, self._ready = value, True
            except Exception:
                self._wire.close()
                raise
        return self

    def request(self, method, params=None, timeout=20):
        seconds = _timeout(timeout)
        if type(method) is not str or method not in (
                'session.info', 'session/history', 'session/history.page', 'session/history.record',
                'model.options', 'prompt.submit', 'interrupt', 'permission.respond'):
            raise ValueError('Unsupported native Claude method.')
        if seconds > 3600:
            raise ValueError('Native timeout exceeds one hour.')
        return self._result(self._wire.request('bridge/request', {'method': method,
            'params': {} if params is None else params}, timeout=seconds))

    def next_event(self, timeout=None):
        with self._lock:
            ident = threading.get_ident()
            if self._consumer is not None and self._consumer != ident:
                raise ValueError('Only one native event consumer is allowed.')
            self._consumer = ident
        frame = self._wire.next_event(timeout)
        params = frame.get('params')
        if (frame.get('method') != 'claude/event' or 'id' in frame or type(params) is not dict
                or set(params) != {'event_seq', 'event'} or type(params['event_seq']) is not int
                or type(params['event']) is not dict):
            raise ClaudeTransportError('invalid_claude_event')
        with self._lock:
            if params['event_seq'] != self._consumed + 1:
                raise ClaudeTransportError('invalid_claude_event_order')
            self._consumed = params['event_seq']
            self._received = max(self._received, params['event_seq'])
        return params['event']

    def close(self):
        if self._wire.running:
            try:
                proof = self._result(self._wire.request('bridge/close', timeout=35))
                self._graceful = (type(proof) is dict and proof.get('closed') is True
                    and proof.get('sdk_disconnected') is True and proof.get('native_process_exited') is True
                    and proof.get('native_io_closed') is True)
            except ClaudeRpcError:
                pass
        self._wire.close()
        self._ready = False
        state, group = self._wire.stats, self._wire.stats['owned_group_id']
        live = False if group is None and not self._wire.started else _live_group(group) if group is not None else None
        self._group_empty = None if live is None else not live
        if not state['process_exited'] or not state['io_threads_stopped'] or self._group_empty is not True:
            raise ClaudeTransportError('native_cleanup_unconfirmed')
