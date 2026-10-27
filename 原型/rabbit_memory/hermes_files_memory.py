"""Owned Hermes file memory. Core and home scope are explicitly injected.

Manual saves coordinate with native MemoryStore's .md.lock files. This is a
cooperating-writer version check, not exclusion of arbitrary external editors.
No Agent, provider lifecycle, retain operation or automatic discovery is used.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import uuid

from .hindsight_memory import MemoryServiceError, MAX_FACT_CHARACTERS, MAX_QUERY_CHARACTERS
from .memory_profiles import make_memory_snapshot


DOCUMENTS = ('MEMORY.md', 'USER.md')
MAX_DOCUMENT_BYTES = 512 * 1024
_MANUAL_WRITES = threading.RLock()


def _error(code='memory_service_unavailable', *, status=None):
    return MemoryServiceError(code, '文件记忆暂时无法完整核验；没有自动写回或重试。', status=status)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _text_bytes(text):
    if (type(text) is not str or any(ord(c) < 32 and c not in '\t\r\n'
            or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in text)):
        raise ValueError('记忆文档需要有效的完整 UTF-8 文字。')
    raw = text.encode('utf-8')
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError('记忆文档超过完整预览容量，没有截断保存。')
    return raw


class HermesFilesMemory:
    def __init__(self, root, *, core_factory, core_scope):
        root = Path(root)
        if (not root.is_absolute() or '..' in root.parts or
                any(ord(c) < 32 or ord(c) == 127 for c in str(root))
                or not callable(core_factory) or not callable(core_scope)):
            raise ValueError('文件记忆需要明确的独立目录和原生核心。')
        self.root = root
        self.connection_id = 'local/hermes-files'
        self._factory, self._scope = core_factory, core_scope
        self.base_url = ''  # No network endpoint or authentication.
        self.authentication_fingerprint = _sha(b'')
        self.source_fingerprint = _sha(str(root).encode('utf-8'))

    @staticmethod
    def _profile(profile):
        value = make_memory_snapshot(profile)
        if (value.get('program') != 'hermes_files' or value.get('connection_id') != 'local/hermes-files'
                or value.get('mode') != 'read_only'):
            raise ValueError('这份配置不属于只读 Hermes 文件记忆。')
        return value

    @staticmethod
    def _directory(path, *, create=False):
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def _documents(self, profile, *, create=False):
        value = self._profile(profile)
        home = self.root / ('rabbit-' + value['id'])
        path = home / 'memories'
        try:
            fd = self._directory(path, create=create)
        except FileNotFoundError:
            raise _error('memory_bank_missing', status=404) from None
        except OSError:
            raise _error() from None
        try:
            before = os.fstat(fd)
            if before.st_uid != os.geteuid():
                raise _error()
            yield fd, home
            current = os.stat(path, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise _error('memory_connection_changed')
        except OSError:
            raise _error() from None
        finally:
            os.close(fd)

    @staticmethod
    def _read(fd, name):
        if name not in DOCUMENTS:
            raise ValueError('只能读取 MEMORY.md 或 USER.md。')
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(handle)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or before.st_nlink != 1 or before.st_size > MAX_DOCUMENT_BYTES):
                raise _error('memory_response_invalid')
            chunks, size = [], 0
            while True:
                chunk = os.read(handle, min(65536, MAX_DOCUMENT_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_DOCUMENT_BYTES:
                    raise _error('memory_response_invalid')
            after = os.fstat(handle)
            current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if stamp(before) != stamp(after) or stamp(after) != stamp(current):
                raise _error('memory_connection_changed')
            raw = b''.join(chunks)
            text = raw.decode('utf-8', 'strict')
            _text_bytes(text)
            return {'name': name, 'text': text, 'sha256': _sha(raw), 'size_bytes': len(raw)}, stamp(after)
        finally:
            os.close(handle)

    @staticmethod
    @contextmanager
    def _lock(fd, name):
        lock = os.open(name + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise _error()
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
        finally:
            os.close(lock)

    def health(self):
        return {'available': True, 'write_supported': False, 'version': 'hermes-files/1'}

    def ensure_bank(self, profile):
        created = False
        with self._documents(profile, create=True) as (fd, _):
            for name in DOCUMENTS:
                try:
                    handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=fd)
                except FileExistsError:
                    self._read(fd, name)
                else:
                    os.fsync(handle)
                    os.close(handle)
                    created = True
            os.fsync(fd)
        return {'created': created}

    def bank_profile(self, profile):
        with self._documents(profile) as (fd, _):
            for name in DOCUMENTS:
                self._read(fd, name)
        return {'program': 'hermes_files'}

    def bank_state(self, profile, *, fresh=False):
        if type(fresh) is not bool:
            raise ValueError('刷新标记需要布尔值。')
        with self._documents(profile) as (fd, _):
            count = sum(bool(self._read(fd, name)[0]['text'].strip()) for name in DOCUMENTS)
        return {'state': 'ready' if count else 'empty', 'facts': count,
                'documents': count, 'pending_operations': 0}

    def read_document(self, profile, name):
        with self._documents(profile) as (fd, _):
            return self._read(fd, name)[0]

    def write_document(self, profile, name, text, *, expected_sha256):
        if name not in DOCUMENTS or type(expected_sha256) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256):
            raise ValueError('手动保存需要明确文档和原文 SHA256。')
        raw = _text_bytes(text)
        stage = '.manual-' + str(uuid.uuid4())
        with _MANUAL_WRITES, self._documents(profile) as (fd, home), self._lock(fd, name):
            # Reuse the native parser and budget calculation without writing a
            # candidate to the real document or starting the Agent/tool gate.
            target = 'memory' if name == 'MEMORY.md' else 'user'
            with self._scope(home):
                core = self._factory()
                entries = list(dict.fromkeys(core._parse_entries(raw.decode('utf-8-sig'))))
                core._set_entries(target, entries)
                if core._char_count(target) > core._char_limit(target):
                    raise ValueError('记忆文档超过原生核心的字符限额，没有保存或截断。')
            original, stamp = self._read(fd, name)
            if original['sha256'] != expected_sha256:
                raise _error('memory_document_changed', status=409)
            if original['sha256'] == _sha(raw):
                return original
            backup = name + '.previous-' + str(uuid.uuid4())
            handle = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                with os.fdopen(handle, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                if self._read(fd, name)[1] != stamp:
                    raise _error('memory_document_changed', status=409)
                previous = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                   0o600, dir_fd=fd)
                with os.fdopen(previous, 'wb') as stream:
                    stream.write(original['text'].encode('utf-8'))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(stage, name, src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
                result = self._read(fd, name)[0]
                if result['sha256'] != _sha(raw):
                    raise _error('memory_document_changed', status=409)
                return result
            finally:
                try:
                    os.unlink(stage, dir_fd=fd)
                except FileNotFoundError:
                    pass

    def recall(self, profile, query):
        memory = self._profile(profile)
        if type(query) is not str or len(query) > MAX_QUERY_CHARACTERS:
            raise ValueError('记忆查询超过容量。')
        facts, characters = [], 0
        with self._documents(profile) as (fd, home):
            before = {name: self._read(fd, name) for name in DOCUMENTS}
            with self._scope(home):
                core = self._factory()
                core.load_from_disk()
                blocks = [core.format_for_system_prompt(target) for target in ('memory', 'user')]
            for name, block in zip(DOCUMENTS, blocks):
                after = self._read(fd, name)
                if before[name] != after:
                    raise _error('memory_connection_changed')
                if block is None or block == '':
                    continue
                _text_bytes(block)
                characters += len(block)
                if characters > MAX_FACT_CHARACTERS:
                    raise _error('memory_response_invalid')
                native = name + ':' + before[name][0]['sha256']
                reference = _sha(('rabbit-' + memory['id'] + '\0' + native + '\0' + block).encode())[:32]
                facts.append({'reference_id': 'memref_' + reference, 'native_id': native,
                    'text': block, 'text_sha256': _sha(block.encode()), 'type': 'world',
                    'document_id': name})
        return {'schema_version': 1, 'memory_profile': memory, 'query_sha256': _sha(query.encode()),
                'retrieved_at': datetime.now(timezone.utc).isoformat(), 'facts': facts,
                'total_results': len(facts), 'omitted_results': 0, 'characters': characters}

    def retain(self, *args, **kwargs):
        raise _error('memory_read_only')

    retain_payload = retain
    operation = retain
    document_receipt = retain
