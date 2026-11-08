"""Bounded, read-only validation of one owned Pi v3 JSONL file.

Returns metadata and optionally the last saved branch's native Token counters.
Never repairs a file, enumerates sessions or proves that a particular prompt
was sent. The last persisted entry is not an arbitrary RAM branch cursor.
A caller must still guard the later native-open operation.
"""
import hashlib
import json
import math
import os
import stat

from ..native_session_store import NativeSessionStoreError, open_directory as _directory, file_identity as _identity, validate_path as _path
from .material_protocol import MAX_SESSION_FILE_BYTES, MAX_SESSION_RECORD_BYTES


MAX_FILE_BYTES = MAX_SESSION_FILE_BYTES
# Separate Pi-only budgets; raw record bytes exclude LF and include CR.
MAX_RECORD_BYTES = MAX_SESSION_RECORD_BYTES
MAX_HEADER_BYTES = 1024 * 1024
MAX_ENTRIES = 200000
_READ_BYTES = 65536
USAGE_FIELDS = ('input', 'output', 'cacheRead', 'cacheWrite')
_MAX_TOKENS = 2**63 - 1
_MISSING_USAGE = object()
_MESSAGES = {
    'invalid_arguments': 'Pi 会话文件核验参数无效。',
    'session_file_unavailable': '原有 Pi 会话文件暂不可读取，未补建或修复。',
    'session_file_invalid': 'Pi 会话文件结构或原生身份不符，未继续恢复。',
    'session_file_too_large': 'Pi 会话文件超过本次核验上限。',
    'session_file_changed': 'Pi 会话文件或目录在核验时发生变化，未继续恢复。',
}


class PiSessionFileError(ValueError):
    def __init__(self, code):
        self.code = code if code in _MESSAGES else 'session_file_invalid'
        super().__init__(_MESSAGES[self.code])


def file_stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('nonfinite JSON number')
    return value


def _constant(_text):
    raise ValueError('nonfinite JSON constant')


def decode_record(raw):
    # Decode each complete LF-framed record. JSON strings may contain U+2028,
    # U+2029 or other Unicode separators: none of them split a record here.
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
        parse_float=_float, parse_constant=_constant)


def _entry_usage(entry):
    if entry.get('type') != 'message':
        return None
    message = entry.get('message')
    if type(message) is not dict or type(message.get('role')) is not str:
        return _MISSING_USAGE
    if message['role'] != 'assistant':
        return None
    usage = message.get('usage')
    if type(usage) is not dict:
        return _MISSING_USAGE
    counts = tuple(usage.get(field) for field in USAGE_FIELDS)
    if any(type(count) is not int or not 0 <= count <= _MAX_TOKENS for count in counts):
        return _MISSING_USAGE
    return counts


def _branch_usage(entries, leaf):
    # Compaction records are summaries. The saved ancestor chain still holds
    # original counters, including those for messages before compaction.
    totals = [0] * len(USAGE_FIELDS)
    observed = False
    while leaf is not None:
        leaf, counts = entries[leaf]
        if counts is _MISSING_USAGE:
            return None
        if counts is not None:
            observed = True
            for index, count in enumerate(counts):
                totals[index] += count
    total = sum(totals)
    if not observed or total > _MAX_TOKENS:
        return None
    return dict(zip(USAGE_FIELDS, totals), total=total)


def _scan(fd, *, session_id, cwd, include_usage=False):
    """Read/hash once, retaining one unfinished record and the tree's IDs.

    A malformed record is remembered while the remaining bounded bytes are
    drained. The caller can therefore check the original file/directory stamps
    before reporting a content error, as with the former read-then-parse path.
    No partial JSON record is accepted and no complete payload is retained.
    """
    digest, total = hashlib.sha256(), 0
    pending, entries = bytearray(), {}
    header_seen, leaf, problem = False, None, None

    def accept(raw):
        nonlocal header_seen, leaf
        if include_usage and not header_seen and len(raw) + 1 > MAX_HEADER_BYTES:
            raise PiSessionFileError('session_file_too_large')
        value = decode_record(raw)
        if not header_seen:
            if (type(value) is not dict or value.get('type') != 'session'
                    or type(value.get('version')) is not int or value['version'] != 3
                    or value.get('id') != session_id or value.get('cwd') != str(cwd)):
                raise PiSessionFileError('session_file_invalid')
            if include_usage and any(key in value for key in ('parentSession', 'parentSessionFile')):
                raise PiSessionFileError('session_file_invalid')
            header_seen = True
            return
        if len(entries) >= MAX_ENTRIES:
            raise PiSessionFileError('session_file_too_large')
        if (type(value) is not dict or type(value.get('id')) is not str or not value['id']
                or value['id'] in entries or 'parentId' not in value
                or value['parentId'] is not None and
                    (type(value['parentId']) is not str or value['parentId'] not in entries)):
            raise PiSessionFileError('session_file_invalid')
        leaf = value['id']
        entries[leaf] = (value['parentId'], _entry_usage(value)) if include_usage else None

    while True:
        chunk = os.read(fd, min(_READ_BYTES, MAX_FILE_BYTES + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_FILE_BYTES:
            raise PiSessionFileError('session_file_too_large')
        digest.update(chunk)
        if problem is not None:
            continue
        start = 0
        while start < len(chunk):
            newline = chunk.find(b'\n', start)
            end = len(chunk) if newline == -1 else newline
            if len(pending) + end - start > MAX_RECORD_BYTES:
                problem = 'session_file_too_large'
                pending.clear()
                break
            pending.extend(chunk[start:end])
            if newline == -1:
                break
            try:
                accept(pending)
            except PiSessionFileError as error:
                problem = error.code
            except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
                problem = 'session_file_invalid'
            pending.clear()
            if problem is not None:
                break
            start = newline + 1
    if problem is None and (not header_seen or pending):
        problem = 'session_file_invalid'
    result = {'leaf_id': leaf, 'entry_count': len(entries), 'file_sha256': digest.hexdigest(), 'bytes': total}
    if include_usage and problem is None:
        result['branch_usage'] = _branch_usage(entries, leaf)
    return result, problem


def _stable(path, directories, fd, expected):
    if file_stamp(os.fstat(fd)) != expected:
        raise PiSessionFileError('session_file_changed')
    for directory, (_held, identity) in directories.items():
        current = _directory(directory)
        try:
            if _identity(os.fstat(current)) != identity:
                raise PiSessionFileError('session_file_changed')
            if directory == path.parent:
                info = os.stat(path.name, dir_fd=current, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or file_stamp(info) != expected:
                    raise PiSessionFileError('session_file_changed')
        finally:
            os.close(current)


def _locations(path, session_dir, cwd):
    path, session_dir, cwd = _path(path), _path(session_dir), _path(cwd)
    if path.suffix != '.jsonl' or session_dir not in path.parents:
        raise PiSessionFileError('invalid_arguments')
    return path, session_dir, cwd


def _directories(path, session_dir, cwd, descriptors):
    # Keep original directory handles, then reopen exact paths after reading.
    directories = {}
    for directory in dict.fromkeys((session_dir, cwd, path.parent)):
        fd = _directory(directory)
        descriptors.append(fd)
        directories[directory] = fd, _identity(os.fstat(fd))
    return directories


def _usage_scope(path, directories, info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
        raise PiSessionFileError('session_file_invalid')
    return (tuple((str(directory), identity) for directory, (_fd, identity) in directories.items()),
        str(path), *_identity(info), getattr(info, 'st_birthtime_ns', getattr(info, 'st_birthtime', None)))


def session_usage_scope(path, *, session_dir, cwd):
    """Observe one exact file's metadata without opening its body."""
    descriptors = []
    try:
        path, session_dir, cwd = _locations(path, session_dir, cwd)
        directories = _directories(path, session_dir, cwd, descriptors)
        named = os.stat(path.name, dir_fd=directories[path.parent][0], follow_symlinks=False)
        return _usage_scope(path, directories, named)
    except (OSError, NativeSessionStoreError):
        raise PiSessionFileError('session_file_unavailable') from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def validate_session_file(path, *, session_dir, cwd, session_id, usage_scope=None):
    try:
        path, session_dir, cwd = _locations(path, session_dir, cwd)
        if type(session_id) is not str or not session_id:
            raise PiSessionFileError('invalid_arguments')
        session_id.encode('utf-8')
    except (NativeSessionStoreError, UnicodeError):
        raise PiSessionFileError('invalid_arguments') from None
    descriptors = []
    try:
        directories = _directories(path, session_dir, cwd, descriptors)
        parent = directories[path.parent][0]
        named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(named.st_mode):
            raise PiSessionFileError('session_file_invalid')
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or file_stamp(named) != file_stamp(before):
            raise PiSessionFileError('session_file_changed')
        if usage_scope is not None and _usage_scope(path, directories, before) != usage_scope:
            raise PiSessionFileError('session_file_changed')
        if before.st_size > MAX_FILE_BYTES:
            raise PiSessionFileError('session_file_too_large')
        result, problem = _scan(fd, session_id=session_id, cwd=cwd, include_usage=usage_scope is not None)
        after = os.fstat(fd)
        if result['bytes'] != before.st_size or file_stamp(before) != file_stamp(after):
            raise PiSessionFileError('session_file_changed')
        _stable(path, directories, fd, file_stamp(before))
        if problem is not None:
            raise PiSessionFileError(problem)
        _stable(path, directories, fd, file_stamp(before))
        return {'session_id': session_id, 'cwd': str(cwd), **result,
            'file_device': before.st_dev, 'file_inode': before.st_ino}
    except PiSessionFileError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        raise PiSessionFileError('session_file_invalid') from None
    except (OSError, NativeSessionStoreError):
        raise PiSessionFileError('session_file_unavailable') from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
