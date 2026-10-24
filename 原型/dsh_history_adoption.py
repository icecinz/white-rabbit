"""Register one verified original DSH log, without starting or creating an Agent.

The index locates a candidate; the trusted offline reader validates its complete
current bytes again. Only source identities and hashes are retained. The native
worker must separately check the frozen source location before cold resume;
an index root is not proof of the running profile's persistence configuration.
"""
from contextlib import closing
import hashlib
import json
import os
import re
import stat
import uuid

from rabbit_runtime.dsh.connection import validate_session_id as _session_id
from dsh_history import META_KEY, MAX_FILE_BYTES, MAX_COVERAGE_BYTES, _decode
from native_catalogue import _title, chat_id
from native_http_common import SpecialistHTTPError
from rabbit_runtime.native_session_store import open_directory as _directory, file_identity as _identity, validate_path as _path

_NAMESPACE = uuid.UUID('05f19ddc-0417-53c1-a87c-586324fa1f48')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
HISTORY_MODE = 'web_profile'
_INSPECTION_KEYS = {'schema_version','header','project_key','session_key','effective_preset','event_count',
    'closed_log','repair_required','pending_next_turn','pending_next_step','ownership'}


class DshHistoryAdoptionError(ValueError):
    """Finite safe reason; never an original body or raw native error."""


def _error(reason): raise DshHistoryAdoptionError(reason)
def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()
def _stamp(info):return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns
def _dir_scope(path):
    descriptor=_directory(path)
    try:return list(_identity(os.fstat(descriptor)))
    finally:os.close(descriptor)


def _only_log(parent,name):
    alternate='session.jsonl' if name.endswith('.zstd') else 'session.jsonl.zstd'
    try:os.stat(alternate,dir_fd=parent,follow_symlinks=False)
    except FileNotFoundError:return
    _error('ambiguous_source_artifact')


def adoption_identities(workspace_id,source_session_id):
    if type(workspace_id) is not str or not _SHA.fullmatch(workspace_id):_error('invalid_service_scope')
    source_session_id=_session_id(source_session_id)
    basis='rabbit/adoption-v1/dsh/'+workspace_id+'/'+source_session_id
    return str(uuid.uuid5(_NAMESPACE,basis+'/chat')),str(uuid.uuid5(_NAMESPACE,basis+'/creation'))


def is_adopted_binding(workspace_id,identity,source_session_id):
    try:return identity==adoption_identities(workspace_id,source_session_id)[0]
    except ValueError:return False


class DshHistoryAdoption:
    def __init__(self,history,source_root,catalogue,store,*,workspace_id,excluded_sources,reader):
        if (getattr(catalogue,'agent',None)!='dsh' or not callable(excluded_sources) or not callable(reader)
                or type(workspace_id) is not str or not _SHA.fullmatch(workspace_id)):
            _error('invalid_service_scope')
        self.history,self.root=history,_path(source_root)
        self.catalogue,self.store=catalogue,store
        self.workspace_id,self.excluded_sources,self.reader=workspace_id,excluded_sources,reader
        self._source_scopes={}

    def _indexed_source(self,session_id):
        # Never project index messages or use their text as an original log.
        with closing(self.history._open()) as db:
            row=db.execute('SELECT session_id,title,cwd,parent_session_id,forked_from_id,kind,archived,source_path '
                'FROM sessions WHERE agent=? AND session_id=?',('dsh',session_id)).fetchone()
            coverage=db.execute('SELECT value FROM meta WHERE key=?',(META_KEY,)).fetchone()
        if row is None:_error('source_not_found')
        value=dict(row)
        if (value['kind']!='root' or value['archived']!=0 or value['parent_session_id'] is not None
                or value['forked_from_id'] is not None):_error('unsupported_source_branch')
        if coverage is None or type(coverage[0]) is not str or len(coverage[0].encode())>MAX_COVERAGE_BYTES:
            _error('source_coverage_unverified_refresh_index')
        try:parsed=_decode(coverage[0])
        except (ValueError,TypeError,RecursionError):_error('source_coverage_unverified_refresh_index')
        item=parsed.get(session_id) if type(parsed) is dict else None
        if (type(item) is not dict or item.get('source_session_id')!=session_id
                or type(item.get('source_version')) is not int or item['source_version']!=0):
            _error('source_coverage_unverified_refresh_index')
        path=_path(value['source_path'])
        if item.get('source_file_id')!=hashlib.sha256(str(path).encode()).hexdigest():
            _error('source_coverage_path_mismatch')
        return value

    @staticmethod
    def _inspection(value,session_id,cwd):
        if (type(value) is not dict or set(value)!=_INSPECTION_KEYS
                or type(value['schema_version']) is not int or value['schema_version']!=1
                or value['closed_log'] is not True or value['repair_required'] is not False
                or type(value['pending_next_turn']) is not int or value['pending_next_turn']!=0
                or type(value['pending_next_step']) is not int or value['pending_next_step']!=0
                or value['ownership']!='not_checked' or type(value['event_count']) is not int
                or not 0<=value['event_count']<=200000):_error('source_resume_unverified')
        header=value['header']
        if (type(header) is not dict or not {'type','version','id','cwd','createdAt','delegationDepth'}<=header.keys()
                or header.keys()-{'type','version','id','cwd','createdAt','delegationDepth','seedLength','agentPreset'}
                or header['type']!='session' or type(header['version']) is not int or header['version']!=0
                or header['id']!=session_id or header['cwd']!=str(cwd)
                or type(header['createdAt']) is not int or not 0<=header['createdAt']<=2**53-1
                or type(header['delegationDepth']) is not int or header['delegationDepth']!=0
                or 'seedLength' in header and (type(header['seedLength']) is not int or header['seedLength']!=0)):
            _error('source_header_mismatch_or_derived')
        preset=value['effective_preset']
        if preset is not None and (type(preset) is not str or not 1<=len(preset)<=256
                or any(c.isspace() or ord(c)<32 or ord(c)==127 or 0xD800<=ord(c)<=0xDFFF for c in preset)):
            _error('source_resume_unverified')
        if any(type(value[key]) is not str or not value[key] for key in ('project_key','session_key')):
            _error('source_resume_unverified')
        return header,preset

    def _source(self,session_id):
        session_id=_session_id(session_id);indexed=self._indexed_source(session_id)
        path,cwd=_path(indexed['source_path']),_path(indexed['cwd'])
        if (path.name not in ('session.jsonl','session.jsonl.zstd') or path.parent.parent.parent!=self.root
                or path.parent.name!=session_id):_error('source_outside_exact_sessions')
        directories={directory:_dir_scope(directory) for directory in dict.fromkeys(
            (self.root,path.parent.parent,path.parent,cwd))}
        parent=descriptor=None
        try:
            parent=_directory(path.parent)
            _only_log(parent,path.name)
            descriptor=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            before=os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>MAX_FILE_BYTES:
                _error('source_unavailable')
            with os.fdopen(descriptor,'rb',closefd=False) as handle:raw=handle.read(MAX_FILE_BYTES+1)
            if len(raw)!=before.st_size or len(raw)>MAX_FILE_BYTES:_error('source_capacity_or_incomplete')
            try:inspection=self.reader(raw,compression='zstd' if path.name.endswith('.zstd') else 'none')
            except (ValueError,TypeError,RecursionError):_error('source_resume_unverified')
            header,preset=self._inspection(inspection,session_id,cwd)
            if path.parent.name!=inspection['session_key'] or path.parent.parent.name!=inspection['project_key']:
                _error('source_namespace_mismatch')
            if (_stamp(before)!=_stamp(os.fstat(descriptor))
                    or _stamp(before)!=_stamp(os.stat(path.name,dir_fd=parent,follow_symlinks=False))
                    or any(_dir_scope(directory)!=identity for directory,identity in directories.items())):
                _error('source_changed')
            _only_log(parent,path.name)
            anchor=dict(session_id=session_id,cwd=str(cwd),cwd_device=directories[cwd][0],cwd_inode=directories[cwd][1],
                source_root=str(self.root),root_device=directories[self.root][0],root_inode=directories[self.root][1],
                project_dir=str(path.parent.parent),project_device=directories[path.parent.parent][0],
                project_inode=directories[path.parent.parent][1],session_dir=str(path.parent),
                session_device=directories[path.parent][0],session_inode=directories[path.parent][1],
                source_path=str(path),file_device=before.st_dev,file_inode=before.st_ino,header_sha256=_hash(header))
            if session_id in self._source_scopes and self._source_scopes[session_id]!=anchor:_error('source_scope_changed')
            self._source_scopes[session_id]=anchor
            title=indexed['title']
            if type(title) is not str:_error('invalid_source_title')
            title.encode('utf-8')
            return dict(session_id=session_id,title=_title((title.strip() or 'DSH '+session_id[:8])[:80]),
                source_title_sha256=_hash(title),cwd=str(cwd),agent_preset=preset,source_anchor=anchor,
                file_sha256=hashlib.sha256(raw).hexdigest(),file_stat=list(_stamp(before)),
                event_count=inspection['event_count'])
        except (OSError,UnicodeError,RecursionError):_error('source_unavailable')
        finally:
            for fd in (descriptor,parent):
                if fd is not None:os.close(fd)

    def _binding_matches(self,identity,source):
        binding=self.store.binding(identity);adoption=self.store.adoption(identity)
        if (binding is None or adoption is None or binding['session_id']!=source['session_id']
                or binding['cwd']!=source['cwd'] or binding['preset_known'] is not True
                or binding['agent_preset']!=source['agent_preset']
                or adoption['chat_id']!=identity or adoption['session_id']!=source['session_id']
                or adoption['agent_preset']!=source['agent_preset'] or adoption['source_anchor']!=source['source_anchor']
                or any(row['chat_id']!=identity and row['session_id']==source['session_id']
                    for row in self.store.source_bindings())):_error('existing_binding_conflict')
        return binding

    def _prepare(self,session_id):
        source=self._source(session_id);identity,creation=adoption_identities(self.workspace_id,source['session_id'])
        if self.store.binding(identity) is not None:self._binding_matches(identity,source)
        elif ('dsh',source['session_id']) in self.excluded_sources():_error('source_already_owned')
        return source,dict(source=source,workspace_id=self.workspace_id,chat_id=identity,creation_id=creation,
            history_mode=HISTORY_MODE)

    def preview(self,session_id):
        source,proof=self._prepare(session_id)
        return dict(agent='dsh',source_session_id=source['session_id'],title=source['title'],cwd=source['cwd'],
            workspace_id=self.workspace_id,revision=_hash(proof),history_mode=HISTORY_MODE,execution_started=False,
            body_copied=False,current_executor_ownership='not_checked')

    def adopt(self,session_id,revision):
        if type(revision) is not str or not _SHA.fullmatch(revision):_error('invalid_preview_revision')
        source,proof=self._prepare(session_id)
        if _hash(proof)!=revision:_error('source_preview_changed')
        identity=proof['chat_id']
        try:published=self.catalogue.get(identity)
        except SpecialistHTTPError as error:
            if error.status!=404 or error.code!='native_chat_not_found':raise
            published=None
        if published is not None:
            self._binding_matches(identity,source);result=published
        else:
            def reserve(value):
                if value!=identity:_error('adoption_identity_changed')
                if self._source(session_id)!=source:_error('source_changed')
                self.store.adopt(identity,source['cwd'],source['session_id'],source['agent_preset'],source['source_anchor'])
                self._binding_matches(identity,source)
            result=self.catalogue.create(proof['creation_id'],source['title'],reserve,fixed_id=identity)
            self._binding_matches(identity,source)
        return dict(chat=result,source_session_id=source['session_id'],workspace_id=self.workspace_id,
            adopted=True,execution_started=False,body_copied=False)

    def verify_open(self,identity):
        identity=chat_id(identity);self.catalogue.get(identity);binding=self.store.binding(identity)
        if binding is None or not is_adopted_binding(self.workspace_id,identity,binding['session_id']):
            _error('existing_binding_conflict')
        source,proof=self._prepare(binding['session_id'])
        if proof['chat_id']!=identity:_error('existing_binding_conflict')
        self._binding_matches(identity,source)
