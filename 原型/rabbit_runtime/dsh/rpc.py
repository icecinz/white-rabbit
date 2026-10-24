"""Bounded envelope around the original DSH profile's private IPC carrier.

Native RPC responses retain rpcId/result. This layer does not retry prompts,
approve interactions, select models or copy transcripts into an application DB.
"""
import json
import threading

from ..process import observe_process_group
from ..codex_rpc import (CodexRpc, CodexRpcError as DshRpcError, ProtocolError,
    TransportError as DshTransportError, SubmittedTimeout as DshSubmittedTimeout,
    KnownUnsentTimeout as DshKnownUnsentTimeout, validate_timeout as _timeout)


class _BoundedWire(CodexRpc):
    def __init__(self, *args, max_event_bytes, **kwargs):
        super().__init__(*args, **kwargs)
        self._event_bytes, self._max_event_bytes = 0, max_event_bytes

    @property
    def stats(self):
        with self._lock:
            return {**super().stats, 'queued_event_bytes': self._event_bytes}

    @staticmethod
    def _size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8'))

    def _dispatch(self, frame):
        with self._changed:
            size = self._size(frame) if type(frame) is dict and 'method' in frame else 0
            if self._event_bytes + size > self._max_event_bytes:
                raise ProtocolError('event_byte_limit')
            before = len(self._events)
            super()._dispatch(frame)
            if len(self._events) > before:
                self._event_bytes += size

    def next_event(self, timeout=None):
        with self._changed:
            frame = super().next_event(timeout)
            self._event_bytes -= self._size(frame)
            return frame

    def _fail(self, error, **kwargs):
        super()._fail(error, **kwargs)
        with self._lock:
            self._event_bytes = 0


METHODS = {'host.describe', 'agentPreset.list', 'session.create', 'session.resume', 'session.info',
           'session.inspect', 'session.flush', 'session.history', 'session.models', 'session.prompt',
           'session.cancel', 'respond'}


class DshRpc:
    def __init__(self, command, *, cwd=None, env=None, max_events=512,
                 max_frame_bytes=2 * 1024 * 1024, max_event_bytes=16 * 1024 * 1024):
        if type(max_event_bytes) is not int or max_event_bytes <= 0:
            raise ValueError('Event bytes must have a positive bound.')
        self._wire = _BoundedWire(command, cwd=cwd, env=env, max_events=max_events,
            max_frame_bytes=max_frame_bytes, max_event_bytes=max_event_bytes)
        self._lock, self._consumer = threading.RLock(), None
        self._ready, self._graceful, self._group_empty = False, False, None
        self._received = self._consumed = 0
        self.ready_info = None

    @property
    def started(self): return self._wire.started

    @property
    def running(self): return self._ready and self._wire.running

    @property
    def stats(self):
        with self._lock:
            return {**self._wire.stats, 'events_received': self._received,
                'graceful_close': self._graceful,
                'owned_group_empty': self._group_empty}

    def _result(self, result):
        if (type(result) is not dict or type(result.get('ok')) is not bool
                or type(result.get('event_seq')) is not int or not 0 <= result['event_seq'] <= (1 << 53) - 1):
            raise DshTransportError('invalid_dsh_envelope', result_unknown=True)
        with self._lock:
            self._received = max(self._received, result['event_seq'])
        if result['ok'] and 'value' in result:
            return result['value']
        code = result.get('code')
        if type(code) is not str or not code.isascii() or not code.replace('_', '').isalpha() or len(code) > 64:
            code = 'native_request_failed'
        raise DshTransportError(code, result_unknown=result.get('result_unknown') is True)

    def start(self):
        with self._lock:
            if self._ready: return self
            self._wire.start()
            try:
                value = self._result(self._wire.request('bridge/ready', timeout=60))
                if (type(value) is not dict or value.get('ready') is not True
                        or type(value.get('schema_version')) is not int or value['schema_version'] != 1
                        or type(value.get('presets')) is not list
                        or value.get('default_agent_preset') is not None and type(value['default_agent_preset']) is not str):
                    raise DshTransportError('native_not_ready')
                self.ready_info, self._ready = value, True
            except Exception:
                self._wire.close(); raise
        return self

    def request(self, method, params=None, timeout=20, *, rpc_id=None):
        seconds = _timeout(timeout)
        if type(method) is not str or method not in METHODS or seconds > 3600:
            raise ValueError('Unsupported native DSH method or timeout.')
        if params is not None and type(params) is not dict:
            raise ValueError('A native payload object is required.')
        if rpc_id is not None and (type(rpc_id) is not str or not rpc_id or len(rpc_id) > 128
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in rpc_id)):
            raise ValueError('A bounded original RPC identity is required.')
        if method == 'session.prompt' and rpc_id is None:
            raise ValueError('Prompt submission requires its preallocated RPC identity.')
        value = self._result(self._wire.request('bridge/request', {'method': method,
            'params': {} if params is None else params, 'rpc_id': rpc_id,
            'timeout_ms': max(1, int(seconds * 1000) - 25)}, timeout=seconds))
        if (type(value) is not dict or set(value) != {'rpcId', 'result'} or type(value.get('rpcId')) is not str
                or type(value.get('result')) is not dict or type(value['result'].get('ok')) is not bool
                or rpc_id is not None and value['rpcId'] != rpc_id):
            raise DshTransportError('native_response_invalid', result_unknown=True)
        return value

    def next_event(self, timeout=None):
        with self._lock:
            ident = threading.get_ident()
            if self._consumer is not None and self._consumer != ident:
                raise ValueError('Only one native event consumer is allowed.')
            self._consumer = ident
        frame = self._wire.next_event(timeout)
        params = frame.get('params')
        if (frame.get('method') != 'dsh/event' or 'id' in frame or type(params) is not dict
                or set(params) != {'event_seq', 'event'} or type(params.get('event_seq')) is not int
                or type(params.get('event')) is not dict):
            raise DshTransportError('native_event_invalid')
        event = params['event']
        if (set(event) != {'stream', 'rpcId', 'payload'} or event.get('stream') not in ('mux', 'host', 'lifecycle')
                or type(event.get('payload')) is not dict
                or event.get('rpcId') is not None and type(event['rpcId']) is not str):
            raise DshTransportError('native_event_invalid')
        with self._lock:
            if params['event_seq'] != self._consumed + 1:
                raise DshTransportError('native_event_order')
            self._consumed = params['event_seq']; self._received = max(self._received, params['event_seq'])
        return params['event']

    def close(self):
        if self._wire.running:
            try:
                value = self._result(self._wire.request('bridge/close', timeout=15))
                self._graceful = type(value) is dict and value.get('closed') is True
            except DshRpcError:
                pass
        self._wire.close(); self._ready = False
        state = self._wire.stats; group = state['owned_group_id']
        live = False if group is None and not self._wire.started else observe_process_group(group) if group is not None else None
        self._group_empty = None if live is None else not live
        if not state['process_exited'] or not state['io_threads_stopped'] or self._group_empty is not True:
            raise DshTransportError('native_cleanup_unconfirmed')
