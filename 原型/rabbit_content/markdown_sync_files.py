"""One retained-version swap, not a filesystem compare-and-swap.

The caller durably freezes the old proof/new payload and the stage callback.
An uncooperative editor can change either name after the last check. A swap
therefore retains the actual displaced object even on conflict; this module
never unlinks it, rolls back, retries a swap, or fills a missing stage.

Only current-user regular files with an owner write bit are accepted. POSIX
permission bits are copied to the new inode; ACLs/xattrs are not synchronized.
fsync covers the existing application's process-crash durability contract,
not an unconditional promise against sudden hardware power loss.
"""
from contextlib import contextmanager
import ctypes
import errno
import hashlib
import os
import stat
import sys

from .project_publish_files import (
    ProjectPublishFiles, ProjectPublishError, ERROR_MESSAGES, MAX_FILE_BYTES,
    _parts, _text_bytes, _operation_id, _valid_identity, _identity,
)

STAGE_PREFIX = '.rabbit-sync-'
ERROR_CODES = frozenset(ERROR_MESSAGES) | {'unsupported_platform', 'swap_unsupported', 'readonly_file'}
_MESSAGES = dict(ERROR_MESSAGES, **{
    'unsupported_platform': '当前平台没有启用保留旧版的原子更新。',
    'swap_unsupported': '当前系统或卷不支持所需原子交换，没有改用覆盖写入。',
    'readonly_file': '目标权限不符合更新范围，原文件和已创建暂存保留。',
})


class MarkdownSyncError(ValueError):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else 'io_failed'
        super().__init__(_MESSAGES[self.code])


@contextmanager
def _errors():
    try:
        yield
    except MarkdownSyncError:
        raise
    except ProjectPublishError as error:
        raise MarkdownSyncError(error.code) from None
    except OSError as error:
        code = 'readonly_file' if error.errno in (errno.EACCES, errno.EPERM) else 'io_failed'
        raise MarkdownSyncError(code) from None


def _digest(sha256, size_bytes):
    if (type(sha256) is not str or len(sha256) != 64
            or any(char not in '0123456789abcdef' for char in sha256)
            or type(size_bytes) is not int or not 0 <= size_bytes <= MAX_FILE_BYTES):
        raise MarkdownSyncError('invalid_arguments')


def _matches(value, identity, sha256, size_bytes):
    return (value is not None and value['identity'] == identity
            and value['sha256'] == sha256 and value['size_bytes'] == size_bytes)


class MarkdownSyncFiles:
    def __init__(self, root):
        if sys.platform != 'darwin':
            raise MarkdownSyncError('unsupported_platform')
        try:
            self._library = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
            self._rename = self._library.renameatx_np
            self._rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                                    ctypes.c_char_p, ctypes.c_uint]
            self._rename.restype = ctypes.c_int
        except (OSError, AttributeError):
            raise MarkdownSyncError('swap_unsupported') from None
        with _errors():
            self.files = ProjectPublishFiles(root)
        self.lock = self.files.lock

    def close(self):
        self.files.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def inspect(self, path):
        with _errors():
            return self.files.inspect(path)

    @staticmethod
    def _arguments(path, operation_id, expected_scope, expected_identity,
                   expected_sha256, expected_size_bytes):
        parts = _parts(path)
        stage = STAGE_PREFIX + _operation_id(operation_id)
        ProjectPublishFiles._validate_scope(expected_scope, parts)
        if not _valid_identity(expected_identity):
            raise MarkdownSyncError('invalid_arguments')
        _digest(expected_sha256, expected_size_bytes)
        scope = {'root': dict(expected_scope['root']),
                 'parents': [dict(row) for row in expected_scope['parents']]}
        return parts, stage, scope, dict(expected_identity)

    @staticmethod
    def _writable(info):
        mode = stat.S_IMODE(info.st_mode)
        if info.st_uid != os.geteuid() or not mode & stat.S_IWUSR or mode & 0o7000:
            raise MarkdownSyncError('readonly_file')
        return mode

    def _swap(self, parent, stage, target):
        """Exactly one kernel call; no replacement or non-atomic fallback."""
        ctypes.set_errno(0)
        if self._rename(parent, os.fsencode(stage), parent, os.fsencode(target), 2) == 0:
            return
        error = ctypes.get_errno()
        if error in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL):
            raise MarkdownSyncError('swap_unsupported')
        if error in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
            raise MarkdownSyncError('file_changed')
        if error in (errno.EACCES, errno.EPERM):
            raise MarkdownSyncError('readonly_file')
        raise MarkdownSyncError('io_failed')

    def apply(self, path, new_text, operation_id, expected_scope, expected_identity,
              expected_sha256, expected_size_bytes, on_stage_created):
        with _errors():
            parts, stage, expected_scope, expected_identity = self._arguments(
                path, operation_id, expected_scope, expected_identity, expected_sha256, expected_size_bytes)
            raw = _text_bytes(new_text)
            if not callable(on_stage_created):
                raise MarkdownSyncError('invalid_arguments')
            new_sha256 = hashlib.sha256(raw).hexdigest()
            with self.lock, self.files._scope(parts, expected_scope) as (root, parent, lineage, scope):
                _text, old_raw, old = self.files._read(parent, parts[-1], expected_identity=expected_identity)
                if len(old_raw) != expected_size_bytes or hashlib.sha256(old_raw).hexdigest() != expected_sha256:
                    raise MarkdownSyncError('file_changed')
                mode = self._writable(old)
                descriptor = None
                try:
                    try:
                        descriptor = os.open(stage, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                             0o600, dir_fd=parent)
                    except FileExistsError:
                        raise MarkdownSyncError('stage_exists') from None
                    os.fchmod(descriptor, mode)
                    stage_identity = {'name': stage, **_identity(os.fstat(descriptor))}
                    os.fsync(descriptor)
                    os.fsync(parent)
                    try:
                        on_stage_created(dict(stage_identity))
                    except Exception:
                        raise MarkdownSyncError('io_failed') from None
                    self.files._check_lineage(root, lineage)
                    _text, initial_raw, _info = self.files._read(
                        parent, stage, expected_identity=_identity(os.fstat(descriptor)))
                    if initial_raw:
                        raise MarkdownSyncError('file_changed')
                    self.files._write(descriptor, raw)
                    self.files._check_lineage(root, lineage)
                    _text, stage_raw, _info = self.files._read(
                        parent, stage, expected_identity={key: stage_identity[key] for key in ('dev', 'ino')})
                    if stage_raw != raw:
                        raise MarkdownSyncError('file_changed')
                    # This catches changes before dispatch. It is deliberately
                    # not described as an atomic inode/content precondition.
                    _text, current_raw, current = self.files._read(
                        parent, parts[-1], expected_identity=expected_identity)
                    if (len(current_raw) != expected_size_bytes
                            or hashlib.sha256(current_raw).hexdigest() != expected_sha256
                            or self._writable(current) != mode):
                        raise MarkdownSyncError('file_changed')
                    self.files._check_lineage(root, lineage)
                    self._swap(parent, stage, parts[-1])
                    os.fsync(parent)
                    self.files._check_lineage(root, lineage)
                    observation = self.observe(path, operation_id, expected_scope, expected_identity,
                        expected_sha256, expected_size_bytes, stage_identity, new_sha256, len(raw))
                    if observation['state'] != 'applied':
                        raise MarkdownSyncError('file_changed')
                    return dict(observation['target'], stage_identity=stage_identity,
                        retained_identity={'name': stage, **observation['retained']['identity']})
                finally:
                    if descriptor is not None:
                        os.close(descriptor)

    def _read_observation(self, parent, name, path, scope):
        try:
            text, raw, info = self.files._read(parent, name)
            return self.files._result(path, scope, text, raw, info), None
        except FileNotFoundError:
            return None, 'missing'
        except ProjectPublishError as error:
            return None, error.code
        except OSError:
            return None, 'io_failed'

    def observe(self, path, operation_id, expected_scope, expected_identity,
                expected_sha256, expected_size_bytes, stage_identity, new_sha256, new_size_bytes):
        """Read the current layout; `not_applied` is not an execution receipt."""
        with _errors():
            parts, stage, expected_scope, expected_identity = self._arguments(
                path, operation_id, expected_scope, expected_identity, expected_sha256, expected_size_bytes)
            _digest(new_sha256, new_size_bytes)
            if (type(stage_identity) is not dict or set(stage_identity) != {'name', 'dev', 'ino'}
                    or stage_identity['name'] != stage
                    or not _valid_identity({key: stage_identity[key] for key in ('dev', 'ino')})):
                raise MarkdownSyncError('invalid_arguments')
            new_identity = {key: stage_identity[key] for key in ('dev', 'ino')}
            if new_identity == expected_identity:
                raise MarkdownSyncError('invalid_arguments')
            stage_path = '/'.join((*parts[:-1], stage))
            with self.lock, self.files._scope(parts, expected_scope) as (root, parent, lineage, scope):
                target, target_error = self._read_observation(parent, parts[-1], path, scope)
                retained, retained_error = self._read_observation(parent, stage, stage_path, scope)
                last, last_error = self._read_observation(parent, parts[-1], path, scope)
                self.files._check_lineage(root, lineage)
                errors = (target_error, retained_error, last_error)
                if any(error in ('missing', 'io_failed') for error in errors):
                    state = 'unknown'
                elif any(errors) or target != last:
                    state = 'conflict'
                elif (_matches(target, new_identity, new_sha256, new_size_bytes)
                        and _matches(retained, expected_identity, expected_sha256, expected_size_bytes)):
                    state = 'applied'
                elif (_matches(target, expected_identity, expected_sha256, expected_size_bytes)
                        and _matches(retained, new_identity, new_sha256, new_size_bytes)):
                    state = 'not_applied'
                else:
                    state = 'conflict'
                return {'state': state, 'target': last, 'retained': retained}

    def recover(self, path, operation_id, expected_scope, expected_identity,
                expected_sha256, expected_size_bytes, stage_identity, new_sha256, new_size_bytes):
        """Recovery only observes; a second swap could undo the first one."""
        return self.observe(path, operation_id, expected_scope, expected_identity,
            expected_sha256, expected_size_bytes, stage_identity, new_sha256, new_size_bytes)
