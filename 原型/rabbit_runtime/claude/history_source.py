"""Read one anchored Claude file through the official public store reader.

The one-shot adapter only supplies complete JSONL entries. It neither chooses
a conversation chain nor searches projects; the SDK owns those semantics.
This store is never an Agent option and implements no write operation.
"""
import hashlib
import json
import math
import os
import stat
import unicodedata
import uuid

from ..native_session_store import open_directory as _directory, file_identity as _identity, validate_path as _path
from .material_protocol import MAX_SESSION_FILE_BYTES, MAX_SESSION_LINE_BYTES


MAX_FILE_BYTES = MAX_SESSION_FILE_BYTES
MAX_LINE_BYTES = MAX_SESSION_LINE_BYTES
MAX_LINES = 100000
MAX_NODES = 2000000
MAX_DEPTH = 256
_FIELDS = frozenset(('session_id', 'cwd', 'path', 'device', 'inode', 'size',
    'mtime_ns', 'cwd_device', 'cwd_inode'))


class ClaudeHistorySourceError(ValueError):
    def __init__(self, code):
        self.code = code if code in ('invalid_arguments', 'sdk_helper_unavailable',
            'source_unverified', 'source_changed', 'source_capacity', 'sdk_read_failed') else 'source_unverified'
        super().__init__('Claude 历史原文件无法完整核验，未搜索其他位置。')


def _fail(code):
    raise ClaudeHistorySourceError(code)


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            _fail('source_unverified')
        result[key] = value
    return result


def _constant(value):
    _fail('source_unverified')


def _validate_json(value, remaining):
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        remaining -= 1
        if remaining < 0 or depth > MAX_DEPTH:
            _fail('source_capacity')
        kind = type(item)
        if item is None or kind is bool:
            continue
        if kind in (int, float):
            if not math.isfinite(float(item)):
                _fail('source_unverified')
        elif kind is str:
            item.encode('utf-8', 'strict')
        elif kind is list:
            if len(stack) + len(item) > remaining:
                _fail('source_capacity')
            stack.extend((nested, depth + 1) for nested in item)
        elif kind is dict:
            if len(stack) + 2 * len(item) > remaining:
                _fail('source_capacity')
            for key, nested in item.items():
                stack.append((key, depth + 1)); stack.append((nested, depth + 1))
        else:
            _fail('source_unverified')
    return remaining


def _stamp(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
        value.st_ctime_ns, value.st_mode, value.st_nlink)


def _regular(value):
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or value.st_size <= 0:
        _fail('source_unverified')
    if value.st_size > MAX_FILE_BYTES:
        _fail('source_capacity')


def _check(descriptor, *, file_fd, parent_fd, work_fd, project, cwd, file_name, first):
    current = os.fstat(file_fd)
    if (_stamp(current) != _stamp(first)
            or _stamp(os.stat(file_name, dir_fd=parent_fd, follow_symlinks=False)) != _stamp(first)
            or _identity(os.fstat(work_fd)) != (descriptor['cwd_device'], descriptor['cwd_inode'])):
        _fail('source_changed')
    _regular(current)
    for path, identity in ((project, _identity(os.fstat(parent_fd))),
            (cwd, _identity(os.fstat(work_fd)))):
        fresh = _directory(path)
        try:
            if _identity(os.fstat(fresh)) != identity:
                _fail('source_changed')
        finally:
            os.close(fresh)


def _entries(file_fd, *, session_id, cwd):
    os.lseek(file_fd, 0, os.SEEK_SET)
    rows, total, remaining, matched = [], 0, MAX_NODES, False
    digest = hashlib.sha256()
    with os.fdopen(file_fd, 'rb', closefd=False) as handle:
        while True:
            raw = handle.readline(MAX_LINE_BYTES + 1)
            if not raw:
                break
            total += len(raw)
            if len(raw) > MAX_LINE_BYTES or total > MAX_FILE_BYTES or len(rows) >= MAX_LINES:
                _fail('source_capacity')
            if not raw.endswith(b'\n'):
                _fail('source_unverified')
            digest.update(raw)
            value = json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=_pairs,
                parse_constant=_constant)
            if type(value) is not dict:
                _fail('source_unverified')
            remaining = _validate_json(value, remaining)
            sid, directory = value.get('sessionId'), value.get('cwd')
            if (sid is not None and sid != session_id or directory is not None
                    and (type(directory) is not str or unicodedata.normalize('NFC', directory) != cwd)):
                _fail('source_unverified')
            matched |= sid == session_id and directory is not None
            rows.append(value)
    if not matched:
        _fail('source_unverified')
    return rows, total, digest.hexdigest()


def _digest(file_fd):
    os.lseek(file_fd, 0, os.SEEK_SET)
    digest, total = hashlib.sha256(), 0
    while True:
        raw = os.read(file_fd, min(1024 * 1024, MAX_FILE_BYTES + 1 - total))
        if not raw:
            break
        total += len(raw)
        if total > MAX_FILE_BYTES:
            _fail('source_capacity')
        digest.update(raw)
    return total, digest.hexdigest()


class _ReadOnlyStore:
    __slots__ = ('_key', '_entries', '_loaded', '_closed')

    def __init__(self, key, entries):
        self._key, self._entries = key, entries
        self._loaded, self._closed = False, False

    async def load(self, key):
        if (self._closed or self._loaded or type(key) is not dict
                or set(key) != {'project_key', 'session_id'} or key != self._key):
            _fail('sdk_read_failed')
        self._loaded = True
        return self._entries

    def close(self):
        self._closed, self._entries = True, None


async def read_sdk_messages(sdk, *, session_id, cwd, project_dir, expected_file, offset, limit):
    """Return (official SDK messages, unchanged file identity descriptor).

    The descriptor identifies the actual file supplied to ``store.load``.
    Full raw-byte SHA is compared internally around the official SDK call.
    No source path, raw RPC or fallback is accepted from a browser.
    """
    descriptors, adapter = [], None
    try:
        try:
            valid_id = type(session_id) is str and str(uuid.UUID(session_id)) == session_id
        except ValueError:
            valid_id = False
        if (not valid_id
                or type(offset) is not int or not 0 <= offset <= 100000
                or type(limit) is not int or not 1 <= limit <= 201
                or type(expected_file) is not dict or set(expected_file) != _FIELDS):
            _fail('invalid_arguments')
        cwd, project = _path(cwd), _path(project_dir)
        file_name = session_id + '.jsonl'
        path = project / file_name
        expected = dict(expected_file)
        if (expected['session_id'] != session_id or expected['cwd'] != str(cwd)
                or expected['path'] != str(path)
                or any(type(expected[key]) is not int or expected[key] < 0 for key in
                    ('device', 'inode', 'size', 'mtime_ns', 'cwd_device', 'cwd_inode'))):
            _fail('invalid_arguments')
        helper, key_helper = getattr(sdk, 'get_session_messages_from_store', None), getattr(sdk, 'project_key_for_directory', None)
        if not callable(helper) or not callable(key_helper):
            _fail('sdk_helper_unavailable')
        work = _directory(cwd); descriptors.append(work)
        parent = _directory(project); descriptors.append(parent)
        source = os.open(file_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        descriptors.append(source)
        first, work_info = os.fstat(source), os.fstat(work)
        _regular(first)
        descriptor = {'session_id': session_id, 'cwd': str(cwd), 'path': str(path),
            'device': first.st_dev, 'inode': first.st_ino, 'size': first.st_size,
            'mtime_ns': first.st_mtime_ns, 'cwd_device': work_info.st_dev, 'cwd_inode': work_info.st_ino}
        if descriptor != expected:
            _fail('source_changed')
        checked = dict(descriptor=descriptor, file_fd=source, parent_fd=parent, work_fd=work,
            project=project, cwd=cwd, file_name=file_name, first=first)
        _check(**checked)
        entries, length, digest = _entries(source, session_id=session_id,
            cwd=unicodedata.normalize('NFC', str(cwd)))
        _check(**checked)
        if length != first.st_size:
            _fail('source_changed')
        project_key = key_helper(str(cwd))
        if (type(project_key) is not str or not 0 < len(project_key) <= 1024
                or any(ord(char) < 32 or ord(char) == 127 for char in project_key)):
            _fail('sdk_read_failed')
        project_key.encode('utf-8', 'strict')
        _check(**checked)
        adapter = _ReadOnlyStore({'project_key': project_key, 'session_id': session_id}, entries)
        try:
            messages = await helper(adapter, session_id, directory=str(cwd), limit=limit, offset=offset)
        except ClaudeHistorySourceError:
            raise
        except Exception:
            raise ClaudeHistorySourceError('sdk_read_failed') from None
        if not adapter._loaded or type(messages) is not list or len(messages) > limit:
            _fail('sdk_read_failed')
        _check(**checked)
        if _digest(source) != (length, digest):
            _fail('source_changed')
        _check(**checked)
        return messages, descriptor
    except ClaudeHistorySourceError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        raise ClaudeHistorySourceError('source_unverified') from None
    finally:
        if adapter is not None:
            adapter.close()
        for descriptor in reversed(descriptors):
            os.close(descriptor)
