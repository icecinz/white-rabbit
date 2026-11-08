"""Persistent directory anchors, separate from live device/inode guards.

Darwin's ATTR_VOL_UUID identifies the filesystem; ATTR_CMN_FILEID is equivalent
to st_ino.  fgetattrlist operates on the already-open descriptor, so no path
lookup or diskutil subprocess is needed for the volume query.  See Apple's
primary manual and the SDK's sys/attr.h:
https://raw.githubusercontent.com/apple-oss-distributions/xnu/main/bsd/man/man2/getattrlist.2

An anchor is evidence captured now, not evidence about an older dev/inode-only
binding.  Callers must never infer or upgrade a mismatching legacy binding from
this module's observations.  There is no write, migration, or Agent operation.

Non-Darwin support is deliberately limited to Linux's current boot identifier
plus raw device/inode.  It is named boot_device and rejects the next boot; it
does not claim a durable filesystem identity.  Darwin never falls back when
the UUID provider is unavailable.
"""
import ctypes
import os
from pathlib import Path
import stat
import struct
import sys
import uuid


class DirectoryIdentityError(ValueError):
    def __init__(self, code):
        self.code = code if code in {
            'invalid_arguments', 'invalid_anchor', 'unavailable', 'scope_changed', 'closed'
        } else 'unavailable'
        super().__init__('Directory identity could not be verified: ' + self.code)


def _path(value):
    try:
        raw = os.fspath(value)
    except TypeError:
        raise DirectoryIdentityError('invalid_arguments') from None
    if (type(raw) is not str or not raw or '\x00' in raw or not os.path.isabs(raw)
            or raw.startswith('//')
            or raw != os.path.abspath(raw)):
        raise DirectoryIdentityError('invalid_arguments')
    try:
        raw.encode('utf-8')
    except UnicodeError:
        raise DirectoryIdentityError('invalid_arguments') from None
    return raw


def _raw_identity(info):
    return info.st_dev, info.st_ino


def _open_directory(path):
    """Return an anchored descriptor and the current no-symlink ancestry."""
    path = _path(path)
    descriptor = None
    lineage = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open('/', flags)
        lineage.append(_raw_identity(os.fstat(descriptor)))
        for part in Path(path).parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            lineage.append(_raw_identity(os.fstat(descriptor)))
        return descriptor, tuple(lineage)
    except (OSError, ValueError):
        if descriptor is not None:
            os.close(descriptor)
        raise DirectoryIdentityError('scope_changed') from None


def _open_file(path):
    """Open a regular, single-link file via a no-symlink parent descriptor."""
    path = _path(path)
    parent = descriptor = None
    try:
        parent, lineage = _open_directory(str(Path(path).parent))
        descriptor = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DirectoryIdentityError('scope_changed')
        return descriptor, lineage + (_raw_identity(info),)
    except (OSError, ValueError):
        if descriptor is not None:
            os.close(descriptor)
        raise DirectoryIdentityError('scope_changed') from None
    finally:
        if parent is not None:
            os.close(parent)


class _AttrList(ctypes.Structure):
    _fields_ = (
        ('bitmapcount', ctypes.c_uint16), ('reserved', ctypes.c_uint16),
        ('commonattr', ctypes.c_uint32), ('volattr', ctypes.c_uint32),
        ('dirattr', ctypes.c_uint32), ('fileattr', ctypes.c_uint32),
        ('forkattr', ctypes.c_uint32),
    )


def _darwin_volume_uuid(descriptor):
    # ATTR_VOL_INFO adds no bytes; ATTR_VOL_UUID is a 16-byte uuid_t after the
    # four-byte length. No unsupported-attribute packing/defaults are requested.
    try:
        library = ctypes.CDLL(None, use_errno=True)
        function = library.fgetattrlist
        function.argtypes = (ctypes.c_int, ctypes.POINTER(_AttrList), ctypes.c_void_p,
                             ctypes.c_size_t, ctypes.c_ulong)
        function.restype = ctypes.c_int
        attributes = _AttrList(5, 0, 0, 0x80040000, 0, 0, 0)
        buffer = ctypes.create_string_buffer(20)
        if function(descriptor, ctypes.byref(attributes), buffer, len(buffer), 0) != 0:
            raise DirectoryIdentityError('unavailable')
        if struct.unpack_from('=I', buffer.raw)[0] != 20:
            raise DirectoryIdentityError('unavailable')
        value = uuid.UUID(bytes=buffer.raw[4:20])
        if value.int == 0:
            raise DirectoryIdentityError('unavailable')
        return str(value)
    except (AttributeError, OSError, ValueError, struct.error):
        raise DirectoryIdentityError('unavailable') from None


def _linux_boot_id():
    try:
        raw = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
        value = uuid.UUID(raw)
        if raw != str(value) or value.int == 0:
            raise ValueError
        return raw
    except (OSError, UnicodeError, ValueError):
        raise DirectoryIdentityError('unavailable') from None


def validate_anchor(value):
    """Validate and copy the exact versioned JSON shape without normalization."""
    if type(value) is not dict or type(value.get('version')) is not int or value['version'] != 1:
        raise DirectoryIdentityError('invalid_anchor')
    if value.get('type') not in ('directory', 'file'):
        raise DirectoryIdentityError('invalid_anchor')
    kind = value.get('kind')
    if kind == 'volume_uuid':
        required = {'version', 'type', 'kind', 'volume_uuid', 'inode'}
        uuid_key = 'volume_uuid'
    elif kind == 'boot_device':
        required = {'version', 'type', 'kind', 'boot_id', 'device', 'inode'}
        uuid_key = 'boot_id'
        if type(value.get('device')) is not int or not 0 <= value['device'] < 2**64:
            raise DirectoryIdentityError('invalid_anchor')
    else:
        raise DirectoryIdentityError('invalid_anchor')
    if (set(value) != required or type(value.get('inode')) is not int
            or not 0 < value['inode'] < 2**64 or type(value.get(uuid_key)) is not str):
        raise DirectoryIdentityError('invalid_anchor')
    try:
        identifier = uuid.UUID(value[uuid_key])
        if str(identifier) != value[uuid_key] or identifier.int == 0:
            raise ValueError
    except (ValueError, AttributeError):
        raise DirectoryIdentityError('invalid_anchor') from None
    return dict(value)


def _capture_fd(descriptor, object_type):
    try:
        before = os.fstat(descriptor)
        if (object_type == 'directory' and not stat.S_ISDIR(before.st_mode)
                or object_type == 'file' and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1)):
            raise DirectoryIdentityError('invalid_arguments')
        if sys.platform == 'darwin':
            anchor = {'version': 1, 'type': object_type, 'kind': 'volume_uuid',
                      'volume_uuid': _darwin_volume_uuid(descriptor), 'inode': before.st_ino}
        elif sys.platform.startswith('linux'):
            anchor = {'version': 1, 'type': object_type, 'kind': 'boot_device', 'boot_id': _linux_boot_id(),
                      'device': before.st_dev, 'inode': before.st_ino}
        else:
            raise DirectoryIdentityError('unavailable')
        after = os.fstat(descriptor)
        if (_raw_identity(after) != _raw_identity(before)
                or object_type == 'file' and (not stat.S_ISREG(after.st_mode) or after.st_nlink != 1)):
            raise DirectoryIdentityError('scope_changed')
        return validate_anchor(anchor)
    except OSError:
        raise DirectoryIdentityError('scope_changed') from None


def capture_directory(path_or_fd):
    """Capture a new anchor; a caller-supplied FD is borrowed, never closed."""
    if type(path_or_fd) is int:
        return _capture_fd(path_or_fd, 'directory')
    if type(path_or_fd) is bool:
        raise DirectoryIdentityError('invalid_arguments')
    with DirectoryPin(path_or_fd) as pin:
        return pin.anchor


def verify_directory(path_or_fd, anchor):
    """Verify existing evidence only; return None and never manufacture an anchor."""
    expected = validate_anchor(anchor)
    if expected['type'] != 'directory':
        raise DirectoryIdentityError('invalid_anchor')
    if capture_directory(path_or_fd) != expected:
        raise DirectoryIdentityError('scope_changed')


def capture_file(path_or_fd):
    """Capture metadata of one regular single-link file, without reading bytes."""
    if type(path_or_fd) is int:
        return _capture_fd(path_or_fd, 'file')
    if type(path_or_fd) is bool:
        raise DirectoryIdentityError('invalid_arguments')
    with FilePin(path_or_fd) as pin:
        return pin.anchor


def verify_file(path_or_fd, anchor):
    expected = validate_anchor(anchor)
    if expected['type'] != 'file':
        raise DirectoryIdentityError('invalid_anchor')
    if capture_file(path_or_fd) != expected:
        raise DirectoryIdentityError('scope_changed')


class _ObjectPin:
    """Hold a directory FD and its complete raw device/inode path lineage.

    Persistent comparison permits a different mount's transient device number
    when this pin is first opened. After opening, check() also requires the
    original raw device/inode lineage and held descriptor throughout its life.
    Use it around an operation when a one-shot verification is insufficient.
    """
    _object_type = None

    def __init__(self, path, anchor=None):
        self.path = _path(path)
        self._fd = None
        self._anchor = validate_anchor(anchor) if anchor is not None else None
        if self._anchor is not None and self._anchor['type'] != self._object_type:
            raise DirectoryIdentityError('invalid_anchor')
        try:
            self._fd, self._lineage = self._open(self.path)
            self._identity = _raw_identity(os.fstat(self._fd))
            observed = _capture_fd(self._fd, self._object_type)
            if self._anchor is not None and observed != self._anchor:
                raise DirectoryIdentityError('scope_changed')
            self._anchor = observed
            self.check()
        except BaseException:
            self.close()
            raise

    @property
    def fd(self):
        if self._fd is None:
            raise DirectoryIdentityError('closed')
        return self._fd

    @property
    def anchor(self):
        if self._fd is None:
            raise DirectoryIdentityError('closed')
        return dict(self._anchor)

    @property
    def current_identity(self):
        """The live pin's raw (device, inode), not a persistent identifier."""
        if self._fd is None:
            raise DirectoryIdentityError('closed')
        return self._identity

    @property
    def process_witness(self):
        """Immutable raw ancestry for this process; contains no open resource."""
        if self._fd is None:
            raise DirectoryIdentityError('closed')
        return self._lineage

    def check(self):
        if self._fd is None:
            raise DirectoryIdentityError('closed')
        descriptor = None
        try:
            if _raw_identity(os.fstat(self._fd)) != self._identity:
                raise DirectoryIdentityError('scope_changed')
            descriptor, lineage = self._open(self.path)
            if lineage != self._lineage or _raw_identity(os.fstat(descriptor)) != self._identity:
                raise DirectoryIdentityError('scope_changed')
            if _capture_fd(descriptor, self._object_type) != self._anchor:
                raise DirectoryIdentityError('scope_changed')
        except OSError:
            raise DirectoryIdentityError('scope_changed') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        self.check()
        return self

    def __exit__(self, kind, _value, _traceback):
        try:
            if kind is None:
                self.check()
        finally:
            self.close()
        return False


class DirectoryPin(_ObjectPin):
    """A no-symlink directory pin; see _ObjectPin for the lifetime contract."""
    _object_type = 'directory'
    _open = staticmethod(_open_directory)


class FilePin(_ObjectPin):
    """A no-symlink, regular single-link file pin; never reads file content."""
    _object_type = 'file'
    _open = staticmethod(_open_file)
