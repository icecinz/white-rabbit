"""Prepare the specialist's one local instruction file before native startup.

The trusted caller supplies the bounded project reader. No model settings,
native thread, global configuration or per-turn input is changed here. Existing
local instructions are preserved; the project original is not a live sync.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat


MAX_SOURCE_BYTES = 8 * 1024
MAX_WORK_BYTES = 12 * 1024


class SpecialistWorkspaceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('专员工作说明尚未准备好：' + code)


def _fail(code):
    raise SpecialistWorkspaceError(code)


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino


def _directory_unchanged(path, directory):
    try:
        actual = os.stat(path, follow_symlinks=False)
        expected = os.fstat(directory)
    except OSError:
        _fail('workspace_directory_changed')
    if not stat.S_ISDIR(actual.st_mode) or _identity(actual) != _identity(expected):
        _fail('workspace_directory_changed')


def _remove_owned(directory, name, identity):
    try:
        actual = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if _identity(actual) != identity:
        return False
    os.unlink(name, dir_fd=directory)
    return True


def _override(directory):
    try:
        os.stat('AGENTS.override.md', dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    _fail('local_override_present')


def _existing(directory):
    _override(directory)
    try:
        descriptor = os.open('AGENTS.md', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError:
        _fail('local_document_unreadable')
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            _fail('local_document_not_independent_file')
        if metadata.st_size > MAX_WORK_BYTES:
            _fail('local_document_too_large')
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            raw = handle.read(MAX_WORK_BYTES + 1)
        if len(raw) > MAX_WORK_BYTES:
            _fail('local_document_too_large')
        try:
            raw.decode('utf-8')
        except UnicodeError:
            _fail('local_document_not_utf8')
        return {'state': 'existing_local', 'sha256': hashlib.sha256(raw).hexdigest(),
                'size_bytes': len(raw), 'source_snapshot': None,
                'source_updated_at': None, 'source_sha256': None}
    finally:
        os.close(descriptor)


def _source(reader):
    if not callable(reader):
        _fail('project_reader_unavailable')
    value = reader()
    if type(value) is not dict or type(value.get('content')) is not str:
        _fail('invalid_project_document')
    text = value['content']
    try:
        raw = text.encode('utf-8')
    except UnicodeError:
        _fail('project_document_not_utf8')
    if not text.strip() or '\x00' in text or len(raw) > MAX_SOURCE_BYTES:
        _fail('project_document_empty_or_too_large')
    for name in ('path', 'source_root'):
        path = value.get(name)
        try:
            path_size = len(path.encode('utf-8')) if type(path) is str else 0
        except UnicodeError:
            _fail('invalid_project_origin')
        if (type(path) is not str or not path or path_size > 2048
                or not Path(path).is_absolute() or any(ord(c) < 32 for c in path)):
            _fail('invalid_project_origin')
    snapshot = value.get('snapshot')
    updated = value.get('updated_at')
    if type(snapshot) is not bool or snapshot and updated is None or (updated is not None and
            (type(updated) is not str or not 1 <= len(updated) <= 100 or any(ord(c) < 32 for c in updated))):
        _fail('invalid_project_snapshot_metadata')
    origin = json.dumps(value['path'], ensure_ascii=False)
    project = json.dumps(value['source_root'], ensure_ascii=False)
    header = ('# 小白兔专员的独立工作目录\n\n'
              '项目原目录：' + project + '\n原始说明文档：' + origin + '\n'
              '下文相对资料链接以原始说明所在目录为基准；没有自动复制或读取链接资料。\n'
              '本文件在首次准备时保存；项目原稿后来变化不会自动覆盖本地说明。\n')
    if snapshot:
        header += '本次角色说明来自项目文字快照；不代表当前原稿实时状态。\n'
    content = (header + '\n---\n\n' + text).encode('utf-8')
    if len(content) > MAX_WORK_BYTES:
        _fail('prepared_document_too_large')
    return content, {'state': 'created', 'sha256': hashlib.sha256(content).hexdigest(),
        'size_bytes': len(content), 'source_snapshot': snapshot,
        'source_updated_at': updated, 'source_sha256': hashlib.sha256(raw).hexdigest()}


def prepare_workspace(cwd, read_document, *, bound_thread_id=None):
    """Publish a new complete AGENTS, or inspect/preserve an existing local one.

    The caller's Service lock precedes this function. A no-replace hard link
    publishes a fully flushed file, so a competing creator is never overwritten.
    A bound thread with a missing file is not silently reconfigured or replaced.
    """
    cwd = Path(os.path.abspath(cwd))
    directory = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = temporary_identity = None
    try:
        _directory_unchanged(cwd, directory)
        previous = _existing(directory)
        if previous is not None:
            _directory_unchanged(cwd, directory)
            return previous
        if bound_thread_id is not None:
            _fail('missing_document_for_bound_thread')
        content, metadata = _source(read_document)
        _directory_unchanged(cwd, directory)
        candidate = '.rabbit-instructions-' + secrets.token_hex(12)
        descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        temporary = candidate
        temporary_identity = _identity(os.fstat(descriptor))
        try:
            with os.fdopen(descriptor, 'wb', closefd=False) as handle:
                handle.write(content)
                handle.flush()
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _override(directory)
        if _identity(os.stat(temporary, dir_fd=directory, follow_symlinks=False)) != temporary_identity:
            _fail('temporary_document_changed')
        try:
            os.link(temporary, 'AGENTS.md', src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
        except FileExistsError:
            previous = _existing(directory)
            if previous is None:
                _fail('competing_document_disappeared')
            _directory_unchanged(cwd, directory)
            return previous
        if not _remove_owned(directory, temporary, temporary_identity):
            _fail('temporary_document_changed')
        temporary = None
        os.fsync(directory)
        _directory_unchanged(cwd, directory)
        actual = _existing(directory)
        if actual is None:
            _fail('published_document_disappeared')
        _directory_unchanged(cwd, directory)
        published_identity = _identity(os.stat('AGENTS.md', dir_fd=directory, follow_symlinks=False))
        return metadata if (actual['sha256'] == metadata['sha256'] and
                            published_identity == temporary_identity) else actual
    finally:
        if temporary is not None:
            _remove_owned(directory, temporary, temporary_identity)
        os.close(directory)
