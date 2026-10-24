"""Read existing Hermes memory content without provisioning or retaining.

The fixed local source comes from Hermes' bounded configuration file. Neither
the source bank nor its configuration is editable through this transport. A
read-only recall may still create Hindsight's own audit records; read-only here
means no memory content/configuration writes, not zero database/log writes.
Public recall references use the Rabbit profile's logical namespace so accepted
packages remain compatible with the existing immutable context validator.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat

from .hindsight_memory import (HindsightMemory, MemoryServiceError, MAX_CONFIG_BYTES,
                              MAX_FACTS, MAX_FACT_CHARACTERS, MAX_QUERY_CHARACTERS,
                              _plain)
from .memory_profiles import make_memory_snapshot, validate_memory_profile_id
from .native_memory_bindings import make_native_source, validate_native_source


SOURCE_FIELDS = ('kind', 'api_url', 'native_bank_id', 'source_fingerprint')


def read_native_source(path):
    """Read api_url/bank_id only; generation-model credentials are ignored.

    JSON parsing necessarily loads the bounded file; no unrelated fields leave
    this function, no credential is used as service authentication, and errors
    never contain source contents or filesystem paths.
    """
    target = Path(path)
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
        raise ValueError('原生记忆连接配置不能是符号链接。')
    try:
        fd = os.open(str(target), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
                raise ValueError('原生记忆连接配置不是受限普通文件。')
            raw = os.read(fd, MAX_CONFIG_BYTES + 1)
            if len(raw) > MAX_CONFIG_BYTES:
                raise ValueError('原生记忆连接配置超过容量。')
        finally:
            os.close(fd)
    except OSError:
        raise ValueError('原生记忆连接配置暂时无法读取。') from None
    try:
        data = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        raise ValueError('原生记忆连接配置暂时无法读取。') from None
    if not isinstance(data, dict):
        raise ValueError('原生记忆连接配置暂时无法读取。')
    return make_native_source(data.get('api_url'), data.get('bank_id'))


class _NativeConnection:
    def __init__(self, source, *, source_getter, transport):
        self._source = validate_native_source(source)
        if not callable(source_getter):
            raise ValueError('原生记忆连接读取方式无效。')
        self._source_getter = source_getter
        self._transport = transport or HindsightMemory(self._source['api_url'])
        if (getattr(self._transport, 'base_url', None) != self._source['api_url']
                or not callable(getattr(self._transport, '_request', None))):
            raise ValueError('原生记忆传输与固定连接不一致。')
        # The current native source carries no memory-service authentication.
        # In particular its generation-model key must never become one.
        self.base_url = self._transport.base_url
        self._service_token = getattr(self._transport, '_service_token', None)
        self.source_fingerprint = self._source['source_fingerprint']
        self._check_source()

    @property
    def authentication_fingerprint(self):
        """Read-only digest of the unchanged underlying service authentication."""
        return hashlib.sha256((self._service_token or '').encode()).hexdigest()

    def _check_source(self):
        try:
            current = validate_native_source(self._source_getter())
        except (ValueError, KeyError, OSError, TypeError):
            raise MemoryServiceError('memory_native_source_unavailable',
                '原生记忆连接暂时无法核验，没有切换或创建记忆库。') from None
        if current != self._source:
            raise MemoryServiceError('memory_native_source_changed',
                '原生记忆连接已变化；原绑定保留，没有跟随换库。')

    def _request(self, method, path, body=None, *, timeout=None):
        # This whitelist closes even internal accidental writes/extra reads.
        bank_path = '/v1/default/banks/' + self._source['native_bank_id']
        valid = ((method == 'GET' and path in ('/health', '/version',
                    bank_path + '/profile', bank_path + '/stats', bank_path + '/stats?refresh=true'))
                 or method == 'POST' and path == bank_path + '/memories/recall')
        if not valid:
            self._readonly_error()
        self._check_source()
        result = self._transport._request(method, path, body, timeout=timeout)
        self._check_source()  # Do not accept a response after a concurrent source change.
        return result

    @staticmethod
    def _readonly_error():
        raise MemoryServiceError('memory_read_only',
            '这份原生记忆只读取，没有创建库、修改原库或提交保存任务。')

    def health(self):
        value = self._request('GET', '/health', timeout=3)
        if value.get('status') != 'healthy' or value.get('database') != 'connected':
            raise MemoryServiceError('memory_service_unavailable', '记忆服务或数据库尚未就绪。')
        version = self._request('GET', '/version', timeout=3).get('api_version')
        if not isinstance(version, str) or len(version) > 40:
            raise MemoryServiceError('memory_response_invalid', '记忆服务版本无法核验。')
        return {'available': True, 'version': version, 'write_supported': False}

    def _profile_metadata(self):
        bank = self._source['native_bank_id']
        value = self._request('GET', '/v1/default/banks/' + bank + '/profile')
        if value.get('bank_id') != bank:
            raise MemoryServiceError('memory_bank_identity_changed',
                '原生记忆库身份无法核验，没有覆盖或切换记忆库。')
        # Private bank name, mission/background, disposition do not leave here.
        return None

    def _bank_state(self, *, fresh=False):
        if type(fresh) is not bool:
            raise ValueError('记忆统计刷新标记无效。')
        self._profile_metadata()
        bank = self._source['native_bank_id']
        value = self._request('GET', '/v1/default/banks/' + bank + '/stats' + ('?refresh=true' if fresh else ''))
        counts = [value.get(key) for key in ('total_nodes', 'total_documents', 'pending_operations')]
        if value.get('bank_id') != bank or any(type(n) is not int or n < 0 for n in counts):
            raise MemoryServiceError('memory_response_invalid', '原生记忆库计数暂时无法核验。')
        return {'state': 'ready' if counts[0] or counts[1] else 'empty',
                'facts': counts[0], 'documents': counts[1], 'pending_operations': counts[2],
                'fresh': fresh}


def probe_native_source(source, *, source_getter, transport=None, fresh=False):
    """Only fixed-source metadata, with no profile creation or memory reads."""
    return _NativeConnection(source, source_getter=source_getter,
                             transport=transport)._bank_state(fresh=fresh)


class NativeHindsightReadOnly(_NativeConnection):
    def __init__(self, profile_binding, *, source_getter, transport=None):
        if not isinstance(profile_binding, dict) or any(key not in profile_binding for key in SOURCE_FIELDS):
            raise ValueError('原生记忆的固定绑定不完整。')
        self.profile_id = validate_memory_profile_id(profile_binding.get('profile_id'))
        source = {key: profile_binding[key] for key in SOURCE_FIELDS}
        super().__init__(source, source_getter=source_getter, transport=transport)

    def _check_profile(self, profile):
        snapshot = make_memory_snapshot(profile)
        if snapshot['id'] != self.profile_id:
            raise MemoryServiceError('memory_bank_identity_changed', '这份记忆配置与原生库绑定不一致。')
        if snapshot['mode'] != 'read_only':
            self._readonly_error()
        self._check_source()
        return snapshot

    def bank_profile(self, profile):
        self._check_profile(profile)
        self._profile_metadata()
        # Matches the existence/identity purpose, without returning private data.
        return {'verified': True}

    def bank_state(self, profile, *, fresh=False):
        self._check_profile(profile)
        return self._bank_state(fresh=fresh)

    def recall(self, profile, query):
        snapshot = self._check_profile(profile)
        _plain(query, MAX_QUERY_CHARACTERS, '记忆查询')
        self._profile_metadata()
        value = self._request('POST', '/v1/default/banks/' + self._source['native_bank_id'] + '/memories/recall',
            {'query': query, 'types': ['world', 'experience', 'observation'],
             'budget': 'low', 'max_tokens': 1024, 'trace': False, 'prefer_observations': True,
             'include': {'entities': None, 'chunks': None, 'source_facts': None}})
        rows = value.get('results')
        if not isinstance(rows, list) or len(rows) > 1024:
            raise MemoryServiceError('memory_response_invalid', '记忆检索结果数量无法核验。')
        facts, seen, characters = [], set(), 0
        logical_bank = 'rabbit-' + snapshot['id']
        for row in rows:
            if not isinstance(row, dict):
                raise MemoryServiceError('memory_response_invalid', '记忆检索结果无法核验。')
            native_id = _plain(row.get('id'), 128, '记忆条目编号')
            text = _plain(row.get('text'), MAX_FACT_CHARACTERS, '记忆条目正文')
            kind, document = row.get('type'), row.get('document_id')
            if kind not in ('world', 'experience', 'observation'):
                raise MemoryServiceError('memory_response_invalid', '记忆条目类型无法核验。')
            if document is not None:
                _plain(document, 256, '记忆文档编号')
            if native_id in seen:
                raise MemoryServiceError('memory_response_invalid', '记忆条目编号出现重复，未混合来源。')
            seen.add(native_id)
            if len(facts) >= MAX_FACTS or characters + len(text) > MAX_FACT_CHARACTERS:
                continue
            characters += len(text)
            reference = 'memref_' + hashlib.sha256((logical_bank + '\0' + native_id + '\0' + text).encode()).hexdigest()[:32]
            facts.append({'reference_id': reference, 'native_id': native_id, 'text': text,
                'text_sha256': hashlib.sha256(text.encode()).hexdigest(), 'type': kind,
                'document_id': document})
        return {'schema_version': 1, 'memory_profile': snapshot,
                'query_sha256': hashlib.sha256(query.encode()).hexdigest(),
                'retrieved_at': datetime.now(timezone.utc).isoformat(), 'facts': facts,
                'total_results': len(rows), 'omitted_results': len(rows) - len(facts),
                'characters': characters}

    # Block before validating any caller data or performing any source/HTTP I/O.
    def ensure_bank(self, *args, **kwargs):
        self._readonly_error()

    def retain(self, *args, **kwargs):
        self._readonly_error()

    def retain_payload(self, *args, **kwargs):
        self._readonly_error()

    def operation(self, *args, **kwargs):
        self._readonly_error()

    def document_receipt(self, *args, **kwargs):
        self._readonly_error()
