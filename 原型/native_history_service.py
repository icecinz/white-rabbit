"""Shared finite history registration; Agent-specific source proofs stay separate.

No transcript store, execution loop, owner construction or native send lives
here. A private lock serializes proofs and cleanup without holding the native
manager registry while an Agent's reader is doing work.
"""
import sqlite3
import threading

from native_http_common import SpecialistHTTPError
from native_maintenance import try_locks


def _failure(code='native_history_unavailable', *, unknown=False):
    messages = {
        'native_history_invalid': '请从原生历史重新核对，只提交会话编号和预览版本。',
        'native_history_busy': '原生入口正在处理操作，请稍后重新核对。',
        'native_history_disabled': '这台入口尚未启用此原生会话恢复。',
        'native_history_unavailable': '原会话来源、目录、待恢复任务或占用情况暂时无法核验；没有连接或发送。',
    }
    return SpecialistHTTPError(400 if code == 'native_history_invalid' else 409,
        code, messages[code], accepted=None if unknown else False, result_unknown=unknown)


class NativeHistoryService:
    def __init__(self, service, manager, adoption, *, reader=None):
        self.service, self.manager, self.adoption, self.reader = service, manager, adoption, reader
        self._lock, self._closed = threading.RLock(), False

    def _available(self):
        with try_locks(self.manager._lock) as acquired:
            if not acquired:
                raise _failure('native_history_busy')
            if (self._closed or self.service._memory_stop.is_set() or self.manager._closing
                    or getattr(self.manager, '_closed', False)):
                raise _failure('native_history_disabled')

    def dispatch(self, action, fields):
        fields = {key: value for key, value in fields.items() if key != '_one_ai_csrf'} if type(fields) is dict else fields
        expected = {'session_id'} if action == 'preview' else {'session_id', 'revision'} if action == 'adopt' else None
        if expected is None or type(fields) is not dict or set(fields) != expected:
            raise _failure('native_history_invalid')
        with try_locks(self._lock) as acquired:
            if not acquired:
                raise _failure('native_history_busy')
            self._available()
            try:
                if action == 'preview':
                    return self.adoption.preview(fields['session_id'])
                return self.adoption.adopt(fields['session_id'], fields['revision'])
            except (ValueError, OSError, sqlite3.Error):
                # A registration can publish across databases before a lost
                # response. This boundary never retries or infers success.
                raise _failure(unknown=action == 'adopt') from None

    def verify_open(self, identity):
        with try_locks(self._lock) as acquired:
            if not acquired:
                raise _failure('native_history_busy')
            self._available()
            try:
                self.adoption.verify_open(identity)
            except (ValueError, OSError, sqlite3.Error):
                raise _failure() from None

    def close(self):
        with try_locks(self._lock) as acquired:
            if not acquired:
                raise _failure('native_history_busy')
            if self.reader is not None:
                self.reader.close()
            self._closed = True
