"""Adopt an existing Codex identity without copying or submitting its body.

Trusted Python service, not an HTTP path or arbitrary RPC interface. The caller
supplies its existing read-only index, native metadata reader and ordinary
catalogue/store. Native metadata reads must not resume or send. Preview is not
an executor lease; a later explicit open still goes through native admission.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from native_catalogue import _title
from native_http_common import SpecialistHTTPError
from rabbit_runtime.native_session_store import open_directory as _directory, file_identity as _identity, validate_path as _path

_NAMESPACE = uuid.UUID('cd58ee57-2c72-5064-92c9-87757e600196')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
MAX_HEADER_BYTES = 1024 * 1024


class CodexHistoryAdoptionError(ValueError):
    """A finite, private-data-free failure before opening or sending."""


def _error(reason):
    raise CodexHistoryAdoptionError(reason)


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError
        return value
    except (ValueError, AttributeError):
        _error('invalid_source_identity')


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            _error('invalid_source_header')
        result[key] = value
    return result


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _has_parent(value):
    if type(value) is not dict:
        return False
    if any(value.get(key) for key in ('parent_thread_id', 'parent_session_id')):
        return True
    return any(_has_parent(child) for child in value.values() if type(child) is dict)


def adoption_identities(workspace_id, thread_id):
    """Stable application identities, not native IDs or a new transcript."""
    if type(workspace_id) is not str or not _SHA.fullmatch(workspace_id):
        _error('invalid_service_scope')
    basis = 'rabbit/adoption-v1/codex/' + workspace_id + '/' + _uuid(thread_id)
    return (str(uuid.uuid5(_NAMESPACE, basis + '/chat')),
            str(uuid.uuid5(_NAMESPACE, basis + '/creation')))


def is_adopted_binding(workspace_id, identity, thread_id):
    try:
        return identity == adoption_identities(workspace_id, thread_id)[0]
    except CodexHistoryAdoptionError:
        return False


class CodexHistoryAdoption:
    def __init__(self, history, codex_root, catalogue, store, read_native, *,
                 workspace_id, excluded_sources=lambda: (), reserved_sources=lambda: ()):
        if (getattr(catalogue, 'agent', None) != 'codex' or not callable(read_native)
                or not callable(excluded_sources) or not callable(reserved_sources) or type(workspace_id) is not str
                or not _SHA.fullmatch(workspace_id)):
            _error('invalid_service_scope')
        self.history, self.root = history, _path(codex_root)
        self.catalogue, self.store, self.read_native = catalogue, store, read_native
        self.workspace_id, self.excluded_sources = workspace_id, excluded_sources
        self.reserved_sources = reserved_sources

    def _identities(self, thread_id):
        return adoption_identities(self.workspace_id, thread_id)

    def _source(self, session_id):
        session_id = _uuid(session_id)
        # The existing index handles publication/schema and has no materializer
        # call on this metadata-only path. Do not use get_session().
        with closing(self.history._open()) as db:
            row = db.execute('SELECT session_id,title,cwd,parent_session_id,forked_from_id,'
                'kind,archived,source_path FROM sessions WHERE agent=? AND session_id=?',
                ('codex', session_id)).fetchone()
        if row is None:
            _error('source_not_found')
        value = dict(row)
        if (value['kind'] != 'root' or value['archived'] != 0
                or value['parent_session_id'] is not None or value['forked_from_id'] is not None):
            _error('unsupported_source_branch')
        path, cwd = _path(value['source_path']), _path(value['cwd'])
        if path.suffix != '.jsonl' or self.root / 'sessions' not in path.parents:
            _error('source_outside_active_sessions')
        work = parent = descriptor = None
        try:
            work = _directory(cwd)
            cwd_identity = _identity(os.fstat(work))
            parent = _directory(path.parent)
            parent_identity = _identity(os.fstat(parent))
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                _error('source_unavailable')
            with os.fdopen(descriptor, 'rb', closefd=False) as handle:
                raw = handle.readline(MAX_HEADER_BYTES + 1)
            if not raw.endswith(b'\n') or len(raw) > MAX_HEADER_BYTES:
                _error('invalid_source_header')
            header = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
            payload = header.get('payload') if type(header) is dict else None
            if (type(payload) is not dict or header.get('type') != 'session_meta'
                    or (payload.get('id') or payload.get('session_id')) != session_id
                    or payload.get('cwd') != str(cwd)
                    or payload.get('forked_from_id') is not None
                    or _has_parent(payload)
                    or any(payload.get(key) for key in ('dynamicTools', 'dynamic_tools'))):
                _error('source_header_mismatch_or_unsupported_tools')
            after = os.fstat(descriptor)
            named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            # Appends are permitted: native resume reads the current original,
            # not a frozen text snapshot. Replacements and header edits are not.
            if _identity(before) != _identity(after) or _identity(before) != _identity(named):
                _error('source_changed')
            if os.pread(descriptor, len(raw), 0) != raw:
                _error('source_changed')
            for directory, expected in ((cwd, cwd_identity), (path.parent, parent_identity)):
                check = _directory(directory)
                try:
                    if _identity(os.fstat(check)) != expected:
                        _error('source_changed')
                finally:
                    os.close(check)
            title = value['title']
            if type(title) is not str:
                _error('invalid_source_title')
            title.encode('utf-8')
            display_title = _title((title.strip() or 'Codex ' + session_id[:8])[:80])
            return dict(session_id=session_id, title=display_title, source_title_sha256=_hash(title), cwd=str(cwd),
                cwd_identity=list(cwd_identity), source_path=str(path),
                source_identity=list(_identity(before)), header_sha256=hashlib.sha256(raw).hexdigest())
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
            _error('source_unavailable')
        finally:
            for fd in (descriptor, parent, work):
                if fd is not None:
                    os.close(fd)

    def _binding_matches(self, identity, source, thread):
        binding = self.store.verify_scope('chat/codex/' + identity)
        if (binding['cwd'] != source['cwd']
                or [binding['cwd_device'], binding['cwd_inode']] != source['cwd_identity']
                or binding['thread_id'] != thread['id']
                or binding['session_id'] != thread['sessionId']):
            _error('existing_binding_conflict')
        return binding

    def _prepare(self, session_id):
        source = self._source(session_id)
        if ('codex', source['session_id']) in self.reserved_sources():
            _error('source_reserved_for_specialist')
        # The trusted reader must separately check persisted dynamic tools;
        # thread/read does not include that database-backed list. Missing
        # evidence is not the same as an empty list of host callbacks.
        native = self.read_native(source['session_id'], source['cwd'])
        tool_evidence = native.get('persisted_dynamic_tools') if type(native) is dict else None
        if (type(tool_evidence) is not dict or tool_evidence.get('thread_id') != source['session_id']
                or type(tool_evidence.get('count')) is not int or tool_evidence['count'] != 0):
            _error('native_dynamic_tools_unverified_or_unsupported')
        thread = native.get('thread') if type(native) is dict else None
        if (type(thread) is not dict or thread.get('id') != source['session_id']
                or thread.get('cwd') != source['cwd'] or thread.get('ephemeral') is not False
                or thread.get('sessionId') != source['session_id']
                or thread.get('forkedFromId') is not None
                or thread.get('source') not in ('cli', 'vscode', 'exec', 'appServer')
                or thread.get('historyMode') != 'paginated'
                or any(thread.get(key) for key in ('dynamicTools', 'dynamic_tools'))
                or thread.get('turns') != []):
            _error('native_metadata_mismatch_or_unsupported_contract')
        # A second source observation prevents a metadata callback changing the
        # indexed source/header/cwd while the native reader was in flight.
        if self._source(session_id) != source:
            _error('source_changed')
        identity, creation_id = self._identities(thread['id'])
        binding = self.store.read_binding('chat/codex/' + identity)
        if binding is not None:
            self._binding_matches(identity, source, thread)
        elif ('codex', thread['id']) in self.excluded_sources():
            _error('source_already_owned')
        proof = dict(source=source, thread_id=thread['id'], session_id=thread['sessionId'],
            history_mode=thread['historyMode'], workspace_id=self.workspace_id,
            chat_id=identity, creation_id=creation_id, persisted_dynamic_tools_count=0)
        return source, thread, proof

    def preview(self, session_id):
        source, thread, proof = self._prepare(session_id)
        return dict(agent='codex', source_session_id=source['session_id'], title=source['title'],
            cwd=source['cwd'], workspace_id=self.workspace_id, revision=_hash(proof),
            history_mode=thread['historyMode'], execution_started=False,
            body_copied=False, current_executor_ownership='not_checked')

    def verify_open(self, identity):
        """Fresh metadata/tool check before explicitly resuming an adopted root."""
        identity = _uuid(identity)
        self.catalogue.get(identity)
        binding = self.store.read_binding('chat/codex/' + identity)
        if binding is None or not is_adopted_binding(self.workspace_id, identity, binding['thread_id']):
            _error('existing_binding_conflict')
        source, thread, proof = self._prepare(binding['thread_id'])
        if proof['chat_id'] != identity:
            _error('existing_binding_conflict')
        self._binding_matches(identity, source, thread)

    def adopt(self, session_id, revision):
        if type(revision) is not str or not _SHA.fullmatch(revision):
            _error('invalid_preview_revision')
        source, thread, proof = self._prepare(session_id)
        if _hash(proof) != revision:
            _error('source_preview_changed')
        identity = proof['chat_id']
        key = 'chat/codex/' + identity
        # A successful publication is repeatable after actual native use. An
        # unpublished reservation has no operations and can be adopted again
        # with the same fixed ID after a cross-database interruption.
        try:
            published = self.catalogue.get(identity)
        except SpecialistHTTPError as error:
            if error.status != 404 or error.code != 'native_chat_not_found':
                raise
            published = None
        if published is not None:
            self._binding_matches(identity, source, thread)
        def reserve(_identity):
            if _identity != identity:
                _error('adoption_identity_changed')
            if self._source(session_id) != source:
                _error('source_changed')
            self.store.adopt_thread(key, source['cwd'], thread['id'], thread['sessionId'])
            self._binding_matches(identity, source, thread)
        result = self.catalogue.create(proof['creation_id'], source['title'], reserve, fixed_id=identity)
        self._binding_matches(identity, source, thread)
        return dict(chat=result, source_session_id=source['session_id'], workspace_id=self.workspace_id,
            adopted=True, execution_started=False, body_copied=False)
