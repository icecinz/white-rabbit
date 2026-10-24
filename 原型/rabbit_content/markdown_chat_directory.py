"""Markdown organization and visible folders, independent of chat execution.

The caller supplies trusted accepted-file hashes from application metadata, not
from a document's self-declared hash. Paths are observations; rabbit_id is the
stable identity. Only explicitly requested empty folders may be created here;
files are never written or interpreted as messages. Scans are bounded observations.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import threading
import uuid

from .markdown_chat_document import MAX_DOCUMENT_BYTES, parse, document_identity

MAX_FILE_BYTES = MAX_DOCUMENT_BYTES
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_FILES = 1000
MAX_DEPTH = 8
_SHA = re.compile(r'[a-f0-9]{64}\Z')
_AGENTS = {'codex', 'pi', 'hermes', 'claude', 'dsh'}


class MarkdownDirectoryError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('Markdown目录暂不能完整核对：' + code)


def _identity(info):
    return info.st_dev, info.st_ino


def _state(info):
    return (_identity(info), info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink)


def _revision(value, native=False):
    if native:
        return type(value) is str and bool(_SHA.fullmatch(value))
    return (type(value) is str and bool(re.fullmatch(r'0|[1-9][0-9]{0,18}', value))
            and int(value) <= 2**63 - 1)


def _uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _identity_fields(identity):
    if _uuid(identity):
        return {'rabbit_id': identity}
    if (type(identity) is tuple and len(identity) == 3
            and identity[0] in ('native_index', 'history_reference')
            and type(identity[1]) is str and identity[1] in _AGENTS and _uuid(identity[2])):
        return {'rabbit_id': identity[2], 'kind': identity[0], 'agent': identity[1]}
    raise MarkdownDirectoryError('invalid_registry')


def _folder_parts(relative):
    if (type(relative) is not str or not relative or len(relative) > 240
            or relative.startswith('/') or '\\' in relative or ':' in relative
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                   for char in relative)):
        raise MarkdownDirectoryError('invalid_folder')
    parts = relative.split('/')
    # Publication counts the final .md name in the same eight-part path bound.
    if (len(parts) >= MAX_DEPTH or any(not part.strip() or part.startswith('.')
                                    or len(part.encode('utf-8')) > 255 for part in parts)):
        raise MarkdownDirectoryError('invalid_folder')
    return parts


class MarkdownChatDirectory:
    def __init__(self, root, *, workspace_id):
        self.root = Path(root)
        if (not self.root.is_absolute() or '..' in self.root.parts
                or type(workspace_id) is not str or not 1 <= len(workspace_id) <= 128
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127
                       or 0xD800 <= ord(c) <= 0xDFFF for c in workspace_id)):
            raise MarkdownDirectoryError('invalid_scope')
        self.workspace_id = workspace_id
        self._folder_lock = threading.RLock()
        descriptor, self._lineage = self._open_root()
        os.close(descriptor)

    def _open_root(self):
        descriptor = None
        lineage = []
        try:
            descriptor = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            lineage.append(_identity(os.fstat(descriptor)))
            for name in self.root.parts[1:]:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                lineage.append(_identity(os.fstat(descriptor)))
            return descriptor, lineage
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            raise MarkdownDirectoryError('scope_unavailable') from None

    @contextmanager
    def root_scope(self):
        """Borrow the bound root descriptor; check its identity before and after.

        The caller must not close the descriptor. Filesystem actions remain the
        caller's responsibility; this scope is an identity guard, not a sandbox.
        """
        descriptor, lineage = self._open_root()
        try:
            if lineage != self._lineage:
                raise MarkdownDirectoryError('scope_changed')
            yield descriptor
            named, after = self._open_root()
            os.close(named)
            if after != self._lineage:
                raise MarkdownDirectoryError('scope_changed')
        finally:
            os.close(descriptor)

    @staticmethod
    def _registry(references, current_revisions):
        if (type(references) is not dict or len(references) > MAX_FILES
                or type(current_revisions) is not dict or set(current_revisions) != set(references)):
            raise MarkdownDirectoryError('invalid_registry')
        for identity, reference in references.items():
            fields = _identity_fields(identity)
            indexed = fields.get('kind') in ('native_index', 'history_reference')
            if (type(reference) is not dict
                    or set(reference) != {'sha256', 'revision'}
                    or type(reference['sha256']) is not str or not _SHA.fullmatch(reference['sha256'])
                    or not _revision(reference['revision'], indexed)
                    or current_revisions[identity] is not None and not _revision(current_revisions[identity], indexed)
                    or fields.get('kind') == 'history_reference' and current_revisions[identity] is not None
                    and current_revisions[identity] != reference['revision']):
                raise MarkdownDirectoryError('invalid_registry')

    @staticmethod
    def _entry_count(descriptor):
        """Count bounded directory metadata, never open conversation bodies."""
        count = 0
        def visit(parent, depth):
            nonlocal count
            if depth > MAX_DEPTH:
                raise MarkdownDirectoryError('depth_capacity')
            before = os.fstat(parent)
            with os.scandir(parent) as entries:
                for entry in entries:
                    count += 1
                    if count > MAX_ENTRIES:
                        raise MarkdownDirectoryError('entry_capacity')
                    info = os.stat(entry.name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                        dir_fd=parent)
                        try:
                            if _identity(os.fstat(child)) != _identity(info):
                                raise MarkdownDirectoryError('directory_changed')
                            visit(child, depth + 1)
                        finally:
                            os.close(child)
            if _state(os.fstat(parent)) != _state(before):
                raise MarkdownDirectoryError('directory_changed')
        visit(descriptor, 0)
        return count

    def create_folder(self, relative):
        """Create one visible child of an existing parent, or reuse that folder.

        A relative path never selects another root. Missing parents, links and
        files are not replaced. One path component is reserved for a document;
        no conversation/publication binding is changed.
        """
        parts = _folder_parts(relative)
        opened, lineage = [], []
        try:
            with self._folder_lock, self.root_scope() as root:
                count = self._entry_count(root)
                parent = root
                for name in parts[:-1]:
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode):
                        raise MarkdownDirectoryError('invalid_folder_parent')
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                    dir_fd=parent)
                    opened.append(child)
                    if _identity(os.fstat(child)) != _identity(info):
                        raise MarkdownDirectoryError('directory_changed')
                    lineage.append((parent, name, _identity(info)))
                    parent = child

                def verify_parents():
                    for owner, name, expected in lineage:
                        info = os.stat(name, dir_fd=owner, follow_symlinks=False)
                        if not stat.S_ISDIR(info.st_mode) or _identity(info) != expected:
                            raise MarkdownDirectoryError('directory_changed')

                verify_parents()
                try:
                    info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    if count >= MAX_ENTRIES:
                        raise MarkdownDirectoryError('entry_capacity')
                    try:
                        os.mkdir(parts[-1], 0o700, dir_fd=parent)
                        created = True
                    except FileExistsError:
                        created = False
                    info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                else:
                    created = False
                if not stat.S_ISDIR(info.st_mode):
                    raise MarkdownDirectoryError('folder_conflict')
                child = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                dir_fd=parent)
                opened.append(child)
                if _identity(os.fstat(child)) != _identity(info):
                    raise MarkdownDirectoryError('directory_changed')
                verify_parents()
                # A concurrent external writer may have used the last slot.
                # Never delete another object to undo an uncertain observation.
                self._entry_count(root)
                verify_parents()
                named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(named.st_mode) or _identity(named) != _identity(os.fstat(child)):
                    raise MarkdownDirectoryError('directory_changed')
                os.fsync(parent)
            return {'path': relative, 'created': created}
        except FileNotFoundError:
            raise MarkdownDirectoryError('folder_parent_missing') from None
        except OSError:
            raise MarkdownDirectoryError('directory_unavailable') from None
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)

    def scan(self, references, *, current_revisions):
        """Return current/missing/duplicate/modified/stale without choosing a copy.

        References must come from a trusted acceptance record. A missing source
        revision is None; it never authorizes removing or recreating its file.
        Bad documents or unsafe paths mark the observation incomplete. The
        caller must not infer absence, accept a new binding or write from it.
        """
        self._registry(references, current_revisions)
        found, extras, issues, folders = {}, [], [], []
        budget = {'entries': 0, 'files': 0, 'read_files': 0, 'bytes': 0}

        def issue(path, code):
            issues.append({'path': path, 'code': code})

        def visit(directory, prefix, depth):
            if depth > MAX_DEPTH:
                raise MarkdownDirectoryError('depth_capacity')
            before = os.fstat(directory)
            names = []
            # Bound enumeration before collecting/sorting; hidden documents
            # retain their identity and also participate in duplicate checks.
            with os.scandir(directory) as entries:
                for entry in entries:
                    budget['entries'] += 1
                    if budget['entries'] > MAX_ENTRIES:
                        raise MarkdownDirectoryError('entry_capacity')
                    names.append(entry.name)
            for name in sorted(names):
                path = '/'.join((*prefix, name))
                if any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in name):
                    raise MarkdownDirectoryError('invalid_name')
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    issue(path, 'linked_entry')
                    continue
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                    dir_fd=directory)
                    try:
                        if _identity(os.fstat(child)) != _identity(info):
                            raise MarkdownDirectoryError('directory_changed')
                        if not any(part.startswith('.') for part in (*prefix, name)):
                            folders.append(path)
                        visit(child, (*prefix, name), depth + 1)
                    finally:
                        os.close(child)
                elif name.casefold().endswith('.md'):
                    budget['files'] += 1
                    if budget['files'] > MAX_FILES:
                        raise MarkdownDirectoryError('file_capacity')
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        issue(path, 'unsafe_file')
                        continue
                    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    try:
                        opened = os.fstat(descriptor)
                        if not stat.S_ISREG(opened.st_mode) or _state(opened) != _state(info):
                            raise MarkdownDirectoryError('file_changed')
                        if opened.st_size > MAX_FILE_BYTES or budget['bytes'] + opened.st_size > MAX_TOTAL_BYTES:
                            raise MarkdownDirectoryError('byte_capacity')
                        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
                            raw = handle.read(MAX_FILE_BYTES + 1)
                        if (len(raw) > MAX_FILE_BYTES or _state(os.fstat(descriptor)) != _state(opened)
                                or _state(os.stat(name, dir_fd=directory, follow_symlinks=False)) != _state(opened)):
                            raise MarkdownDirectoryError('file_changed')
                    finally:
                        os.close(descriptor)
                    budget['bytes'] += len(raw)
                    budget['read_files'] += 1
                    try:
                        document = parse(raw)
                    except (ValueError, UnicodeError):
                        issue(path, 'invalid_document')
                        continue
                    metadata = document['metadata']
                    row = {'path': path, 'folder': '/'.join(prefix), 'name': Path(name).stem,
                           'rabbit_id': metadata['rabbit_id'], 'sha256': hashlib.sha256(raw).hexdigest(),
                           'file_revision': metadata['revision']}
                    identity = document_identity(metadata)
                    if metadata['kind'] in ('native_index', 'history_reference'):
                        row.update(kind=metadata['kind'], agent=metadata['source']['agent'])
                    if metadata['workspace_id'] != self.workspace_id:
                        extras.append(dict(row, status='foreign_workspace'))
                    elif identity not in references:
                        extras.append(dict(row, status='unregistered'))
                    else:
                        found.setdefault(identity, []).append(row)
            if _state(os.fstat(directory)) != _state(before):
                raise MarkdownDirectoryError('directory_changed')

        try:
            with self.root_scope() as descriptor:
                visit(descriptor, (), 0)
        except OSError:
            raise MarkdownDirectoryError('directory_unavailable') from None
        rows = []
        for identity, reference in references.items():
            matches = found.get(identity, [])
            current = current_revisions[identity]
            # Navigation belongs to the trusted registry key. A changed file
            # header can be unregistered/modified, never choose another owner.
            row = {**_identity_fields(identity), 'paths': sorted(item['path'] for item in matches),
                   'source_revision': current, 'accepted_revision': reference['revision']}
            if len(matches) > 1:
                status = 'duplicate'
            elif not matches:
                status = 'unavailable' if issues else 'missing'
            elif matches[0]['sha256'] != reference['sha256']:
                status = 'external_modified'
            elif matches[0]['file_revision'] != reference['revision']:
                status = 'registry_mismatch'
            elif current is None:
                status = 'source_missing'
            elif current != reference['revision']:
                status = 'db_stale'
            else:
                status = 'current'
            row['status'] = status
            if len(matches) == 1:
                row['file'] = matches[0]
            rows.append(row)
        return {'workspace_id': self.workspace_id, 'complete': not issues,
                'folders': sorted(folders),
                'chats': sorted(rows, key=lambda row: (row.get('kind') in ('native_index', 'history_reference'),
                    row.get('agent', ''), row['rabbit_id'])),
                'unregistered': sorted(extras, key=lambda row: row['path']),
                'issues': issues, 'files_examined': budget['files'],
                'files_read': budget['read_files'], 'bytes_read': budget['bytes']}
