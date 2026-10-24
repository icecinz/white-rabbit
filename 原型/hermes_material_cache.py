"""Hermes-owned media location for Rabbit's content-addressed image snapshots.

The shared publisher owns byte verification, bounds and read-only files. This
adapter owns only the exact native profile/workspace/chat namespace. No source
paths are accepted from HTTP and no native image or transcript is rewritten.
"""
import base64
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

from native_materials import NativeMaterials, NativeMaterialError
from rabbit_content.materials import MAX_FILE_BYTES


_WORKSPACE = re.compile(r'[0-9a-f]{64}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')


def cache_root(profile_home, workspace_id, chat_id):
    try:
        home = Path(profile_home)
        if (type(profile_home) is not str or not home.is_absolute() or str(home) != profile_home
                or '..' in home.parts or '$' in profile_home or '\\' in profile_home
                or len(profile_home) > 4096 or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in profile_home)
                or type(workspace_id) is not str or not _WORKSPACE.fullmatch(workspace_id)
                or type(chat_id) is not str or str(uuid.UUID(chat_id)) != chat_id):
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise NativeMaterialError('snapshot_unavailable') from None
    return home / 'images' / 'rabbit-native' / workspace_id / chat_id


@contextmanager
def _parents(home, workspace_id):
    """Make only our namespace parents; retain no-follow directory anchors."""
    descriptors, anchors = [], []
    try:
        parent = NativeMaterials._open_chain(home, descriptors, anchors)
        for index, name in enumerate(('images', 'rabbit-native', workspace_id)):
            try: os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError: pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(child); anchors.append((parent, name, child))
            info = os.fstat(child)
            # Hermes owns the existing images directory. Our descendants have
            # the same strict private-directory contract as the shared cache.
            if info.st_uid != os.getuid() or index and stat.S_IMODE(info.st_mode) != 0o700:
                raise NativeMaterialError('snapshot_changed')
            parent = child
        yield
        for parent, name, child in anchors:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            held = os.fstat(child)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                raise NativeMaterialError('snapshot_changed')
    except NativeMaterialError: raise
    except OSError: raise NativeMaterialError('snapshot_unavailable') from None
    finally:
        for descriptor in reversed(descriptors): os.close(descriptor)


def publish(images, originals, *, profile_home, workspace_id, chat_id):
    root = cache_root(profile_home, workspace_id, chat_id)
    with _parents(Path(profile_home), workspace_id):
        return NativeMaterials(None, None, root).publish_images(images, originals)


def plan(images, *, profile_home, workspace_id, chat_id):
    """Pure destination metadata; capacity checks can precede publication."""
    root = cache_root(profile_home, workspace_id, chat_id)
    if type(images) is not list or not 1 <= len(images) <= 4:
        raise NativeMaterialError('invalid_request')
    result = []
    for image in images:
        if (type(image) is not dict or image.get('mime') not in ('image/png', 'image/jpeg')
                or type(image.get('sha256')) is not str or not _SHA.fullmatch(image['sha256'])
                or type(image.get('size_bytes')) is not int or not 0 < image['size_bytes'] <= MAX_FILE_BYTES):
            raise NativeMaterialError('material_changed')
        name = image['sha256'] + ('.png' if image['mime'] == 'image/png' else '.jpg')
        result.append({'path': str(root / 'images' / name),
            **{key: image[key] for key in ('sha256', 'size_bytes', 'mime')}})
    if sum(image['size_bytes'] for image in result) > 20 * 1024 * 1024:
        raise NativeMaterialError('capacity')
    return result


def read(image, *, profile_home, workspace_id, chat_id):
    """Read one ledger-owned exact cache entry; never mkdir, lock or publish."""
    root = cache_root(profile_home, workspace_id, chat_id)
    if (type(image) is not dict or set(image) != {'path', 'sha256', 'size_bytes', 'mime'}
            or type(image['sha256']) is not str or not _SHA.fullmatch(image['sha256'])
            or image['mime'] not in ('image/png', 'image/jpeg')
            or type(image['size_bytes']) is not int or not 0 < image['size_bytes'] <= MAX_FILE_BYTES):
        raise NativeMaterialError('snapshot_changed')
    name = image['sha256'] + ('.png' if image['mime'] == 'image/png' else '.jpg')
    if image['path'] != str(root / 'images' / name):
        raise NativeMaterialError('snapshot_changed')
    descriptors, anchors, descriptor = [], [], None
    try:
        directory = NativeMaterials._open_chain(root / 'images', descriptors, anchors)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o400 or before.st_size != image['size_bytes']):
            raise NativeMaterialError('snapshot_changed')
        with os.fdopen(descriptor, 'rb', closefd=False) as handle: raw = handle.read(MAX_FILE_BYTES + 1)
        version = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if (len(raw) != image['size_bytes'] or hashlib.sha256(raw).hexdigest() != image['sha256']
                or version(before) != version(os.fstat(descriptor))
                or version(before) != version(os.stat(name, dir_fd=directory, follow_symlinks=False))
                or not (raw.startswith(b'\x89PNG\r\n\x1a\n') if image['mime'] == 'image/png' else raw.startswith(b'\xff\xd8\xff'))):
            raise NativeMaterialError('snapshot_changed')
        for parent, entry, child in anchors:
            named, held = os.stat(entry, dir_fd=parent, follow_symlinks=False), os.fstat(child)
            if (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino) or not stat.S_ISDIR(named.st_mode):
                raise NativeMaterialError('snapshot_changed')
        return {'data_base64': base64.b64encode(raw).decode('ascii'),
                **{key: image[key] for key in ('mime', 'size_bytes', 'sha256')}}
    except NativeMaterialError: raise
    except OSError: raise NativeMaterialError('snapshot_unavailable') from None
    finally:
        if descriptor is not None: os.close(descriptor)
        for item in reversed(descriptors): os.close(item)
