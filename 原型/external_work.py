"""One explicit in-memory HAPI binding, separate from Rabbit-owned work.

GET reads this cache. Only refresh calls the existing fixed list reader; this
module never authenticates, reads HAPI files, starts a process or retries.
"""
from copy import deepcopy
import json
import re
import threading

from rabbit_runtime.hapi_work import HapiWorkError, HapiWorkReader, project_sessions


PATH = '/api/management/external-work'
MAX_BODY = 12 * 1024
MAX_REVISION = 2**53 - 1
REASONS = frozenset(('auth_required', 'connection_unavailable', 'http_error',
                     'redirect_refused', 'invalid_response', 'response_limit',
                     'observation_unavailable'))
_REPORT_FIELDS = frozenset(('schema_version', 'provider', 'scope', 'origin',
    'snapshot_at', 'native_event_freshness', 'native_executor_verified',
    'capabilities', 'complete', 'reason', 'works'))
_MESSAGES = {
    'invalid_arguments': '外部工作参数无效。',
    'revision_conflict': '外部工作绑定已变化，请重新读取。',
    'binding_changed': '读取期间绑定已变化；旧结果未采用。',
    'refresh_busy': '正在刷新这个外部工作来源，请稍后再试。',
    'unconfigured': '请先明确连接外部工作来源。',
}


class ExternalWorkError(ValueError):
    def __init__(self, reason):
        self.code = 'external_work_' + reason
        self.status = 400 if reason == 'invalid_arguments' else 409
        super().__init__(_MESSAGES[reason])


def decode_body(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError

    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_BODY:
            raise ValueError
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)
        if type(value) is not dict or ('_one_ai_csrf' in value and type(value['_one_ai_csrf']) is not str):
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ExternalWorkError('invalid_arguments') from None


class ExternalWork:
    def __init__(self, workspace_id, *, reader_factory=None):
        if (type(workspace_id) is not str or not re.fullmatch(r'[0-9a-f]{64}', workspace_id)
                or reader_factory is not None and not callable(reader_factory)):
            raise ExternalWorkError('invalid_arguments')
        self.workspace_id = workspace_id
        self._factory = reader_factory
        self._lock = threading.Lock()
        self._revision = 0
        self._origin = self._reader = self._report = self._reason = None
        self._state = 'unconfigured'
        self._inflight = set()
        self._closed = False

    def _view(self):
        return dict(schema_version=1, workspace_id=self.workspace_id, provider='hapi',
            scope='external_hub', revision=self._revision, origin=self._origin,
            state=self._state, reason=self._reason, report=deepcopy(self._report))

    def read(self):
        with self._lock:
            return self._view()

    def operation(self, body):
        if type(body) is not dict:
            raise ExternalWorkError('invalid_arguments')
        action = body.get('operation')
        fields = {'operation', 'expected_revision'}
        if action == 'bind':
            fields |= {'origin', 'bearer_token'}
        if (type(action) is not str or action not in ('bind', 'clear', 'refresh')
                or set(body) != fields or type(body['expected_revision']) is not int
                or not 0 <= body['expected_revision'] <= MAX_REVISION):
            raise ExternalWorkError('invalid_arguments')
        with self._lock:
            if self._closed or body['expected_revision'] != self._revision:
                raise ExternalWorkError('revision_conflict')
            if action in ('bind', 'clear'):
                if self._revision == MAX_REVISION:
                    raise ExternalWorkError('invalid_arguments')
                reader = None
                if action == 'bind':
                    # The real constructor always validates origin/token even
                    # when a trusted fixture supplies a replacement reader.
                    try:
                        reader = HapiWorkReader(body['origin'], body['bearer_token'])
                        if self._factory is not None:
                            reader = self._factory(body['origin'], body['bearer_token'])
                        if not callable(getattr(reader, 'snapshot', None)):
                            raise ValueError
                    except Exception:
                        raise ExternalWorkError('invalid_arguments') from None
                self._revision += 1
                self._reader = reader
                self._origin = body['origin'] if action == 'bind' else None
                self._state = 'unverified' if reader is not None else 'unconfigured'
                self._reason = self._report = None
                return self._view()
            if self._reader is None:
                raise ExternalWorkError('unconfigured')
            revision, reader, origin = self._revision, self._reader, self._origin
            if revision in self._inflight:
                raise ExternalWorkError('refresh_busy')
            self._inflight.add(revision)
        report, reason = None, None
        try:
            try:
                report = reader.snapshot()
                if (type(report) is not dict or set(report) != _REPORT_FIELDS
                        or type(report['schema_version']) is not int or report['schema_version'] != 1
                        or report['provider'] != 'hapi' or report['scope'] != 'hapi_hub_summary'
                        or report['origin'] != origin or report['complete'] is not True or report['reason'] is not None
                        or report['native_event_freshness'] != 'unknown' or report['native_executor_verified'] is not False
                        or report['capabilities'] != {'send': False, 'approve': False, 'stop': False}
                        or type(report['works']) is not list):
                    raise HapiWorkError('invalid_response')
                # Reuse the same pure allowlisted projection for trusted test
                # factories too; no unknown metadata can escape via this cache.
                report = dict(report, works=project_sessions({'sessions': report['works']}, origin))
                report = deepcopy(report)
            except HapiWorkError as error:
                report, reason = None, error.code if type(error.code) is str and error.code in REASONS else 'observation_unavailable'
            except Exception:
                report, reason = None, 'observation_unavailable'
            with self._lock:
                if self._closed or self._revision != revision or self._reader is not reader:
                    raise ExternalWorkError('binding_changed')
                self._report, self._reason = report, reason
                self._state = 'observed' if reason is None else 'unavailable'
                return self._view()
        finally:
            with self._lock:
                self._inflight.discard(revision)

    def close(self):
        with self._lock:
            self._closed = True
            self._reader = self._origin = self._report = self._reason = None
            self._state = 'unconfigured'
