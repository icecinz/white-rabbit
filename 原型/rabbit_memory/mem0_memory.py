"""Optional, read-only Mem0 SDK connection with explicit private configuration.

Metadata checks never construct Mem0 or contact a service. Only recall starts
the fixed worker; there is no automatic credential discovery or write outbox.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from urllib.parse import urlsplit

from .hindsight_memory import MemoryServiceError, MAX_CONFIG_BYTES, MAX_QUERY_CHARACTERS
from .memory_profiles import make_memory_snapshot

SDK_VERSION = '2.2.1'
MAX_RESPONSE_BYTES = 192 * 1024


def _error(code='memory_mem0_unavailable'):
    return MemoryServiceError(code, 'Mem0记忆尚未配置或本次检索无法完整核验；没有写入记忆或自动重试。')


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('duplicate field')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))


def _path(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or any(item.is_symlink() for item in (path, *path.parents)):
        raise _error()
    return path


def _private_config(path):
    def stamp(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_nlink)
    path = _path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_mode & 0o077 or before.st_size > MAX_CONFIG_BYTES):
            raise _error()
        raw = os.read(fd, MAX_CONFIG_BYTES + 1)
        after = os.fstat(fd)
        if (len(raw) > MAX_CONFIG_BYTES or stamp(before) != stamp(after)
                or stamp(path.lstat()) != stamp(before)):
            raise _error('memory_connection_changed')
    finally: os.close(fd)
    value = _json(raw.decode('utf-8'))
    if type(value) is not dict or set(value) != {'schema_version', 'llm', 'embedder'} or type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise _error()
    for name in ('llm', 'embedder'):
        component = value[name]
        fields = {'model', 'base_url', 'api_key'} | ({'dimensions'} if name == 'embedder' else set())
        if type(component) is not dict or set(component) != fields:
            raise _error()
        for key in ('model', 'base_url', 'api_key'):
            item = component[key]
            if (type(item) is not str or not item.strip() or len(item) > (4096 if key == 'api_key' else 512)
                    or any(ord(c) < 33 or ord(c) == 127 for c in item)):
                raise _error('memory_mem0_unconfigured')
        url = urlsplit(component['base_url'])
        if (url.scheme not in ('https', 'http') or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment):
            raise _error()
        if name == 'embedder' and (type(component['dimensions']) is not int or not 1 <= component['dimensions'] <= 65536):
            raise _error()
    return value, hashlib.sha256(raw).hexdigest()


class Mem0Memory:
    connection_id = 'local/mem0'

    def __init__(self, python=None, config_path=None, root=None, *, worker_path=None, timeout=20):
        self.python, self.config_path, self.root, self.worker_path = python, config_path, root, worker_path
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise ValueError('Mem0 timeout invalid')
        self.timeout = timeout

    def _configured(self):
        if any(value is None for value in (self.python, self.config_path, self.root, self.worker_path)):
            raise _error('memory_mem0_unconfigured')
        python = Path(self.python)  # A trusted venv interpreter may itself be a symlink.
        if not python.is_absolute() or '..' in python.parts or not python.is_file() or not os.access(python, os.X_OK):
            raise _error('memory_mem0_unconfigured')
        worker, root = _path(self.worker_path), _path(self.root)
        if not worker.is_file() or root.exists() and not root.is_dir(): raise _error()
        config, digest = _private_config(self.config_path)
        return config, digest

    @property
    def authentication_fingerprint(self):
        try:
            _, digest = self._configured()
            return hashlib.sha256((digest + '\0' + str(self.root) + '\0' + str(self.python)).encode()).hexdigest()
        except (MemoryServiceError, ValueError, OSError):
            return 'mem0-unconfigured'

    def _invoke(self, payload):
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'SYSTEMROOT', 'SSL_CERT_FILE') if key in os.environ}
        env.update(PYTHONNOUSERSITE='1', MEM0_TELEMETRY='false')
        try:
            result = subprocess.run([str(self.python), '-B', '-I', str(self.worker_path)],
                input=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8'),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=self.timeout, env=env)
            if result.returncode != 0 or len(result.stdout) > MAX_RESPONSE_BYTES: raise _error()
            value = _json(result.stdout.decode('utf-8'))
        except (OSError, ValueError, subprocess.SubprocessError, UnicodeError):
            raise _error() from None
        if type(value) is not dict: raise _error()
        return value

    def health(self):
        try:
            self._configured()
            value = self._invoke({'schema_version': 1, 'action': 'probe'})
            if value != {'schema_version': 1, 'available': True, 'sdk_version': SDK_VERSION}: raise _error()
            return {'available': True, 'version': SDK_VERSION, 'write_supported': False, 'connection_verified': False}
        except (MemoryServiceError, ValueError, OSError):
            return {'available': False, 'write_supported': False, 'reason': 'Mem0待配置：需要独立SDK环境及明确的模型、嵌入和密钥配置。'}

    @staticmethod
    def _profile(profile):
        value = make_memory_snapshot(profile)
        if value.get('program') != 'mem0' or value.get('connection_id') != 'local/mem0' or value.get('mode') != 'read_only':
            raise _error('memory_read_only')
        return value

    def bank_state(self, profile, *, fresh=False):
        self._profile(profile)
        if type(fresh) is not bool: raise ValueError('Mem0 refresh invalid')
        if not self.health()['available']: raise _error('memory_mem0_unconfigured')
        # Opening the SDK to count would create clients and may contact providers.
        return {'state': 'ready', 'facts': None, 'documents': None, 'pending_operations': None, 'fresh': fresh}

    def ensure_bank(self, profile):
        snapshot = self._profile(profile)
        if not self.health()['available']: raise _error('memory_mem0_unconfigured')
        root = _path(self.root); root.mkdir(parents=True, exist_ok=True, mode=0o700)
        scope = _path(root / snapshot['id']); created = not scope.exists()
        scope.mkdir(exist_ok=True, mode=0o700)
        if not scope.is_dir(): raise _error()
        return {'created': created}

    def recall(self, profile, query):
        snapshot = self._profile(profile)
        if (type(query) is not str or not query.strip() or len(query) > MAX_QUERY_CHARACTERS
                or any(ord(c) < 32 and c not in '\r\n\t' or ord(c) == 127 for c in query)):
            raise _error('memory_invalid_input')
        try: query_sha = hashlib.sha256(query.encode('utf-8')).hexdigest()
        except UnicodeError: raise _error('memory_invalid_input') from None
        config, fingerprint = self._configured()
        self.ensure_bank(snapshot)
        scope = _path(Path(self.root) / snapshot['id'])
        for name in ('sdk', 'qdrant', 'history.sqlite'):
            _path(scope / name)
        if self._configured()[1] != fingerprint: raise _error('memory_connection_changed')
        value = self._invoke({'schema_version': 1, 'action': 'recall', 'profile_id': snapshot['id'],
            'query': query, 'scope': str(scope), 'config': config})
        _path(scope)
        if self._configured()[1] != fingerprint: raise _error('memory_connection_changed')
        if (set(value) != {'schema_version', 'sdk_version', 'profile_id', 'query_sha256', 'results'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['sdk_version'] != SDK_VERSION or value['profile_id'] != snapshot['id']
                or value['query_sha256'] != query_sha or type(value['results']) is not list or len(value['results']) > 64):
            raise _error('memory_response_invalid')
        facts, seen, characters = [], set(), 0
        bank = 'rabbit-' + snapshot['id']
        for row in value['results']:
            if (type(row) is not dict or set(row) != {'id', 'memory', 'user_id'} or row['user_id'] != bank
                    or type(row['id']) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', row['id'])
                    or row['id'] in seen or type(row['memory']) is not str or not row['memory'].strip()
                    or len(row['memory']) > 8192
                    or any(ord(c) < 32 and c not in '\r\n\t' or ord(c) == 127 for c in row['memory'])):
                raise _error('memory_response_invalid')
            native, text = row['id'], row['memory']; seen.add(native)
            if len(facts) >= 16 or characters + len(text) > 8192: continue
            characters += len(text)
            try: sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
            except UnicodeError: raise _error('memory_response_invalid') from None
            ref = 'memref_' + hashlib.sha256((bank + '\0' + native + '\0' + text).encode()).hexdigest()[:32]
            facts.append({'reference_id': ref, 'native_id': native, 'text': text, 'text_sha256': sha,
                          'type': 'observation', 'document_id': None})
        package = {'schema_version': 1, 'memory_profile': snapshot, 'query_sha256': query_sha,
            'retrieved_at': datetime.now(timezone.utc).isoformat(), 'facts': facts,
            'total_results': len(value['results']), 'omitted_results': len(value['results']) - len(facts), 'characters': characters}
        try:
            if len(json.dumps(package, ensure_ascii=False, allow_nan=False).encode('utf-8')) > MAX_RESPONSE_BYTES:
                raise _error('memory_capacity')
        except (ValueError, UnicodeError): raise _error('memory_response_invalid') from None
        return package

    def retain(self, *args, **kwargs):
        raise _error('memory_read_only')

    retain_payload = operation = document_receipt = retain
