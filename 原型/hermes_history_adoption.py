"""Adopt an existing CLI identity; original Hermes owns history and execution.

Only index/session metadata is read. A trusted injected guard separately checks
interrupted turns and live owners. No RPC, home copy or transcript copy occurs.
"""
from contextlib import closing
import hashlib
import json
import os
import re
import sqlite3
import stat
from urllib.parse import quote
import uuid

from hermes_native import _native_keys
from rabbit_runtime.hermes.session_store import validate_session_id as _session_id, MAX_ADOPT_KEYS
from native_catalogue import _title, chat_id
from native_http_common import SpecialistHTTPError
from native_maintenance import _sidecars
from rabbit_runtime.native_session_store import open_directory as _directory, file_identity as _identity, validate_path as _path

_NAMESPACE = uuid.UUID('e8374308-05ef-4c53-bcf4-9211fd36f534')
_SHA = re.compile(r'[0-9a-f]{64}\Z')


class HermesHistoryAdoptionError(ValueError):
    """Finite safe error code; never source text, authentication or a traceback."""


def _error(reason): raise HermesHistoryAdoptionError(reason)
def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def adoption_identities(workspace_id, source_session_id):
    if type(workspace_id) is not str or not _SHA.fullmatch(workspace_id):
        _error('invalid_service_scope')
    source_session_id = _session_id(source_session_id)
    basis = 'rabbit/adoption-v1/hermes/' + workspace_id + '/' + source_session_id
    return (str(uuid.uuid5(_NAMESPACE, basis+'/chat')), str(uuid.uuid5(_NAMESPACE, basis+'/creation')))


def is_adopted_binding(workspace_id, identity, source_session_id):
    try: return identity == adoption_identities(workspace_id, source_session_id)[0]
    except ValueError: return False


def _dir_scope(path):
    descriptor = _directory(path)
    try: return list(_identity(os.fstat(descriptor)))
    finally: os.close(descriptor)


class HermesHistoryAdoption:
    def __init__(self, history, native_db, catalogue, store, *, workspace_id,
                 excluded_sources, guard_resume):
        if (getattr(catalogue, 'agent', None) != 'hermes' or not callable(excluded_sources)
                or not callable(guard_resume) or type(workspace_id) is not str or not _SHA.fullmatch(workspace_id)):
            _error('invalid_service_scope')
        self.history, self.native_db = history, _path(native_db)
        if self.native_db.name != 'state.db': _error('invalid_service_scope')
        self.catalogue, self.store, self.workspace_id = catalogue, store, workspace_id
        self.excluded_sources, self.guard_resume = excluded_sources, guard_resume
        self._native_scope = None

    def _source(self, session_id):
        session_id = _session_id(session_id)
        with closing(self.history._open()) as db:
            row = db.execute('SELECT session_id,title,cwd,parent_session_id,forked_from_id,kind,archived,source_path '
                'FROM sessions WHERE agent=? AND session_id=?', ('hermes', session_id)).fetchone()
        if row is None: _error('source_not_found')
        index = dict(row)
        if (index['kind'] != 'root' or index['archived'] != 0 or index['parent_session_id'] is not None
                or index['forked_from_id'] is not None or _path(index['source_path']) != self.native_db):
            _error('unsupported_source')
        cwd, home = _path(index['cwd']), self.native_db.parent
        home_scope, cwd_scope = _dir_scope(home), _dir_scope(cwd)
        descriptor = _directory(home)
        try:
            info = os.stat(self.native_db.name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: _error('native_scope_changed')
            scope = (tuple(home_scope), _identity(info))
            if self._native_scope is None: self._native_scope = scope
            if self._native_scope != scope: _error('native_scope_changed')
            sidecars = _sidecars(self.native_db)
            binding = dict(cwd=str(cwd), cwd_device=cwd_scope[0], cwd_inode=cwd_scope[1],
                profile_home=str(home), home_device=home_scope[0], home_inode=home_scope[1], session_key=session_id)
            chain = _native_keys(binding)
            if (type(chain) is not list or not 1 <= len(chain) <= MAX_ADOPT_KEYS
                    or chain[0] != session_id or len(set(chain)) != len(chain)):
                _error('native_chain_invalid')
            chain = tuple(_session_id(key) for key in chain)
            with closing(sqlite3.connect('file:'+quote(str(self.native_db), safe='/')+'?mode=ro', uri=True, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA query_only=ON'); db.execute('PRAGMA trusted_schema=OFF'); db.execute('BEGIN')
                table = db.execute("SELECT type,sql FROM sqlite_master WHERE name='sessions'").fetchone()
                if table is None or table[0] != 'table' or not table[1].lstrip().upper().startswith('CREATE TABLE '):
                    _error('native_schema_unverified')
                cfg = "COALESCE(model_config,'{}')"
                columns = ('id,parent_session_id,end_reason,cwd,source,archived,'
                    f'CASE WHEN json_valid({cfg}) THEN json_type({cfg}) ELSE NULL END AS config_type,' + ','.join(
                    f"CASE WHEN json_valid({cfg}) THEN COALESCE(json_type({cfg}, '$.{key}'),'null') != 'null' ELSE 1 END AS {key}"
                    for key in ('_branched_from', '_delegate_from', '_reset_from')))
                rows = []
                for position, key in enumerate(chain):
                    value = db.execute('SELECT '+columns+' FROM sessions WHERE id=?', (key,)).fetchone()
                    if value is None: _error('native_chain_changed')
                    value = dict(value)
                    if (value['cwd'] != str(cwd) or value['archived'] != 0 or value['config_type'] != 'object'
                            or any(value[key] != 0 for key in ('_branched_from','_delegate_from','_reset_from'))
                            or value['parent_session_id'] != (None if position == 0 else chain[position-1])
                            or position == 0 and value['source'] != 'cli'
                            or position > 0 and value['source'] == 'tool'
                            or position < len(chain)-1 and value['end_reason'] != 'compression'):
                        _error('unsupported_native_source')
                    rows.append(value)
            if (_identity(os.stat(self.native_db.name, dir_fd=descriptor, follow_symlinks=False)) != _identity(info)
                    or _dir_scope(home) != home_scope or _dir_scope(cwd) != cwd_scope
                    or sidecars != _sidecars(self.native_db) or tuple(_native_keys(binding)) != chain):
                _error('native_scope_changed')
            title = index['title']
            if type(title) is not str: _error('invalid_source_title')
            title.encode('utf-8')
            return dict(session_id=session_id, title=_title((title.strip() or 'Hermes '+session_id[:8])[:80]),
                source_title_sha256=_hash(title), cwd=str(cwd), cwd_identity=cwd_scope,
                profile_home=str(home), home_identity=home_scope, source_path=str(self.native_db),
                source_identity=list(_identity(info)), chain=list(chain), rows=rows)
        except (OSError, UnicodeError, sqlite3.Error, RecursionError):
            _error('source_unavailable')
        finally: os.close(descriptor)

    def _binding_matches(self, identity, source):
        binding = self.store.binding(identity)
        owners = self.store.source_bindings()
        keys = {row['session_id'] for row in owners if row['chat_id'] == identity}
        chain = source['chain']
        if (binding is None or binding['cwd'] != source['cwd'] or binding['profile_home'] != source['profile_home']
                or [binding['cwd_device'],binding['cwd_inode']] != source['cwd_identity']
                or [binding['home_device'],binding['home_inode']] != source['home_identity']
                or binding['session_key'] not in chain or source['session_id'] not in keys
                or any(row['chat_id'] != identity and row['session_id'] in chain for row in owners)
                or keys != set(chain[:chain.index(binding['session_key'])+1])):
            _error('existing_binding_conflict')
        return binding

    def _prepare(self, session_id):
        source = self._source(session_id)
        binding = dict(cwd=source['cwd'], cwd_device=source['cwd_identity'][0], cwd_inode=source['cwd_identity'][1],
            profile_home=source['profile_home'], home_device=source['home_identity'][0],
            home_inode=source['home_identity'][1], session_key=source['session_id'])
        guard = self.guard_resume(binding, tuple(source['chain']))
        if type(guard) is not dict: _error('resume_guard_rejected')
        if self._source(session_id) != source: _error('source_changed')
        identity, creation = adoption_identities(self.workspace_id, source['session_id'])
        if self.store.binding(identity) is not None:
            self._binding_matches(identity, source)
        else:
            excluded = set(self.excluded_sources())
            if any(('hermes', key) in excluded for key in source['chain']): _error('source_already_owned')
        proof = dict(source=source, workspace_id=self.workspace_id, chat_id=identity, creation_id=creation,
            history_mode='stored_session', resume_guard=guard)
        return source, proof

    def preview(self, session_id):
        source, proof = self._prepare(session_id)
        return dict(agent='hermes', source_session_id=source['session_id'], title=source['title'], cwd=source['cwd'],
            workspace_id=self.workspace_id, revision=_hash(proof), history_mode='stored_session',
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
            self.store.adopt(identity, source['cwd'], source['profile_home'], source['chain'])
            self._binding_matches(identity, source)
        result = self.catalogue.create(proof['creation_id'], source['title'], reserve, fixed_id=identity)
        self._binding_matches(identity, source)
        return dict(chat=result, source_session_id=source['session_id'], workspace_id=self.workspace_id,
            adopted=True, execution_started=False, body_copied=False)

    def verify_open(self, identity):
        identity = chat_id(identity)
        self.catalogue.get(identity)
        candidates = [row['session_id'] for row in self.store.source_bindings() if row['chat_id'] == identity
            and is_adopted_binding(self.workspace_id, identity, row['session_id'])]
        if len(candidates) != 1: _error('existing_binding_conflict')
        source, proof = self._prepare(candidates[0])
        if proof['chat_id'] != identity: _error('existing_binding_conflict')
        self._binding_matches(identity, source)
