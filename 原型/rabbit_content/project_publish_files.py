"""Host-only, new-file publication into one fixed existing project root.

No scanning, project manifests, lock files, directory creation or overwrites.
The ledger must freeze stage identity synchronously before the first byte is
written. Recovery uses that identity and accepted hash, never a fresh payload.
"""
from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import stat
import threading
import uuid


MAX_FILE_BYTES = 512 * 1024
MAX_PATH_CHARACTERS = 240
MAX_DEPTH = 8
EXTENSIONS = frozenset(('.md', '.txt'))
STAGE_PREFIX = '.rabbit-publish-'
ERROR_MESSAGES = {
    'invalid_arguments': '项目发布参数不完整或不符合已确认记录。',
    'invalid_path': '请选择项目内非隐藏的相对 .md 或 .txt 文件名。',
    'invalid_text': '项目文件需要完整的 UTF-8 文字，不能包含空字节。',
    'capacity': '项目发布文本不能超过512 KiB。',
    'scope_unavailable': '原项目目录暂时无法使用，没有改用其他目录。',
    'scope_changed': '原项目或目标父目录已经变化，本次发布未跟随目录变化。',
    'not_found': '目标文件或已有父目录找不到。',
    'unsafe_file': '目标范围存在链接或特殊文件，未读取或覆盖它。',
    'file_exists': '目标文件名已经存在，原文件保留，请另选新文件名。',
    'file_changed': '项目文件或暂存内容已变化，未把不确定内容标记为发布成功。',
    'stage_exists': '这次发布的暂存名称已存在，没有重新写入它。',
    'stage_missing': '已登记的发布暂存找不到，未重新生成或恢复旧内容。',
    'io_failed': '项目发布尚未完成，没有确认未经核验的文件。',
    'closed': '项目发布文件接口已经关闭。',
}


class ProjectPublishError(ValueError):
    def __init__(self, code):
        self.code = code if code in ERROR_MESSAGES else 'io_failed'
        super().__init__(ERROR_MESSAGES[self.code])


def _identity(info):
    return {'dev': info.st_dev, 'ino': info.st_ino}


def _state(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink)


def _valid_identity(value):
    return (isinstance(value, dict) and set(value) == {'dev', 'ino'}
            and type(value['dev']) is int and value['dev'] >= 0
            and type(value['ino']) is int and value['ino'] > 0)


def _parts(path):
    if (not isinstance(path, str) or not 1 <= len(path) <= MAX_PATH_CHARACTERS
            or path.startswith('/') or '\\' in path or ':' in path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        raise ProjectPublishError('invalid_path')
    parts = path.split('/')
    if (len(parts) > MAX_DEPTH or any(not item or item.startswith('.') for item in parts)
            or Path(parts[-1]).suffix.casefold() not in EXTENSIONS):
        raise ProjectPublishError('invalid_path')
    try:
        if any(len(item.encode('utf-8')) > 255 for item in parts):
            raise ProjectPublishError('invalid_path')
    except UnicodeEncodeError:
        raise ProjectPublishError('invalid_path') from None
    return tuple(parts)


def _text_bytes(text):
    if not isinstance(text, str) or '\0' in text:
        raise ProjectPublishError('invalid_text')
    try:
        raw = text.encode('utf-8')
    except UnicodeEncodeError:
        raise ProjectPublishError('invalid_text') from None
    if len(raw) > MAX_FILE_BYTES:
        raise ProjectPublishError('capacity')
    return raw


def _operation_id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise ProjectPublishError('invalid_arguments') from None
    return value


def _digest_arguments(stage_identity, sha256, size_bytes):
    if (not isinstance(stage_identity, dict)
            or set(stage_identity) != {'name', 'dev', 'ino'}
            or not _valid_identity({key: stage_identity.get(key) for key in ('dev', 'ino')})
            or not isinstance(stage_identity['name'], str)
            or not stage_identity['name'].startswith(STAGE_PREFIX)):
        raise ProjectPublishError('invalid_arguments')
    _operation_id(stage_identity['name'][len(STAGE_PREFIX):])
    if (not isinstance(sha256, str) or len(sha256) != 64
            or any(char not in '0123456789abcdef' for char in sha256)
            or type(size_bytes) is not int or not 0 <= size_bytes <= MAX_FILE_BYTES):
        raise ProjectPublishError('invalid_arguments')


class ProjectPublishFiles:
    def __init__(self, root):
        try:
            named = os.fspath(root)
            if not isinstance(named, str) or not named or '\0' in named:
                raise ProjectPublishError('scope_unavailable')
            self.root = Path(os.path.abspath(named))
        except (TypeError, ValueError, OSError):
            raise ProjectPublishError('scope_unavailable') from None
        self.lock = threading.RLock()
        self._closed = False
        descriptor = None
        try:
            descriptor, chain = self._open_named_root()
            self._root_identity = _identity(os.fstat(descriptor))
            self._root_chain = chain
        except OSError:
            raise ProjectPublishError('scope_unavailable') from None
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

    def _open_named_root(self):
        opened = []
        chain = []
        try:
            current = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            opened.append(current)
            chain.append(_identity(os.fstat(current)))
            for component in self.root.parts[1:]:
                current = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                  | os.O_NONBLOCK, dir_fd=current)
                opened.append(current)
                chain.append(_identity(os.fstat(current)))
            return os.dup(current), chain
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)

    def _check_root(self, descriptor):
        named = None
        try:
            named, chain = self._open_named_root()
            if (chain != self._root_chain or _identity(os.fstat(named)) != self._root_identity
                    or _identity(os.fstat(descriptor)) != self._root_identity):
                raise ProjectPublishError('scope_changed')
        except OSError:
            raise ProjectPublishError('scope_changed') from None
        finally:
            if named is not None:
                os.close(named)

    @contextmanager
    def _scope(self, parts, expected_scope=None):
        if self._closed:
            raise ProjectPublishError('closed')
        root_fd = None
        children = []
        try:
            try:
                root_fd, _chain = self._open_named_root()
            except OSError:
                raise ProjectPublishError('scope_changed') from None
            self._check_root(root_fd)
            current = root_fd
            lineage = []
            identities = []
            for index, component in enumerate(parts[:-1]):
                try:
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                    | os.O_NONBLOCK, dir_fd=current)
                except OSError:
                    if expected_scope is not None:
                        raise ProjectPublishError('scope_changed') from None
                    raise
                children.append(child)
                identity = _identity(os.fstat(child))
                lineage.append((current, component, identity))
                identities.append({'path': '/'.join(parts[:index + 1]), **identity})
                current = child
            scope = {'root': dict(self._root_identity), 'parents': identities}
            if expected_scope is not None:
                self._validate_scope(expected_scope, parts)
                if expected_scope != scope:
                    raise ProjectPublishError('scope_changed')
            self._check_lineage(root_fd, lineage)
            yield root_fd, current, lineage, scope
        except ProjectPublishError:
            raise
        except FileNotFoundError:
            raise ProjectPublishError('not_found') from None
        except OSError as error:
            code = 'unsafe_file' if error.errno in (errno.ELOOP, errno.ENOTDIR) else 'io_failed'
            raise ProjectPublishError(code) from None
        finally:
            for descriptor in reversed(children):
                os.close(descriptor)
            if root_fd is not None:
                os.close(root_fd)

    @staticmethod
    def _validate_scope(value, parts):
        if (not isinstance(value, dict) or set(value) != {'root', 'parents'}
                or not _valid_identity(value['root']) or not isinstance(value['parents'], list)
                or len(value['parents']) != len(parts) - 1):
            raise ProjectPublishError('invalid_arguments')
        for index, row in enumerate(value['parents']):
            if (not isinstance(row, dict) or set(row) != {'path', 'dev', 'ino'}
                    or row['path'] != '/'.join(parts[:index + 1])
                    or not _valid_identity({key: row.get(key) for key in ('dev', 'ino')})):
                raise ProjectPublishError('invalid_arguments')

    def _check_lineage(self, root_fd, lineage):
        self._check_root(root_fd)
        for parent, name, expected in lineage:
            try:
                actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError:
                raise ProjectPublishError('scope_changed') from None
            if not stat.S_ISDIR(actual.st_mode) or _identity(actual) != expected:
                raise ProjectPublishError('scope_changed')

    @staticmethod
    def _read(parent, name, *, expected_identity=None, allowed_links=(1,)):
        descriptor = None
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink not in allowed_links:
                raise ProjectPublishError('unsafe_file')
            if expected_identity is not None and _identity(before) != expected_identity:
                raise ProjectPublishError('file_changed')
            if before.st_size > MAX_FILE_BYTES:
                raise ProjectPublishError('capacity')
            chunks, size = [], 0
            while True:
                raw = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - size))
                if not raw:
                    break
                chunks.append(raw)
                size += len(raw)
                if size > MAX_FILE_BYTES:
                    raise ProjectPublishError('capacity')
            after = os.fstat(descriptor)
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (_state(before) != _state(after) or _state(named) != _state(after)
                    or size != before.st_size):
                raise ProjectPublishError('file_changed')
            raw = b''.join(chunks)
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError:
                raise ProjectPublishError('invalid_text') from None
            if '\0' in text:
                raise ProjectPublishError('invalid_text')
            return text, raw, before
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _write(descriptor, raw):
        remaining = memoryview(raw)
        while remaining:
            count = os.write(descriptor, remaining)
            if count <= 0:
                raise ProjectPublishError('io_failed')
            remaining = remaining[count:]
        os.fsync(descriptor)

    @staticmethod
    def _link(parent, stage, name):
        os.link(stage, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)

    @staticmethod
    def _result(path, scope, text=None, raw=None, info=None):
        result = {'path': path, 'exists': info is not None, 'scope': scope}
        if info is not None:
            result.update(text=text, sha256=hashlib.sha256(raw).hexdigest(),
                          size_bytes=len(raw), identity=_identity(info), content_verified=True)
        return result

    def inspect(self, relative_path):
        parts = _parts(relative_path)
        with self.lock, self._scope(parts) as (root_fd, parent, lineage, scope):
            try:
                text, raw, info = self._read(parent, parts[-1])
            except FileNotFoundError:
                self._check_lineage(root_fd, lineage)
                return self._result(relative_path, scope)
            self._check_lineage(root_fd, lineage)
            return self._result(relative_path, scope, text, raw, info)

    def create_new(self, relative_path, text, operation_id, expected_scope, on_stage_created):
        parts = _parts(relative_path)
        raw = _text_bytes(text)
        stage = STAGE_PREFIX + _operation_id(operation_id)
        self._validate_scope(expected_scope, parts)
        if not callable(on_stage_created):
            raise ProjectPublishError('invalid_arguments')
        with self.lock, self._scope(parts, expected_scope) as (root_fd, parent, lineage, scope):
            try:
                os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ProjectPublishError('file_exists')
            descriptor = None
            stage_identity = None
            accepted = False
            try:
                try:
                    descriptor = os.open(stage, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=parent)
                except FileExistsError:
                    raise ProjectPublishError('stage_exists') from None
                stage_identity = {'name': stage, **_identity(os.fstat(descriptor))}
                os.fsync(descriptor)
                os.fsync(parent)
                # This is deliberately before writing. A callback failure cannot
                # leave a target, and accepted stage identity survives a crash.
                try:
                    on_stage_created(dict(stage_identity))
                except Exception:
                    raise ProjectPublishError('io_failed') from None
                accepted = True
                self._check_lineage(root_fd, lineage)
                self._write(descriptor, raw)
                self._check_lineage(root_fd, lineage)
                stage_text, stage_raw, _info = self._read(parent, stage,
                    expected_identity={key: stage_identity[key] for key in ('dev', 'ino')})
                if stage_raw != raw:
                    raise ProjectPublishError('file_changed')
                self._check_lineage(root_fd, lineage)
                try:
                    self._link(parent, stage, parts[-1])
                except FileExistsError:
                    raise ProjectPublishError('file_exists') from None
                os.fsync(parent)
                self._check_lineage(root_fd, lineage)
                result = self._verify_open(relative_path, parent, scope, stage_identity,
                                           hashlib.sha256(raw).hexdigest(), len(raw))
                self._remove_stage(parent, stage_identity)
                os.fsync(parent)
                self._check_lineage(root_fd, lineage)
                result = self._verify_open(relative_path, parent, scope, stage_identity,
                                           hashlib.sha256(raw).hexdigest(), len(raw))
                self._check_lineage(root_fd, lineage)
                result['stage_identity'] = dict(stage_identity)
                return result
            finally:
                if descriptor is not None:
                    try:
                        if not accepted and stage_identity is not None:
                            # Only our own inode; never remove an external stage name.
                            self._remove_stage(parent, stage_identity, missing_ok=True)
                            os.fsync(parent)
                    finally:
                        os.close(descriptor)

    @staticmethod
    def _remove_stage(parent, stage_identity, missing_ok=False):
        try:
            current = os.stat(stage_identity['name'], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return
            raise ProjectPublishError('not_found') from None
        if (not stat.S_ISREG(current.st_mode)
                or _identity(current) != {key: stage_identity[key] for key in ('dev', 'ino')}):
            raise ProjectPublishError('file_changed')
        os.unlink(stage_identity['name'], dir_fd=parent)

    def _verify_open(self, relative_path, parent, scope, stage_identity, sha256, size_bytes):
        expected_identity = {key: stage_identity[key] for key in ('dev', 'ino')}
        text, raw, info = self._read(parent, relative_path.split('/')[-1],
                                     expected_identity=expected_identity, allowed_links=(1, 2))
        if len(raw) != size_bytes or hashlib.sha256(raw).hexdigest() != sha256:
            raise ProjectPublishError('file_changed')
        try:
            _stage_text, stage_raw, stage_info = self._read(parent, stage_identity['name'],
                expected_identity=expected_identity, allowed_links=(2,))
        except FileNotFoundError:
            if info.st_nlink != 1:
                raise ProjectPublishError('unsafe_file')
        else:
            if (info.st_nlink != 2 or stage_info.st_nlink != 2 or stage_raw != raw):
                raise ProjectPublishError('file_changed')
        return self._result(relative_path, scope, text, raw, info)

    def verify(self, relative_path, expected_scope, stage_identity, sha256, size_bytes):
        parts = _parts(relative_path)
        self._validate_scope(expected_scope, parts)
        _digest_arguments(stage_identity, sha256, size_bytes)
        with self.lock, self._scope(parts, expected_scope) as (root_fd, parent, lineage, scope):
            result = self._verify_open(relative_path, parent, scope, stage_identity, sha256, size_bytes)
            self._check_lineage(root_fd, lineage)
            result['stage_identity'] = dict(stage_identity)
            return result

    def recover(self, relative_path, expected_scope, stage_identity, sha256, size_bytes):
        parts = _parts(relative_path)
        self._validate_scope(expected_scope, parts)
        _digest_arguments(stage_identity, sha256, size_bytes)
        with self.lock, self._scope(parts, expected_scope) as (root_fd, parent, lineage, scope):
            try:
                os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                expected_identity = {key: stage_identity[key] for key in ('dev', 'ino')}
                try:
                    _text, raw, _info = self._read(parent, stage_identity['name'],
                                                  expected_identity=expected_identity)
                except FileNotFoundError:
                    raise ProjectPublishError('stage_missing') from None
                if len(raw) != size_bytes or hashlib.sha256(raw).hexdigest() != sha256:
                    raise ProjectPublishError('file_changed')
                self._check_lineage(root_fd, lineage)
                try:
                    self._link(parent, stage_identity['name'], parts[-1])
                except FileExistsError:
                    # A concurrent publication must still pass inode and SHA.
                    pass
                os.fsync(parent)
            result = self._verify_open(relative_path, parent, scope, stage_identity, sha256, size_bytes)
            self._check_lineage(root_fd, lineage)
            try:
                os.stat(stage_identity['name'], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                self._remove_stage(parent, stage_identity)
                os.fsync(parent)
            self._check_lineage(root_fd, lineage)
            result = self._verify_open(relative_path, parent, scope, stage_identity, sha256, size_bytes)
            self._check_lineage(root_fd, lineage)
            result['stage_identity'] = dict(stage_identity)
            return result
