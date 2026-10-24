"""Three bounded host file tools, confined to a server-owned task directory.

Only new UTF-8 .md/.txt files can be saved. Existing files are never overwritten;
existing nonhidden subdirectories are allowed, but tools do not create them.
Tool results are host-verified filesystem evidence, independent of AI prose.
Each call opens/closes anchored descriptors, checks the frozen root identity,
and cooperates with other instances through a private file lock. The root is
supplied by the host constructor and is never accepted as a tool argument.
"""
from contextlib import contextmanager
import copy
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import re
import stat
import threading
import time


MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 20
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_PATH_CHARACTERS = 240
MAX_DEPTH = 8
MAX_DIRECTORIES = 128
LOCK_WAIT_SECONDS = 5
LOCK_NAME = '.rabbit-file-task.lock'
CANCEL_NAME = '.rabbit-cancelled'
INPUT_MANIFEST = '.rabbit-inputs.json'
MAX_MANIFEST_BYTES = 16 * 1024
EXTENSIONS = frozenset(('.md', '.txt'))

ERROR_MESSAGES = {
    'invalid_tool': '这项文件工具不在本轮允许的清单内。',
    'invalid_arguments': '文件工具参数不完整或包含不允许的字段。',
    'invalid_path': '请选择非隐藏的相对 .md 或 .txt 文件名；不能跨出任务目录。',
    'scope_unavailable': '本轮任务目录暂时无法使用；没有改用其他目录。',
    'scope_changed': '本轮任务目录或子目录已变化；没有跟随换目录。',
    'not_found': '这份任务文件或它的已有子目录找不到。',
    'unsafe_file': '任务范围中存在链接或特殊文件，未读取或覆盖它。',
    'file_changed': '任务文件在核验中变化，未把不确定内容作为成果。',
    'file_exists': '这个文件名已经存在；原文件保留，请另选新文件名。',
    'invalid_text': '文件需要完整的 UTF-8 文字，不能包含无效编码或空字节。',
    'capacity': '任务文本超过容量：单文件512 KiB、最多20份、合计4 MiB。',
    'busy': '另一项文件操作尚未完成；本次没有重复写入。',
    'cancelled': '本轮文件任务已取消，没有继续保存未提交的成果。',
    'io_failed': '本轮文件操作未完成；没有把未经核验的文件标记为已保存。',
    'closed': '这轮文件工具已经关闭。',
}


class FileTaskError(ValueError):
    def __init__(self, code):
        if code not in ERROR_MESSAGES:
            code = 'io_failed'
        self.code = code
        super().__init__(ERROR_MESSAGES[code])


def _identity(info):
    return info.st_dev, info.st_ino


def _file_state(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink)


def _parts(path):
    if (not isinstance(path, str) or not 1 <= len(path) <= MAX_PATH_CHARACTERS
            or path.startswith('/') or '\\' in path or ':' in path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        raise FileTaskError('invalid_path')
    parts = path.split('/')
    if (len(parts) > MAX_DEPTH or any(not part or part.startswith('.') for part in parts)
            or Path(parts[-1]).suffix.casefold() not in EXTENSIONS):
        raise FileTaskError('invalid_path')
    try:
        if any(len(part.encode('utf-8')) > 255 for part in parts):
            raise FileTaskError('invalid_path')
    except UnicodeEncodeError:
        raise FileTaskError('invalid_path') from None
    return tuple(parts)


def _text_bytes(text):
    if not isinstance(text, str) or '\0' in text:
        raise FileTaskError('invalid_text')
    try:
        raw = text.encode('utf-8')
    except UnicodeEncodeError:
        raise FileTaskError('invalid_text') from None
    if len(raw) > MAX_FILE_BYTES:
        raise FileTaskError('capacity')
    return raw


TOOLS = (
    {'name': 'rabbit_files_list', 'description':
        '列出本轮任务目录内可用的 .md/.txt 文件，并核验完整内容及哈希；不读取其他目录。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'rabbit_file_read', 'description':
        '读取本轮任务目录内一份完整 UTF-8 .md/.txt 文件，返回文字、大小和哈希；不截断。',
     'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string',
         'minLength': 1, 'maxLength': MAX_PATH_CHARACTERS}}, 'required': ['path'],
         'additionalProperties': False}},
    {'name': 'rabbit_file_save', 'description':
        '在本轮任务目录新增 UTF-8 .md/.txt 成果；存在不覆盖。子目录需已经存在，不自动创建目录。',
     'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string',
         'minLength': 1, 'maxLength': MAX_PATH_CHARACTERS},
         'text': {'type': 'string', 'maxLength': MAX_FILE_BYTES}},
         'required': ['path', 'text'], 'additionalProperties': False}},
)


class FileTaskFiles:
    def __init__(self, root, *, cancel_event=None, host_readonly=False):
        try:
            named = os.fspath(root)
            if not isinstance(named, str) or not named or '\0' in named:
                raise FileTaskError('scope_unavailable')
            self.root = Path(os.path.abspath(named))
        except (TypeError, ValueError, OSError):
            raise FileTaskError('scope_unavailable') from None
        if cancel_event is not None and not callable(getattr(cancel_event, 'is_set', None)):
            raise FileTaskError('invalid_arguments')
        if not isinstance(host_readonly, bool):
            raise FileTaskError('invalid_arguments')
        self.cancel_event = cancel_event
        self.host_readonly = host_readonly
        self._active_root_fd = None
        self._input_signature = None
        self._input_hashes = {}
        self.lock = threading.RLock()
        self._closed = False
        self._check_cancel()
        descriptor = None
        try:
            descriptor = self._open_named_root()
            self._root_identity = _identity(os.fstat(descriptor))
            self._check_cancel(descriptor)
            if not self.host_readonly:
                self._input_signature, self._input_hashes = self._manifest_value(descriptor)
        except OSError:
            raise FileTaskError('scope_unavailable') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def tools(self):
        return copy.deepcopy(list(TOOLS[:2] if self.host_readonly else TOOLS))

    def check_cancelled(self):
        """Host preflight/final check; reads no file body and is not an MCP tool."""
        with self.lock:
            if self._closed:
                raise FileTaskError('closed')
            descriptor = None
            try:
                descriptor = self._open_named_root()
                self._check_root(descriptor)
                self._check_cancel(descriptor)
            except OSError:
                raise FileTaskError('scope_changed') from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def close(self):
        with self.lock:
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _check_cancel(self, root_fd=None):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise FileTaskError('cancelled')
        if self.host_readonly or not hasattr(self, '_root_identity'):
            return
        descriptor = root_fd if root_fd is not None else self._active_root_fd
        owned = descriptor is None
        try:
            if owned:
                try:
                    descriptor = self._open_named_root()
                    self._check_root(descriptor)
                except OSError:
                    raise FileTaskError('scope_changed') from None
            try:
                os.stat(CANCEL_NAME, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise FileTaskError('cancelled')
        finally:
            if owned and descriptor is not None:
                os.close(descriptor)

    def _open_named_root(self):
        # Walk all absolute components using opened parents. A later symlink
        # or ancestor replacement cannot redirect these opens outside scope.
        descriptors = []
        try:
            current = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(current)
            for component in self.root.parts[1:]:
                current = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=current)
                descriptors.append(current)
            return os.dup(current)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _check_root(self, descriptor):
        named = None
        try:
            named = self._open_named_root()
            if (_identity(os.fstat(named)) != self._root_identity
                    or _identity(os.fstat(descriptor)) != self._root_identity):
                raise FileTaskError('scope_changed')
        except OSError:
            raise FileTaskError('scope_changed') from None
        finally:
            if named is not None:
                os.close(named)

    def _manifest_value(self, root_fd):
        """Only host-created metadata; no text source body is loaded here."""
        descriptor = None
        try:
            try:
                descriptor = os.open(INPUT_MANIFEST, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
            except FileNotFoundError:
                return None, {}
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_MANIFEST_BYTES:
                raise FileTaskError('file_changed')
            chunks, size = [], 0
            while True:
                raw = os.read(descriptor, min(4096, MAX_MANIFEST_BYTES + 1 - size))
                if not raw:
                    break
                chunks.append(raw)
                size += len(raw)
                if size > MAX_MANIFEST_BYTES:
                    raise FileTaskError('file_changed')
            named = os.stat(INPUT_MANIFEST, dir_fd=root_fd, follow_symlinks=False)
            after = os.fstat(descriptor)
            if _file_state(before) != _file_state(after) or _file_state(named) != _file_state(after) or size != before.st_size:
                raise FileTaskError('file_changed')
            raw = b''.join(chunks)
            value = json.loads(raw.decode('utf-8'))
            if (not isinstance(value, dict) or set(value) != {'version', 'inputs'}
                    or type(value['version']) is not int or value['version'] != 1
                    or not isinstance(value['inputs'], list) or len(value['inputs']) > 4):
                raise FileTaskError('file_changed')
            expected = {}
            for item in value['inputs']:
                if not isinstance(item, dict) or set(item) != {'path', 'sha256', 'size_bytes'}:
                    raise FileTaskError('file_changed')
                parts = _parts(item['path'])
                if (len(parts) != 1 or item['path'] in expected or not isinstance(item['sha256'], str)
                        or re.fullmatch('[0-9a-f]{64}', item['sha256']) is None
                        or type(item['size_bytes']) is not int or not 0 <= item['size_bytes'] <= MAX_FILE_BYTES):
                    raise FileTaskError('file_changed')
                expected[item['path']] = {'sha256': item['sha256'], 'size_bytes': item['size_bytes']}
            return (_file_state(after), hashlib.sha256(raw).hexdigest()), expected
        except (OSError, UnicodeError, ValueError, TypeError):
            raise FileTaskError('file_changed') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _check_manifest(self, root_fd):
        if not self.host_readonly:
            signature, _expected = self._manifest_value(root_fd)
            if signature != self._input_signature:
                raise FileTaskError('file_changed')

    @contextmanager
    def _scope(self, *, write=False):
        root_fd = lock_fd = None
        locked = False
        try:
            try:
                root_fd = self._open_named_root()
            except OSError:
                raise FileTaskError('scope_changed') from None
            self._check_root(root_fd)
            self._active_root_fd = root_fd
            self._check_cancel(root_fd)
            self._check_manifest(root_fd)
            try:
                lock_fd = os.open(LOCK_NAME,
                                  os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                                  0o600, dir_fd=root_fd)
            except FileExistsError:
                lock_fd = os.open(LOCK_NAME, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
            lock_info = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
                raise FileTaskError('unsafe_file')
            start = time.monotonic()
            kind = fcntl.LOCK_EX if write else fcntl.LOCK_SH
            while True:
                self._check_cancel()
                try:
                    fcntl.flock(lock_fd, kind | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    if time.monotonic() - start >= LOCK_WAIT_SECONDS:
                        raise FileTaskError('busy') from None
                    wait = getattr(self.cancel_event, 'wait', None)
                    if callable(wait):
                        wait(0.01)
                    else:
                        time.sleep(0.01)
            self._check_root(root_fd)
            actual_lock = os.stat(LOCK_NAME, dir_fd=root_fd, follow_symlinks=False)
            if _identity(actual_lock) != _identity(lock_info):
                raise FileTaskError('scope_changed')
            yield root_fd
            if not write:
                self._check_cancel()
                self._check_root(root_fd)
                self._check_manifest(root_fd)
            # The writer's last cancel/scope checks live inside _save's rollback
            # boundary. That is its commit point; checking again outside it
            # could report a failed save after leaving a verified new file.
        finally:
            self._active_root_fd = None
            try:
                if lock_fd is not None:
                    try:
                        if locked:
                            fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)
            finally:
                if root_fd is not None:
                    os.close(root_fd)

    @contextmanager
    def _parent(self, root_fd, parts):
        descriptors = []
        lineage = []
        current = root_fd
        try:
            for component in parts[:-1]:
                opened = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=current)
                descriptors.append(opened)
                lineage.append((current, component, _identity(os.fstat(opened))))
                current = opened
            yield current, lineage
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _check_lineage(self, root_fd, lineage):
        self._check_root(root_fd)
        for parent, component, expected in lineage:
            actual = os.stat(component, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(actual.st_mode) or _identity(actual) != expected:
                raise FileTaskError('scope_changed')

    def _read(self, parent, name):
        descriptor = None
        try:
            self._check_cancel()
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise FileTaskError('unsafe_file')
            if before.st_size > MAX_FILE_BYTES:
                raise FileTaskError('capacity')
            chunks, size = [], 0
            while True:
                self._check_cancel()
                raw = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - size))
                if not raw:
                    break
                chunks.append(raw)
                size += len(raw)
                if size > MAX_FILE_BYTES:
                    raise FileTaskError('capacity')
            after = os.fstat(descriptor)
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (_file_state(before) != _file_state(after) or size != before.st_size
                    or not stat.S_ISREG(named.st_mode) or _file_state(named) != _file_state(after)):
                raise FileTaskError('file_changed')
            raw = b''.join(chunks)
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError:
                raise FileTaskError('invalid_text') from None
            if '\0' in text:
                raise FileTaskError('invalid_text')
            expected = self._input_hashes.get(name) if _identity(os.fstat(parent)) == self._root_identity else None
            if (not self.host_readonly and expected is not None
                    and (len(raw) != expected['size_bytes'] or hashlib.sha256(raw).hexdigest() != expected['sha256'])):
                raise FileTaskError('file_changed')
            self._check_cancel()
            return text, raw, before
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _inventory(self, root_fd):
        files, directories = [], [0]
        def walk(parent, prefix, lineage):
            self._check_cancel()
            for name in sorted(os.listdir(parent)):
                if name.startswith('.'):
                    continue  # Host lock/staging files and hidden data are not tool inputs.
                self._check_cancel()
                actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISDIR(actual.st_mode):
                    directories[0] += 1
                    if directories[0] > MAX_DIRECTORIES or len(prefix) + 1 >= MAX_DEPTH:
                        raise FileTaskError('capacity')
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                    dir_fd=parent)
                    try:
                        identity = _identity(os.fstat(child))
                        if identity != _identity(actual):
                            raise FileTaskError('scope_changed')
                        child_lineage = lineage + [(parent, name, identity)]
                        walk(child, prefix + [name], child_lineage)
                        self._check_lineage(root_fd, child_lineage)
                    finally:
                        os.close(child)
                elif not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
                    raise FileTaskError('unsafe_file')
                elif Path(name).suffix.casefold() in EXTENSIONS:
                    path = '/'.join(prefix + [name])
                    _parts(path)
                    _text, raw, _info = self._read(parent, name)
                    files.append({'name': name, 'path': path, 'size_bytes': len(raw),
                                  'sha256': hashlib.sha256(raw).hexdigest(), 'content_verified': True})
                    if len(files) > MAX_FILES or sum(row['size_bytes'] for row in files) > MAX_TOTAL_BYTES:
                        raise FileTaskError('capacity')
            self._check_lineage(root_fd, lineage)
        walk(root_fd, [], [])
        files.sort(key=lambda row: row['path'])
        return {'files': files, 'file_count': len(files), 'total_bytes': sum(row['size_bytes'] for row in files)}

    @staticmethod
    def _write(descriptor, raw):
        view = memoryview(raw)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise FileTaskError('io_failed')
            view = view[count:]
        os.fsync(descriptor)

    def _save(self, root_fd, parts, raw):
        inventory = self._inventory(root_fd)
        if inventory['file_count'] >= MAX_FILES or inventory['total_bytes'] + len(raw) > MAX_TOTAL_BYTES:
            raise FileTaskError('capacity')
        with self._parent(root_fd, parts) as (parent, lineage):
            name = parts[-1]
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileTaskError('file_exists')
            temporary = '.rabbit-file-' + secrets.token_hex(16)
            descriptor = None
            published = False
            success = False
            try:
                descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=parent)
                self._write(descriptor, raw)
                info = os.fstat(descriptor)
                self._check_cancel()
                self._check_lineage(root_fd, lineage)
                self._check_manifest(root_fd)
                # A hard-link publication is atomic and refuses any existing
                # leaf, including a concurrent regular file or symlink.
                os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                published = True
                os.unlink(temporary, dir_fd=parent)
                temporary = None
                os.fsync(parent)
                self._check_cancel()
                self._check_lineage(root_fd, lineage)
                text, actual, verified = self._read(parent, name)
                del text
                if _identity(verified) != _identity(info) or actual != raw:
                    raise FileTaskError('file_changed')
                self._check_cancel()
                self._check_lineage(root_fd, lineage)
                self._check_manifest(root_fd)
                self._check_cancel()
                success = True
                return {'name': name, 'path': '/'.join(parts), 'size_bytes': len(raw),
                        'sha256': hashlib.sha256(actual).hexdigest(), 'content_verified': True}
            finally:
                try:
                    if published and not success and descriptor is not None:
                        # Roll back only our own published inode, never an
                        # external replacement. Cancellation before the commit
                        # point removes our new artifact when the OS permits it.
                        try:
                            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                            if _identity(current) == _identity(os.fstat(descriptor)):
                                os.unlink(name, dir_fd=parent)
                                os.fsync(parent)
                        except FileNotFoundError:
                            pass
                finally:
                    try:
                        if temporary is not None:
                            try:
                                os.unlink(temporary, dir_fd=parent)
                            except FileNotFoundError:
                                pass
                    finally:
                        if descriptor is not None:
                            os.close(descriptor)

    def call(self, name, args):
        if name not in ('rabbit_files_list', 'rabbit_file_read', 'rabbit_file_save'):
            raise FileTaskError('invalid_tool')
        expected = {'rabbit_files_list': set(), 'rabbit_file_read': {'path'},
                    'rabbit_file_save': {'path', 'text'}}[name]
        if not isinstance(args, dict) or set(args) != expected:
            raise FileTaskError('invalid_arguments')
        if self.host_readonly and name == 'rabbit_file_save':
            raise FileTaskError('invalid_tool')
        parts = _parts(args['path']) if 'path' in args else None
        raw = _text_bytes(args['text']) if name == 'rabbit_file_save' else None
        with self.lock:
            if self._closed:
                raise FileTaskError('closed')
            self._check_cancel()
            try:
                with self._scope(write=name == 'rabbit_file_save') as root_fd:
                    if name == 'rabbit_files_list':
                        return self._inventory(root_fd)
                    if name == 'rabbit_file_save':
                        return self._save(root_fd, parts, raw)
                    with self._parent(root_fd, parts) as (parent, lineage):
                        text, actual, _info = self._read(parent, parts[-1])
                        self._check_lineage(root_fd, lineage)
                        return {'path': '/'.join(parts), 'text': text, 'size_bytes': len(actual),
                                'sha256': hashlib.sha256(actual).hexdigest()}
            except FileTaskError:
                raise
            except FileExistsError:
                raise FileTaskError('file_exists') from None
            except FileNotFoundError:
                raise FileTaskError('not_found') from None
            except OSError as error:
                code = 'unsafe_file' if error.errno in (errno.ELOOP, errno.ENOTDIR) else 'io_failed'
                raise FileTaskError(code) from None
