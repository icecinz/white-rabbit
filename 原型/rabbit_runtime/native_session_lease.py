"""A single-Mac executor lease for one authoritative ledger and session key.

An empty, private file is locked for the executor's lifetime. No timestamps,
PID files, expiry, native message bodies, or model requests are involved. The fixed
namespace is the ledger parent's native-session-leases directory; the file key
uses the ledger's device/inode rather than its filename. This does not make a
hand-copied ledger authoritative or coordinate independently configured tools.
A released owner lease does not prove that all native background tools stopped.
"""
import errno
import fcntl
import hashlib
import json
import os
import stat
import threading

from .native_session_store import (NativeSessionStore, NativeSessionStoreError,
                                  open_directory, file_identity, validate_path, validate_token)


DIRECTORY_NAME = 'native-session-leases'
_MESSAGES = {
    'invalid_arguments': '原生执行器租约参数无效。',
    'invalid_namespace': '原生执行器租约只能位于权威记录库的固定目录。',
    'busy': '这段原生会话已由另一执行器持有，未接管或重放。',
    'scope_changed': '原生执行器租约路径或身份已变化，未继续执行。',
    'store_scope': '权威原生记录库已关闭或身份变化，未继续执行。',
    'not_acquired': '原生执行器尚未取得租约。',
    'wrong_process': '当前进程不能借用其他进程的原生执行器租约。',
    'closed': '原生执行器租约已关闭。',
    'io_failed': '原生执行器租约暂时不可用。',
}


class NativeSessionLeaseError(ValueError):
    def __init__(self, code):
        self.code = code if code in _MESSAGES else 'io_failed'
        super().__init__(_MESSAGES[self.code])


class NativeSessionLease:
    def __init__(self, store, session_key, *, lease_dir):
        if not isinstance(store, NativeSessionStore):
            raise NativeSessionLeaseError('invalid_arguments')
        try:
            self.session_key = validate_token(session_key)
            self.directory = validate_path(lease_dir)
        except NativeSessionStoreError:
            raise NativeSessionLeaseError('invalid_arguments') from None
        self.store = store
        self.lock = threading.RLock()
        self._file_fd = self._directory_fd = None
        self._closed = self._acquired = False
        self._owner_pid = None
        if self.directory != store.path.parent / DIRECTORY_NAME:
            raise NativeSessionLeaseError('invalid_namespace')
        with store.lock:
            self._check_store()
            self._ledger_identity = store._file_identity
            self._parent_identity = store._parent_identity
        namespace = json.dumps([*self._ledger_identity, self.session_key], separators=(',', ':')).encode('utf-8')
        self._name = 'lease-' + hashlib.sha256(namespace).hexdigest() + '.lock'

    def _check_store(self):
        try:
            with self.store.lock:
                self.store._check_path()
                if hasattr(self, '_ledger_identity') and self.store._file_identity != self._ledger_identity:
                    raise NativeSessionLeaseError('store_scope')
        except (NativeSessionStoreError, OSError):
            raise NativeSessionLeaseError('store_scope') from None

    @staticmethod
    def _private(info, *, directory=False):
        required = 0o700 if directory else 0o600
        if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != required
                or not directory and (info.st_nlink != 1 or info.st_size != 0)):
            raise NativeSessionLeaseError('scope_changed')

    def _open_directory(self):
        parent = open_directory(self.directory.parent)
        try:
            if file_identity(os.fstat(parent)) != self._parent_identity:
                raise NativeSessionLeaseError('scope_changed')
            try:
                os.mkdir(DIRECTORY_NAME, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            descriptor = os.open(DIRECTORY_NAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        finally:
            os.close(parent)
        try:
            self._private(os.fstat(descriptor), directory=True)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _verify(self):
        if self._closed:
            raise NativeSessionLeaseError('closed')
        if not self._acquired:
            raise NativeSessionLeaseError('not_acquired')
        if self._owner_pid != os.getpid():
            raise NativeSessionLeaseError('wrong_process')
        current = open_directory(self.directory)
        try:
            info = os.fstat(current)
            self._private(info, directory=True)
            if file_identity(info) != self._directory_identity:
                raise NativeSessionLeaseError('scope_changed')
        finally:
            os.close(current)
        held = os.fstat(self._file_fd)
        named = os.stat(self._name, dir_fd=self._directory_fd, follow_symlinks=False)
        self._private(held)
        self._private(named)
        if file_identity(held) != self._file_identity or file_identity(named) != self._file_identity:
            raise NativeSessionLeaseError('scope_changed')
        try:
            with self.store.lock:
                if self.store._file_identity != self._ledger_identity:
                    raise NativeSessionLeaseError('store_scope')
                return self.store.verify_scope(self.session_key)
        except NativeSessionStoreError:
            raise NativeSessionLeaseError('store_scope') from None

    @property
    def held(self):
        """Memory-only ownership flag; this is not a filesystem scope check."""
        with self.lock:
            return self._acquired and not self._closed and self._owner_pid == os.getpid()

    @property
    def active(self):
        return self.held

    def acquire(self):
        """Take a nonblocking lifetime lock, or reject another live executor."""
        with self.lock:
            if self._closed:
                raise NativeSessionLeaseError('closed')
            if self._acquired:
                self.verify()
                return self
            try:
                self._check_store()
                self._directory_fd = self._open_directory()
                self._directory_identity = file_identity(os.fstat(self._directory_fd))
                try:
                    self._file_fd = os.open(self._name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                            0o600, dir_fd=self._directory_fd)
                except FileExistsError:
                    self._file_fd = os.open(self._name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                                            dir_fd=self._directory_fd)
                os.set_inheritable(self._file_fd, False)
                info = os.fstat(self._file_fd)
                self._private(info)
                self._file_identity = file_identity(info)
                try:
                    fcntl.flock(self._file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                        raise NativeSessionLeaseError('busy') from None
                    raise
                self._owner_pid = os.getpid()
                self._acquired = True
                self._verify()
                return self
            except BaseException as error:
                self._discard()
                if isinstance(error, NativeSessionLeaseError):
                    raise
                if isinstance(error, OSError):
                    raise NativeSessionLeaseError('scope_changed') from None
                raise

    def verify(self):
        """Verify lease and cwd identity; return only the current binding."""
        with self.lock:
            try:
                return self._verify()
            except OSError:
                raise NativeSessionLeaseError('scope_changed') from None

    def _discard(self):
        # Closing, rather than explicit LOCK_UN, preserves a parent's lock when
        # a forked child closes its inherited duplicate. FD_CLOEXEC excludes RPC
        # children from retaining the parent's lifetime lock after exec.
        for field in ('_file_fd', '_directory_fd'):
            descriptor = getattr(self, field)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, field, None)
        self._acquired = False

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._discard()
            self._closed = True

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_args):
        self.close()
