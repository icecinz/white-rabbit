"""Adopt one original Pi v3 session file without copying its conversation.

The existing tree validator streams the complete JSONL to verify identities,
parent links and a source hash. Only bounded header/index metadata and that
proof are retained. No RPC, Agent, repair, transcript copy or send occurs.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from rabbit_content.history_records import PI_COVERAGE_MAX_BYTES
from native_catalogue import _title, chat_id
from native_http_common import SpecialistHTTPError
from rabbit_runtime.native_session_store import open_directory as _directory, file_identity as _identity, validate_path as _path
from pi_history import META_KEY as PI_META_KEY
from rabbit_runtime.pi.session_store import validate_session_id as _session_id
from rabbit_runtime.pi.session_file import validate_session_file, decode_record as _record, file_stamp as _stamp

_NAMESPACE = uuid.UUID('873ae0d5-c97e-5ea6-a416-0f63b5859bd5')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
MAX_HEADER_BYTES = 1024 * 1024


class PiHistoryAdoptionError(ValueError):
    """Finite safe failure; no source body, private path or native raw error."""


def _error(reason): raise PiHistoryAdoptionError(reason)
def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def adoption_identities(workspace_id, source_session_id):
    if type(workspace_id) is not str or not _SHA.fullmatch(workspace_id):
        _error('invalid_service_scope')
    source_session_id = _session_id(source_session_id)
    basis = 'rabbit/adoption-v1/pi/' + workspace_id + '/' + source_session_id
    return str(uuid.uuid5(_NAMESPACE, basis+'/chat')), str(uuid.uuid5(_NAMESPACE, basis+'/creation'))


def is_adopted_binding(workspace_id, identity, source_session_id):
    try: return identity == adoption_identities(workspace_id, source_session_id)[0]
    except ValueError: return False


def _dir_scope(path):
    fd = _directory(path)
    try: return list(_identity(os.fstat(fd)))
    finally: os.close(fd)


class PiHistoryAdoption:
    def __init__(self, history, pi_root, catalogue, store, *, workspace_id, excluded_sources):
        if (getattr(catalogue, 'agent', None) != 'pi' or not callable(excluded_sources)
                or type(workspace_id) is not str or not _SHA.fullmatch(workspace_id)):
            _error('invalid_service_scope')
        self.history, self.root = history, _path(pi_root)
        self.catalogue, self.store = catalogue, store
        self.workspace_id, self.excluded_sources = workspace_id, excluded_sources
        self._source_scopes = {}

    def _indexed_source(self, session_id):
        # Never use get_session(): it projects bodies and attachment metadata.
        with closing(self.history._open()) as db:
            row = db.execute('SELECT session_id,title,cwd,parent_session_id,forked_from_id,kind,archived,source_path '
                'FROM sessions WHERE agent=? AND session_id=?', ('pi', session_id)).fetchone()
            coverage = db.execute('SELECT value FROM meta WHERE key=?', (PI_META_KEY,)).fetchone()
        if row is None: _error('source_not_found')
        value = dict(row)
        if (value['kind'] != 'root' or value['archived'] != 0 or value['parent_session_id'] is not None
                or value['forked_from_id'] is not None):
            _error('unsupported_source_branch')
        # Reuse the indexer's bounded coverage map solely for main/copy/export
        # identity. Text-projection continuable and message coverage are not
        # admission conditions for a complete original native session.
        if (coverage is None or type(coverage[0]) is not str
                or len(coverage[0].encode('utf-8')) > PI_COVERAGE_MAX_BYTES):
            _error('source_coverage_unverified_refresh_index')
        try: parsed = _record(coverage[0].encode('utf-8'))
        except (UnicodeError, ValueError, TypeError, RecursionError):
            _error('source_coverage_unverified_refresh_index')
        item = parsed.get(session_id) if type(parsed) is dict else None
        if (type(item) is not dict or item.get('source_session_id') != session_id
                or type(item.get('source_version')) is not int or item['source_version'] != 3
                or item.get('branch_kind') != 'main' or item.get('is_exported_branch') is not False
                or type(item.get('ambiguous_copies')) is not int or item['ambiguous_copies'] != 0):
            _error('source_coverage_unverified_refresh_index')
        path = _path(value['source_path'])
        if item.get('source_file_id') != hashlib.sha256(str(path).encode()).hexdigest():
            _error('source_coverage_path_mismatch')
        return value

    def _source(self, session_id):
        session_id = _session_id(session_id)
        value = self._indexed_source(session_id)
        path, cwd = _path(value['source_path']), _path(value['cwd'])
        sessions = self.root/'sessions'
        if path.suffix != '.jsonl' or sessions not in path.parents:
            _error('source_outside_active_sessions')
        relative = path.relative_to(sessions)
        if len(relative.parts) not in (1, 2) or any(part.startswith('.') for part in relative.parts):
            _error('source_outside_active_sessions')
        directories = {directory:_dir_scope(directory) for directory in dict.fromkeys((self.root,sessions,path.parent,cwd))}
        parent = descriptor = None
        try:
            parent = _directory(path.parent)
            descriptor = os.open(path.name, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=parent)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1: _error('source_unavailable')
            with os.fdopen(descriptor, 'rb', closefd=False) as source:
                raw = source.readline(MAX_HEADER_BYTES+1)
            if not raw.endswith(b'\n') or len(raw) > MAX_HEADER_BYTES: _error('invalid_source_header')
            header = _record(raw)
            if (type(header) is not dict or header.get('type') != 'session'
                    or type(header.get('version')) is not int or header['version'] != 3
                    or header.get('id') != session_id or header.get('cwd') != str(cwd)
                    or any(key in header for key in ('parentSession','parentSessionFile'))):
                _error('source_header_mismatch_or_exported_branch')
            metadata = validate_session_file(path, session_dir=path.parent, cwd=cwd, session_id=session_id)
            named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if (_stamp(before) != _stamp(os.fstat(descriptor)) or _stamp(before) != _stamp(named)
                    or [metadata['file_device'],metadata['file_inode']] != list(_identity(before))
                    or metadata['bytes'] != before.st_size or os.pread(descriptor,len(raw),0) != raw
                    or any(_dir_scope(directory) != identity for directory,identity in directories.items())):
                _error('source_changed')
            # Directory mtimes and unrelated files never enter the proof.
            scope = (tuple(directories[self.root]),tuple(directories[sessions]),
                tuple(directories[path.parent]),tuple(directories[cwd]),_identity(before))
            if session_id in self._source_scopes and self._source_scopes[session_id] != scope:
                _error('source_scope_changed')
            self._source_scopes[session_id] = scope
            title = value['title']
            if type(title) is not str: _error('invalid_source_title')
            title.encode('utf-8')
            return dict(session_id=session_id, title=_title((title.strip() or 'Pi '+session_id[:8])[:80]),
                source_title_sha256=_hash(title), cwd=str(cwd), cwd_identity=directories[cwd],
                session_dir=str(path.parent), session_dir_identity=directories[path.parent],
                source_path=str(path), source_identity=list(_identity(before)),
                root_identity=directories[self.root], sessions_identity=directories[sessions],
                header_sha256=hashlib.sha256(raw).hexdigest(), native_file=metadata)
        except (OSError, UnicodeError, RecursionError, json.JSONDecodeError):
            _error('source_unavailable')
        finally:
            for fd in (descriptor,parent):
                if fd is not None: os.close(fd)

    def _binding_matches(self, identity, source):
        binding = self.store.binding(identity)
        if (binding is None or binding['cwd'] != source['cwd'] or binding['session_dir'] != source['session_dir']
                or binding['session_file'] != source['source_path'] or binding['session_id'] != source['session_id']
                or [binding['cwd_device'],binding['cwd_inode']] != source['cwd_identity']
                or [binding['session_dir_device'],binding['session_dir_inode']] != source['session_dir_identity']
                or any(row['chat_id'] != identity and row['session_id'] == source['session_id']
                    for row in self.store.source_bindings())):
            _error('existing_binding_conflict')
        return binding

    def _prepare(self, session_id):
        source = self._source(session_id)
        identity, creation = adoption_identities(self.workspace_id, source['session_id'])
        if self.store.binding(identity) is not None:
            self._binding_matches(identity, source)
        elif ('pi',source['session_id']) in self.excluded_sources():
            _error('source_already_owned')
        return source, dict(source=source, workspace_id=self.workspace_id, chat_id=identity,
            creation_id=creation, history_mode='active_branch')

    def preview(self, session_id):
        source, proof = self._prepare(session_id)
        return dict(agent='pi', source_session_id=source['session_id'], title=source['title'], cwd=source['cwd'],
            workspace_id=self.workspace_id, revision=_hash(proof), history_mode='active_branch',
            execution_started=False, body_copied=False, current_executor_ownership='not_checked')

    def adopt(self, session_id, revision):
        if type(revision) is not str or not _SHA.fullmatch(revision): _error('invalid_preview_revision')
        source, proof = self._prepare(session_id)
        if _hash(proof) != revision: _error('source_preview_changed')
        identity = proof['chat_id']
        try: published = self.catalogue.get(identity)
        except SpecialistHTTPError as error:
            if error.status != 404 or error.code != 'native_chat_not_found': raise
            published = None
        if published is not None: self._binding_matches(identity, source)
        def reserve(value):
            if value != identity: _error('adoption_identity_changed')
            if self._source(session_id) != source: _error('source_changed')
            self.store.adopt(identity, source['cwd'], source['session_dir'], source['source_path'], source['session_id'])
            self._binding_matches(identity, source)
        result = self.catalogue.create(proof['creation_id'],source['title'],reserve,fixed_id=identity)
        self._binding_matches(identity, source)
        return dict(chat=result, source_session_id=source['session_id'], workspace_id=self.workspace_id,
            adopted=True, execution_started=False, body_copied=False)

    def verify_open(self, identity):
        identity = chat_id(identity); self.catalogue.get(identity)
        binding = self.store.binding(identity)
        if binding is None or not is_adopted_binding(self.workspace_id, identity, binding['session_id']):
            _error('existing_binding_conflict')
        source, proof = self._prepare(binding['session_id'])
        if proof['chat_id'] != identity: _error('existing_binding_conflict')
        self._binding_matches(identity, source)
