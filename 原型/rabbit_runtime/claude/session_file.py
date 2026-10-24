"""Verify one owned Claude transcript; never search, mutate or import an Agent.

The SDK supplies its exact project directory. Worktree/prefix fallbacks are not
ownership evidence. The original SDK remains the transcript reader and parser;
this module only verifies the file/metadata anchor around that read or resume.
"""
import json
import os
from pathlib import Path
import stat
import unicodedata
import uuid

from ..native_session_store import open_directory as _directory, file_identity as _identity, validate_path as _path
from .material_protocol import MAX_SESSION_FILE_BYTES, MAX_SESSION_LINE_BYTES

MAX_FILE_BYTES = MAX_SESSION_FILE_BYTES
MAX_LINE_BYTES = MAX_SESSION_LINE_BYTES


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:
        raise ValueError('Invalid exact Claude session identity.')
    return value


def verify_session_file(session_id, cwd, *, project_dir):
    """Return finite metadata only; no message body leaves this function."""
    identity = _uuid(session_id)
    cwd, project = _path(cwd), _path(project_dir)
    path, descriptors = project / (identity + '.jsonl'), []
    try:
        work = _directory(cwd); descriptors.append(work)
        work_info = os.fstat(work)
        parent = _directory(project); descriptors.append(parent)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or not 0 < before.st_size <= MAX_FILE_BYTES):
            raise ValueError('Claude transcript is unavailable or exceeds the read boundary.')
        matched, total = False, 0
        expected_cwd = unicodedata.normalize('NFC', str(cwd))
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            while True:
                raw = handle.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                total += len(raw)
                if len(raw) > MAX_LINE_BYTES or total > MAX_FILE_BYTES or not raw.endswith(b'\n'):
                    raise ValueError('Claude transcript is incomplete or exceeds the read boundary.')
                value = json.loads(raw.decode('utf-8'))
                if type(value) is not dict:
                    raise ValueError('Claude transcript metadata is not a mapping.')
                sid, directory = value.get('sessionId'), value.get('cwd')
                if sid is not None and sid != identity:
                    raise ValueError('Claude transcript contains a different session identity.')
                if directory is not None and (type(directory) is not str
                        or unicodedata.normalize('NFC', directory) != expected_cwd):
                    raise ValueError('Claude transcript contains a different working directory.')
                matched |= sid == identity and directory is not None
        after = os.fstat(descriptor)
        current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (not matched or _identity(before) != _identity(after)
                or _identity(before) != _identity(current)
                or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
                or _identity(work_info) != _identity(os.stat(cwd, follow_symlinks=False))):
            raise ValueError('Claude transcript changed while its identity was checked.')
        return {'session_id': identity, 'cwd': str(cwd), 'path': str(path),
                'device': before.st_dev, 'inode': before.st_ino,
                'size': before.st_size, 'mtime_ns': before.st_mtime_ns,
                'cwd_device': work_info.st_dev, 'cwd_inode': work_info.st_ino}
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Claude transcript identity could not be verified.') from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def recheck_session_file(descriptor, session_id, cwd, *, project_dir):
    """Permit native appends, reject replacement or a different original scope."""
    if type(descriptor) is not dict:
        raise ValueError('Missing original Claude transcript identity.')
    current = verify_session_file(session_id, cwd, project_dir=project_dir)
    for field in ('session_id', 'cwd', 'path', 'device', 'inode', 'cwd_device', 'cwd_inode'):
        if descriptor.get(field) != current[field]:
            raise ValueError('Claude transcript identity was replaced.')
    return current
