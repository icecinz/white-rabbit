"""Link an exact original Claude file; never copy or create its conversation.

Verification streams the full JSONL. Body values are temporary parse inputs,
not retained, returned or written. The trusted project resolver must implement
this installation's SDK path selection; no directory-prefix search or guessed
cwd is accepted. Preview does not acquire an external CLI executor lease.
"""
from contextlib import closing
import hashlib
import json
import os
import re
import stat
import unicodedata
import uuid

from claude_history import META_KEY as CLAUDE_META_KEY
from rabbit_runtime.claude.session_store import validate_uuid as _uuid
from rabbit_runtime.claude.session_file import verify_session_file, MAX_FILE_BYTES, MAX_LINE_BYTES
from native_catalogue import _title, chat_id
from native_http_common import SpecialistHTTPError
from rabbit_runtime.native_session_store import open_directory as _directory, file_identity as _identity, validate_path as _path

_NAMESPACE = uuid.UUID('8e0a9fc4-476b-5a56-b326-7e20127e5581')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
MAX_COVERAGE_BYTES = 2*1024*1024  # Same published bound as HistoryView.
HISTORY_MODE = 'sdk_history'


class ClaudeHistoryAdoptionError(ValueError):
    """Finite safe failure; no source text, private path or native raw error."""


def _error(reason): raise ClaudeHistoryAdoptionError(reason)
def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _pairs(values):
    result={}
    for key,value in values:
        if key in result: _error('invalid_source_json')
        result[key]=value
    return result


def _constant(_value): _error('invalid_source_json')
def _record(raw):
    return json.loads(raw.decode('utf-8'),object_pairs_hook=_pairs,parse_constant=_constant)


def _stamp(info):
    return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns


def adoption_identities(workspace_id, source_session_id):
    if type(workspace_id) is not str or not _SHA.fullmatch(workspace_id): _error('invalid_service_scope')
    source_session_id=_uuid(source_session_id)
    basis='rabbit/adoption-v1/claude/'+workspace_id+'/'+source_session_id
    return str(uuid.uuid5(_NAMESPACE,basis+'/chat')),str(uuid.uuid5(_NAMESPACE,basis+'/creation'))


def is_adopted_binding(workspace_id, identity, source_session_id):
    try: return identity==adoption_identities(workspace_id,source_session_id)[0]
    except ValueError: return False


def _dir_scope(path):
    descriptor=_directory(path)
    try: return list(_identity(os.fstat(descriptor)))
    finally: os.close(descriptor)


class ClaudeHistoryAdoption:
    def __init__(self, history, claude_root, catalogue, store, *, workspace_id, excluded_sources,
                 project_resolver=None):
        if (getattr(catalogue,'agent',None)!='claude' or not callable(excluded_sources)
                or project_resolver is not None and not callable(project_resolver)
                or type(workspace_id) is not str or not _SHA.fullmatch(workspace_id)):
            _error('invalid_service_scope')
        self.history,self.root=history,_path(claude_root)
        self.catalogue,self.store,self.workspace_id=catalogue,store,workspace_id
        self.excluded_sources,self.project_resolver=excluded_sources,project_resolver
        self._source_scopes={}

    def _indexed_source(self, session_id):
        with closing(self.history._open()) as db:
            row=db.execute('SELECT session_id,title,cwd,parent_session_id,forked_from_id,kind,archived,source_path '
                'FROM sessions WHERE agent=? AND session_id=?',('claude',session_id)).fetchone()
            coverage=db.execute('SELECT value FROM meta WHERE key=?',(CLAUDE_META_KEY,)).fetchone()
        if row is None: _error('source_not_found')
        value=dict(row)
        if (value['kind']!='root' or value['archived']!=0 or value['parent_session_id'] is not None
                or value['forked_from_id'] is not None): _error('unsupported_source_branch')
        if (coverage is None or type(coverage[0]) is not str
                or len(coverage[0].encode('utf-8'))>MAX_COVERAGE_BYTES):
            _error('source_coverage_unverified_refresh_index')
        try: metadata=_record(coverage[0].encode('utf-8'))
        except (ValueError,UnicodeError,RecursionError): _error('source_coverage_unverified_refresh_index')
        item=metadata.get(session_id) if type(metadata) is dict else None
        if (type(item) is not dict or item.get('source_session_id')!=session_id or item.get('branch_kind')!='main'
                or type(item.get('ambiguous_copies')) is not int or item['ambiguous_copies']!=0):
            _error('source_coverage_unverified_refresh_index')
        # Full native resume is not limited by text-projection continuable.
        return value

    def _source(self, session_id):
        session_id=_uuid(session_id);value=self._indexed_source(session_id)
        cwd,path=_path(value['cwd']),_path(value['source_path'])
        projects=self.root/'projects'
        if (path.name!=session_id+'.jsonl' or path.parent.parent!=projects
                or path.parent.name.startswith('.') or len(path.parent.name)>200): _error('source_outside_exact_project')
        if self.project_resolver is None: _error('sdk_project_resolver_required')
        resolved=_path(self.project_resolver(str(cwd)))
        if resolved!=path.parent: _error('sdk_project_scope_mismatch')
        directories={directory:_dir_scope(directory) for directory in dict.fromkeys((self.root,projects,path.parent,cwd))}
        descriptor=verify_session_file(session_id,cwd,project_dir=resolved)
        parent=source=None
        try:
            parent=_directory(path.parent)
            source=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            before=os.fstat(source)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1
                    or [before.st_dev,before.st_ino]!=[descriptor['device'],descriptor['inode']]
                    or before.st_size!=descriptor['size'] or before.st_mtime_ns!=descriptor['mtime_ns']):
                _error('source_changed')
            digest,total,records=hashlib.sha256(),0,0
            with os.fdopen(source,'rb',closefd=False) as handle:
                while True:
                    raw=handle.readline(MAX_LINE_BYTES+1)
                    if not raw: break
                    total+=len(raw);records+=1
                    if len(raw)>MAX_LINE_BYTES or total>MAX_FILE_BYTES or not raw.endswith(b'\n'):
                        _error('source_capacity_or_incomplete')
                    digest.update(raw);entry=_record(raw)
                    if type(entry) is not dict: _error('invalid_source_json')
                    directory=entry.get('cwd')
                    if (entry.get('sessionId') not in (None,session_id) or directory is not None and
                            (type(directory) is not str or unicodedata.normalize('NFC',directory)!=unicodedata.normalize('NFC',str(cwd)))):
                        _error('source_identity_mismatch')
                    if entry.get('isSidechain') or entry.get('teamName') or entry.get('forkedFrom'):
                        _error('unsupported_sidechain_or_export')
            named=os.stat(path.name,dir_fd=parent,follow_symlinks=False)
            if (_stamp(before)!=_stamp(os.fstat(source)) or _stamp(before)!=_stamp(named) or total!=before.st_size
                    or any(_dir_scope(directory)!=identity for directory,identity in directories.items())):
                _error('source_changed')
            scope=(tuple(directories[self.root]),tuple(directories[projects]),tuple(directories[path.parent]),
                tuple(directories[cwd]),_identity(before))
            if session_id in self._source_scopes and self._source_scopes[session_id]!=scope: _error('source_scope_changed')
            self._source_scopes[session_id]=scope
            title=value['title']
            if type(title) is not str: _error('invalid_source_title')
            title.encode('utf-8')
            return dict(session_id=session_id,title=_title((title.strip() or 'Claude '+session_id[:8])[:80]),
                source_title_sha256=_hash(title),cwd=str(cwd),cwd_identity=directories[cwd],project_dir=str(resolved),
                root_identity=directories[self.root],projects_identity=directories[projects],project_identity=directories[path.parent],
                source_path=str(path),descriptor=descriptor,file_sha256=digest.hexdigest(),record_count=records)
        except (OSError,UnicodeError,json.JSONDecodeError,RecursionError): _error('source_unavailable')
        finally:
            for fd in (source,parent):
                if fd is not None: os.close(fd)

    def _binding_matches(self, identity, source):
        binding=self.store.binding(identity)
        expected={key:source['descriptor'][key] for key in ('session_id','cwd','path','device','inode','cwd_device','cwd_inode')}
        if (binding is None or binding['session_id']!=source['session_id'] or binding['native_file']!=expected
                or any(row['chat_id']!=identity and row['session_id']==source['session_id']
                    for row in self.store.source_bindings())): _error('existing_binding_conflict')
        return binding

    def _prepare(self, session_id):
        source=self._source(session_id);identity,creation=adoption_identities(self.workspace_id,source['session_id'])
        if self.store.binding(identity) is not None: self._binding_matches(identity,source)
        elif ('claude',source['session_id']) in self.excluded_sources(): _error('source_already_owned')
        return source,dict(source=source,workspace_id=self.workspace_id,chat_id=identity,creation_id=creation,history_mode=HISTORY_MODE)

    def preview(self, session_id):
        source,proof=self._prepare(session_id)
        return dict(agent='claude',source_session_id=source['session_id'],title=source['title'],cwd=source['cwd'],
            workspace_id=self.workspace_id,revision=_hash(proof),history_mode=HISTORY_MODE,execution_started=False,
            body_copied=False,current_executor_ownership='not_checked')

    def adopt(self, session_id, revision):
        if type(revision) is not str or not _SHA.fullmatch(revision): _error('invalid_preview_revision')
        source,proof=self._prepare(session_id)
        if _hash(proof)!=revision: _error('source_preview_changed')
        identity=proof['chat_id']
        try: published=self.catalogue.get(identity)
        except SpecialistHTTPError as error:
            if error.status!=404 or error.code!='native_chat_not_found': raise
            published=None
        if published is not None: self._binding_matches(identity,source)
        def reserve(value):
            if value!=identity: _error('adoption_identity_changed')
            if self._source(session_id)!=source: _error('source_changed')
            self.store.adopt(identity,source['session_id'],source['cwd'],source['descriptor'])
            self._binding_matches(identity,source)
        result=self.catalogue.create(proof['creation_id'],source['title'],reserve,fixed_id=identity)
        self._binding_matches(identity,source)
        return dict(chat=result,source_session_id=source['session_id'],workspace_id=self.workspace_id,
            adopted=True,execution_started=False,body_copied=False)

    def verify_open(self, identity):
        identity=chat_id(identity);self.catalogue.get(identity);binding=self.store.binding(identity)
        if binding is None or not is_adopted_binding(self.workspace_id,identity,binding['session_id']): _error('existing_binding_conflict')
        source,proof=self._prepare(binding['session_id'])
        if proof['chat_id']!=identity: _error('existing_binding_conflict')
        self._binding_matches(identity,source)
