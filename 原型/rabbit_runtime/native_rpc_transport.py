"""Shared event-byte budget for the existing native JSONL/process transport.

Only queue accounting lives here. Protocol initialization, identities, tool
decisions, history and request receipts remain with each native connector.
"""
import json

from .codex_rpc import CodexRpc, ProtocolError


class BoundedRpc(CodexRpc):
    def __init__(self, *args, max_event_bytes, **kwargs):
        super().__init__(*args, **kwargs)
        self._event_bytes, self._max_event_bytes = 0, max_event_bytes

    @property
    def stats(self):
        """Return transport counters and queued bytes from one locked snapshot."""
        with self._lock:
            return {**super().stats, 'queued_event_bytes': self._event_bytes}

    @staticmethod
    def _size(frame):
        return len(json.dumps(frame, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8'))

    def _dispatch(self, frame):
        with self._changed:
            event = type(frame) is dict and 'method' in frame
            size = self._size(frame) if event else 0
            if self._event_bytes + size > self._max_event_bytes:
                raise ProtocolError('event_byte_limit')
            before = len(self._events)
            super()._dispatch(frame)
            if len(self._events) > before:
                self._event_bytes += size

    def next_event(self, timeout=None):
        with self._changed:
            value = super().next_event(timeout)
            self._event_bytes -= self._size(value)
            return value

    def _fail(self, error, **kwargs):
        super()._fail(error, **kwargs)
        with self._lock:
            self._event_bytes = 0
