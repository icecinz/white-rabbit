"""Bounded offline DSH index: original transcript and current text context.

Only the caller's derived index is written. The decoder uses installed official
packing/surface functions, with no CLI, profile, persistence owner or Agent.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess

META_KEY='dsh_session_coverage'
MAX_FILE_BYTES=16*1024*1024
MAX_OUTPUT_BYTES=24*1024*1024
MAX_FILES=10000
MAX_ENTRIES=100000
MAX_TOTAL_BYTES=256*1024*1024
MAX_COVERAGE_BYTES=2*1024*1024
MAX_CONTEXT_IDS=20000
MAX_ROWS=40000
SHA=re.compile(r'[0-9a-f]{64}\Z')
COUNTS=('events','replacements','unknown_ignorable_events','omitted_tool_blocks','omitted_thinking_blocks',
    'omitted_image_blocks','unsupported_content_blocks','unknown_context_records','context_omitted_tool_blocks',
    'context_omitted_thinking_blocks','context_omitted_image_blocks','text_messages','generated_context_messages')
TRANSFORM_FIELDS={'native_message_id','native_seq','native_role','context_role','phase','source_kind','replacement'}


class DshHistoryError(ValueError):
    def __init__(self,reason='invalid_source'):
        self.reason=reason if reason in ('invalid_source','unsupported_lineage','unsupported_events','repair_required',
            'pending_input','unsupported_background_state') else 'invalid_source'
        super().__init__('DSH历史来源未能完整核验，原索引保留。')


def _json(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def _sig(value):return value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns
def _opaque(value):return type(value) is str and 0<len(value)<=256 and not any(ord(c)<32 or ord(c)==127 or 0xD800<=ord(c)<=0xDFFF for c in value)
def _integer(value):return type(value) is int and 0<=value<=2**53-1

def _decode(raw):
    def pairs(values):
        result={}
        for key,value in values:
            if key in result:raise DshHistoryError()
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(DshHistoryError()))


class DshHistoryDecoder:
    """Trusted, finite offline Node call. Construction reads no source history."""
    def __init__(self,node,package_root):
        self.node,self.package_root=Path(node),Path(package_root)
        if not self.node.is_absolute() or not self.package_root.is_absolute():raise DshHistoryError()

    def __call__(self,raw,*,compression):
        return self._run(raw,compression=compression)

    def inspect_resume(self,raw,*,compression):
        """Inspect only the exact supplied bytes; never resume or reserve ownership."""
        return self._run(raw,compression=compression,mode='resume_inspect')

    def _run(self,raw,*,compression,mode=None):
        if type(raw) is not bytes or len(raw)>MAX_FILE_BYTES or compression not in ('none','zstd'):raise DshHistoryError()
        worker=Path(__file__).with_name('dsh_history_worker.mjs')
        try:
            # Fixed argv and one payload. Timeout kills and reaps this process;
            # there are no IO threads, CLI flags, arbitrary methods or retries.
            payload={'schema_version':1,'compression':compression,'body':base64.b64encode(raw).decode('ascii')}
            if mode is not None:
                if mode!='resume_inspect':raise DshHistoryError()
                payload['mode']=mode
            result=subprocess.run([str(self.node), '--max-old-space-size=256', str(worker), '--package-root',str(self.package_root)],
                input=_json(payload).encode(),
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=20,check=False,start_new_session=True)
            if result.returncode!=0 or len(result.stdout)>(65536 if mode=='resume_inspect' else MAX_OUTPUT_BYTES):raise DshHistoryError()
            value=_decode(result.stdout.decode('utf-8','strict'))
            return _checked_resume(value) if mode=='resume_inspect' else _checked(value)
        except DshHistoryError:raise
        except (OSError,ValueError,TypeError,UnicodeError,RecursionError,subprocess.SubprocessError):
            raise DshHistoryError() from None


def _checked_resume(value):
    if (type(value) is not dict or type(value.get('schema_version')) is not int or value['schema_version']!=1
            or value.get('mode')!='resume_inspect' or type(value.get('ok')) is not bool):raise DshHistoryError()
    if value['ok'] is False:
        if set(value)!={'schema_version','mode','ok','reason'}:raise DshHistoryError()
        raise DshHistoryError(value['reason'])
    fields={'schema_version','mode','ok','header','project_key','session_key','effective_preset','event_count',
        'closed_log','repair_required','pending_next_turn','pending_next_step','ownership'}
    if (set(value)!=fields or value['closed_log'] is not True or value['repair_required'] is not False
            or type(value['pending_next_turn']) is not int or value['pending_next_turn']!=0
            or type(value['pending_next_step']) is not int or value['pending_next_step']!=0
            or not _integer(value['event_count']) or value['event_count']>200000 or value['ownership']!='not_checked'
            or value['effective_preset'] is not None and (not _opaque(value['effective_preset'])
                or any(c.isspace() for c in value['effective_preset']))):raise DshHistoryError()
    # Reuse the ordinary header/path codec, without constructing a projection.
    coverage={key:0 for key in COUNTS}
    coverage.update(source_version=0,context_mode='text_projection',history_mode='append_origin',continuable=False,
        context_message_ids=[],context_transforms={})
    _checked({'schema_version':1,'header':value['header'],'project_key':value['project_key'],
        'session_key':value['session_key'],'title':'','rows':[],'coverage':coverage})
    head=value['header']
    if ('parentSession' in head or 'origin' in head or head['delegationDepth']!=0 or head.get('seedLength',0)!=0
            or 'cwd' not in head):raise DshHistoryError('unsupported_lineage')
    return {key:item for key,item in value.items() if key not in ('mode','ok')}


def _checked(value):
    try:
        if (type(value) is not dict or set(value)!={'schema_version','header','project_key','session_key','title','rows','coverage'}
                or value['schema_version']!=1 or type(value['schema_version']) is not int
                or type(value['header']) is not dict or value['header'].get('version')!=0
                or not _opaque(value['header'].get('id')) or not _integer(value['header'].get('createdAt'))
                or not _opaque(value['project_key']) or not _opaque(value['session_key'])
                or '/' in value['project_key'] or '/' in value['session_key']
                or value['project_key'] in ('.','..') or value['session_key'] in ('.','..')
                or type(value['title']) is not str or len(value['title'])>240
                or type(value['rows']) is not list or len(value['rows'])>MAX_ROWS):raise DshHistoryError()
        header=value['header']
        if (not {'type','version','id','createdAt','delegationDepth'}<=header.keys()
                or header.keys()-{'type','version','id','createdAt','delegationDepth','cwd','parentSession','seedLength','origin','agentPreset'}
                or header['type']!='session' or type(header['version']) is not int or not _integer(header['delegationDepth'])
                or 'cwd' in header and (type(header['cwd']) is not str or not Path(header['cwd']).is_absolute()
                    or len(header['cwd'])>8192 or any(ord(c)<32 or ord(c)==127 or 0xD800<=ord(c)<=0xDFFF for c in header['cwd']))
                or any(key in header and not _opaque(header[key]) for key in ('parentSession','agentPreset'))
                or 'seedLength' in header and not _integer(header['seedLength'])
                or 'origin' in header and header['origin']!='subagent'):raise DshHistoryError()
        seen=set()
        for row in value['rows']:
            if (type(row) is not dict or set(row)!={'id','native_message_id','seq','role','phase','source_kind','text','time','source_line'}
                    or not _integer(row['seq']) or row['id']!='dsh:event:'+str(row['seq']) or row['id'] in seen
                    or not _opaque(row['native_message_id']) or row['role'] not in ('user','assistant')
                    or row['phase'] not in ('','compact_summary','custom_context') or row['source_kind'] not in ('user','model','plugin','skill-catalog')
                    or row['role']=='assistant' and row['source_kind']!='model'
                    or row['source_kind']=='skill-catalog' and (row['role']!='user' or row['phase']!='custom_context')
                    or type(row['text']) is not str or not row['text'].strip() or not _integer(row['time'])
                    or not _integer(row['source_line']) or row['source_line']<2):raise DshHistoryError()
            seen.add(row['id'])
        _coverage(value['coverage'],rows={r['id']:r for r in value['rows']},saved=False)
        return value
    except (KeyError,ValueError,TypeError,UnicodeError,RecursionError):raise DshHistoryError() from None


def _coverage(item,*,rows=None,saved=True):
    required=set(COUNTS)|{'source_version','context_mode','history_mode','continuable','context_message_ids','context_transforms'}
    if saved:required|={'source_session_id','source_file_id'}
    if (type(item) is not dict or set(item)!=required or type(item['source_version']) is not int or item['source_version']!=0
            or item['context_mode']!='text_projection' or item['history_mode']!='append_origin'
            or type(item['continuable']) is not bool or any(not _integer(item[key]) for key in COUNTS)):raise DshHistoryError()
    if saved and (not _opaque(item['source_session_id']) or type(item['source_file_id']) is not str or not SHA.fullmatch(item['source_file_id'])):raise DshHistoryError()
    ids=item['context_message_ids'];transforms=item['context_transforms']
    if (type(ids) is not list or len(ids)>MAX_CONTEXT_IDS or any(not _opaque(value) for value in ids)
            or len(ids)!=len(set(ids)) or type(transforms) is not dict or set(transforms)!=set(ids)
            or item['continuable'] and (not ids or item['unsupported_content_blocks'] or item['unknown_context_records'])):raise DshHistoryError()
    for identifier in ids:
        transform=transforms[identifier]
        if (type(transform) is not dict or set(transform)!=TRANSFORM_FIELDS or not _integer(transform['native_seq'])
                or identifier!='dsh:event:'+str(transform['native_seq']) or not _opaque(transform['native_message_id'])
                or transform['native_role'] not in ('user','assistant') or transform['context_role']!=transform['native_role']
                or transform['source_kind'] not in ('user','plugin','model','skill-catalog') or type(transform['replacement']) is not bool):raise DshHistoryError()
        if transform['native_role']=='assistant' and transform['source_kind']!='model':raise DshHistoryError()
        phase=('custom_context' if transform['source_kind']=='skill-catalog' else 'compact_summary' if transform['replacement']
            else 'custom_context' if transform['native_role']=='user' and transform['source_kind']!='user' else '')
        if transform['phase']!=phase:raise DshHistoryError()
        if rows is not None:
            row=rows.get(identifier)
            if (row is None or row['role']!=transform['context_role'] or row['phase']!=phase
                    or any(key in row and row[key]!=transform[target] for key,target in (
                        ('native_message_id','native_message_id'),('seq','native_seq'),('source_kind','source_kind')))):
                raise DshHistoryError()
    return item


def _directory(path):
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
        return fd
    except BaseException:os.close(fd);raise


def _check_dir(fd,path,before):
    other=_directory(path)
    try:
        if _sig(os.fstat(fd))!=before or _sig(os.fstat(other))!=before:raise DshHistoryError()
    finally:os.close(other)


def _names(fd,count):
    values=[]
    with os.scandir(fd) as items:
        for item in items:
            count[0]+=1
            if count[0]>MAX_ENTRIES:raise DshHistoryError()
            values.append(item.name)
    return sorted(values)


def _files(root):
    try:rootfd=_directory(root)
    except FileNotFoundError:return
    before=_sig(os.fstat(rootfd));count=[0]
    try:
        for project in _names(rootfd,count):
            info=os.stat(project,dir_fd=rootfd,follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):raise DshHistoryError()
            if not stat.S_ISDIR(info.st_mode):continue
            pfd=os.open(project,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=rootfd);ppath=root/project;pstate=_sig(os.fstat(pfd))
            try:
                for session in _names(pfd,count):
                    info=os.stat(session,dir_fd=pfd,follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):raise DshHistoryError()
                    if not stat.S_ISDIR(info.st_mode):
                        if session.endswith(('.jsonl','.jsonl.zstd')):raise DshHistoryError()
                        continue
                    sfd=os.open(session,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=pfd);spath=ppath/session;sstate=_sig(os.fstat(sfd))
                    try:
                        files=[]
                        for name in _names(sfd,count):
                            state=os.stat(name,dir_fd=sfd,follow_symlinks=False)
                            if stat.S_ISLNK(state.st_mode):raise DshHistoryError()
                            if name in ('session.jsonl','session.jsonl.zstd'):
                                if not stat.S_ISREG(state.st_mode) or state.st_nlink!=1:raise DshHistoryError()
                                files.append(name)
                        if len(files)>1:raise DshHistoryError()
                        if files:yield sfd,files[0],spath/files[0]
                        _check_dir(sfd,spath,sstate)
                    finally:os.close(sfd)
                _check_dir(pfd,ppath,pstate)
            finally:os.close(pfd)
        _check_dir(rootfd,root,before)
    finally:os.close(rootfd)


def ingest_dsh(connection,dsh_root,stats,*,indexer,decoder):
    """Read only this explicit sessions root, and write only the staged index."""
    metadata={};total=files=0;seen={}
    try:
        if not callable(decoder):raise DshHistoryError()
        root=Path(os.path.abspath(os.path.expanduser(os.fspath(dsh_root))))
        for directory,name,path in _files(root):
            files+=1
            if files>MAX_FILES:raise DshHistoryError()
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            try:
                info=os.fstat(fd);before=_sig(info);total+=before[2]
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or before[2]>MAX_FILE_BYTES or total>MAX_TOTAL_BYTES:raise DshHistoryError()
                with os.fdopen(os.dup(fd),'rb') as stream:raw=stream.read(MAX_FILE_BYTES+1)
                if len(raw)>MAX_FILE_BYTES or len(raw)!=before[2]:raise DshHistoryError()
                value=_checked(decoder(raw,compression='zstd' if name.endswith('.zstd') else 'none'))
                if _sig(os.fstat(fd))!=before or _sig(os.stat(name,dir_fd=directory,follow_symlinks=False))!=before:raise DshHistoryError()
            finally:os.close(fd)
            header=value['header'];sid=header['id']
            if path.parent.name!=value['session_key'] or path.parent.parent.name!=value['project_key'] or sid in seen:raise DshHistoryError()
            seen[sid]=str(path)
            indexer.save_session(connection,'dsh',sid,title=value['title'],cwd=header.get('cwd',''),parent=header.get('parentSession'),
                started=indexer.timestamp(header['createdAt']/1000),source_path=path)
            for row in value['rows']:
                indexer.save_message(connection,'dsh',sid,row['id'],row['role'],indexer.timestamp(row['time']/1000),row['text'],path,row['source_line'],row['seq'],row['phase'])
            coverage=dict(value['coverage'],source_session_id=sid,source_file_id=hashlib.sha256(str(path).encode()).hexdigest())
            metadata[sid]=coverage
            if len(_json(metadata).encode())>MAX_COVERAGE_BYTES:raise DshHistoryError()
            stats['dsh_messages_added']=stats.get('dsh_messages_added',0)+len(value['rows'])
        connection.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',(META_KEY,_json(metadata)))
        stats['dsh_files']=files
    except (OSError,ValueError,TypeError,KeyError,UnicodeError,RecursionError,sqlite3.Error):
        stats['source_errors']=stats.get('source_errors',0)+1


def read_coverage(connection,session_id,*,internal=False):
    """Return only a validated current text projection; never query originals."""
    unavailable={'continuable':False,'context_mode':'text_projection','history_mode':'append_origin',
        'notice':'DSH当前上下文来源尚未核验；可以浏览或引用旧原文，不能整段接续。'}
    try:
        if type(internal) is not bool or not _opaque(session_id):return unavailable
        row=connection.execute('SELECT value FROM meta WHERE key=?',(META_KEY,)).fetchone()
        if row is None or type(row[0]) is not str or len(row[0].encode())>MAX_COVERAGE_BYTES:return unavailable
        data=_decode(row[0]);item=_coverage(data.get(session_id))
        if item['source_session_id']!=session_id:return unavailable
        ids=item['context_message_ids']
        rows=connection.execute("SELECT message_id,role,phase FROM app_history_messages WHERE agent='dsh' AND session_id=? "
            "AND message_id IN ("+','.join('?' for _ in ids)+")",[session_id,*ids]).fetchall() if ids else []
        mapped={r['message_id']:{'role':r['role'],'phase':r['phase']} for r in rows}
        _coverage(item,rows=mapped)
        result={key:item[key] for key in COUNTS}
        result.update(continuable=item['continuable'],context_mode='text_projection',history_mode='append_origin',
            context_message_count=len(ids),source_version=0,
            notice='DSH接续只引用原生当前上下文的文字投影；旧压缩原文仍可浏览。工具、思考与图片不回放。' if item['continuable'] else unavailable['notice'])
        if internal:result['context_message_ids']=list(ids) if item['continuable'] else []
        return result
    except (OSError,ValueError,TypeError,KeyError,UnicodeError,RecursionError,sqlite3.Error,AttributeError):return unavailable
