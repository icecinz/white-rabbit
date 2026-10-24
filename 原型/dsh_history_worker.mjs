/*
Third-party attribution
Adapted frame scanning and path spelling from:
@deepseek-ai/dsh-session-persistence-jsonl 0.1.0-rc.6, lib/index.js
Upstream: https://github.com/deepseek-ai/deepseek-harness
Repository package: packages/session/session-persistence-jsonl
The original @deepseek-ai/dsh-session 0.1.0-rc.6 pure functions are
loaded from the separately installed package; its package is not bundled.

MIT License

Copyright (c) 2026 DeepSeek

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/
// Offline projection only. No CLI, profile, persistence service or Agent is loaded.
import { readFileSync } from 'node:fs'
import { resolve, isAbsolute } from 'node:path'
import { pathToFileURL } from 'node:url'
import { TextDecoder } from 'node:util'
import { zstdDecompressSync } from 'node:zlib'

const MAX_FILE=16*1024*1024, MAX_PLAIN=64*1024*1024, MAX_OUTPUT=24*1024*1024
const MAX_EVENTS=200000, MAX_ROWS=40000, MAX_LINE=1024*1024
const object=v=>v!==null && typeof v==='object' && !Array.isArray(v)
const exact=(v,required,optional=[])=>object(v) && required.every(k=>Object.hasOwn(v,k)) && Object.keys(v).every(k=>[...required,...optional].includes(k))
const opaque=v=>typeof v==='string' && v.length>0 && v.length<=256 && !/[\x00-\x1f\x7f\uD800-\uDFFF]/u.test(v)
const integer=v=>Number.isSafeInteger(v) && v>=0 && !Object.is(v,-0)
function fail(){throw new Error('invalid_dsh_history')}
// Bounded adaptation of scanZstdFrames from DSH rc.6 JSONL persistence (MIT).
// Node's one-shot decoder consumes one frame, not the whole concatenated log.
function frames(buffer){
  const result=[];let offset=0
  while(offset<buffer.length){
    const start=offset
    if(buffer.length-offset<5||buffer.readUInt32LE(offset)!==0xFD2FB528)fail();offset+=4
    const descriptor=buffer[offset++];if((descriptor&24)!==0)fail()
    const flag=descriptor>>>6,single=(descriptor&32)!==0,checksum=(descriptor&4)!==0,dictionary=descriptor&3
    const rest=(single?0:1)+(dictionary===3?4:dictionary)+(flag===0?(single?1:0):1<<flag)
    if(buffer.length-offset<rest)fail()
    if(!single){const wd=buffer[offset],base=2**(10+(wd>>>3));if(base+base/8*(wd&7)>MAX_PLAIN)fail()}
    offset+=rest
    for(;;){
      if(buffer.length-offset<3)fail();const block=buffer.readUIntLE(offset,3);offset+=3
      const kind=(block>>>1)&3;if(kind===3)fail();const bytes=kind===1?1:block>>>3
      if(buffer.length-offset<bytes)fail();offset+=bytes;if((block&1)!==0)break
    }
    if(checksum){if(buffer.length-offset<4)fail();offset+=4}
    result.push([start,offset]);if(result.length>MAX_EVENTS+1)fail()
  }
  if(!result.length)fail();return result
}
function decompress(raw){
  const chunks=[];let total=0
  for(const [start,end] of frames(raw)){
    const decoded=zstdDecompressSync(raw.subarray(start,end),{maxOutputLength:MAX_PLAIN-total})
    total+=decoded.length;if(total>MAX_PLAIN)fail()
    if(chunks.length===0&&(decoded.length>65536||decoded.at(-1)!==10||decoded.indexOf(10)!==decoded.length-1))fail()
    chunks.push(decoded)
  }
  return Buffer.concat(chunks,total)
}
// Same version-0 path spelling as the installed JSONL backend (MIT).
function segment(raw){if(!opaque(raw))fail();return [...raw].map(c=>/^[A-Za-z0-9._-]$/u.test(c)?c:[...c].map(x=>{let out='';for(let i=0;i<x.length;i++)out+='~'+x.charCodeAt(i).toString(16).toUpperCase().padStart(4,'0');return out}).join('')).join('').replace(/^\.{1,2}$/u,m=>[...m].map(()=> '~002E').join(''))}
function project(cwd){if(cwd===undefined)return '_no-cwd';let out='',separator=false;for(let i=0;i<cwd.length;i++){const c=cwd[i],code=cwd.charCodeAt(i);if('/\\:'.includes(c)){if(!separator)out+='-';separator=true}else{out+=c!=='~'&&/^[A-Za-z0-9._-]$/u.test(c)?c:'~'+code.toString(16).toUpperCase().padStart(4,'0');separator=false}}return '--'+(out.replace(/^-+/u,'')||'root').slice(0,251)+'--'}
function header(v){
  if(!exact(v,['type','version','id','createdAt','delegationDepth'],['cwd','parentSession','seedLength','origin','agentPreset']) || v.type!=='session' || v.version!==0 || !opaque(v.id) || !integer(v.createdAt) || !integer(v.delegationDepth))fail()
  if(Object.hasOwn(v,'cwd') && (typeof v.cwd!=='string'||!isAbsolute(v.cwd)||v.cwd.length>8192||/[\x00-\x1f\x7f\uD800-\uDFFF]/u.test(v.cwd)))fail()
  for(const k of ['parentSession','agentPreset'])if(Object.hasOwn(v,k)&&!opaque(v[k]))fail()
  if(Object.hasOwn(v,'seedLength')&&!integer(v.seedLength)||Object.hasOwn(v,'origin')&&v.origin!=='subagent')fail()
  return v
}
const refused=reason=>({schema_version:1,mode:'resume_inspect',ok:false,reason})
// A narrow structural fence, not the complete optional native invariant plugin.
// This checks pairing only; it never constructs or commits recovery events.
function paired(events){
  let turn=null,step=null,nextTurn=1,nextStep=1
  for(const event of events){
    const data=event.data
    switch(event.type){
      case 'turn/start':if(turn!==null||data.turn!==nextTurn)return false;turn=data.turn;nextStep=1;break
      case 'turn/end':if(turn!==data.turn||step!==null||!object(data.reason)||!['completed','aborted','interrupted','blocked','error','max-tokens'].includes(data.reason.kind))return false;turn=null;nextTurn++;break
      case 'step/start':if(turn!==data.turn||step!==null||data.step!==nextStep)return false;step=data.step;break
      case 'step/end':if(turn!==data.turn||step!==data.step)return false;step=null;nextStep++;break
      case 'assistant/chunk':case 'assistant/message':case 'tool/call':case 'tool/result':
        if(turn!==data.turn||step!==data.step)return false;break
      case 'request/header':case 'request/context':case 'todo/write':if(turn===null)return false;break
      default:break
    }
  }
  return true
}
async function resumeInspection(root,meta,events,unknown,interruptedTurnClosers){
  if(Object.hasOwn(meta,'parentSession')||Object.hasOwn(meta,'origin')||meta.delegationDepth!==0||(meta.seedLength??0)!==0||!Object.hasOwn(meta,'cwd'))return refused('unsupported_lineage')
  if(unknown)return refused('unsupported_events')
  if(events.some(event=>event.type==='agent-preset/selected'&&(!opaque(event.data.agentPreset)||/\s/u.test(event.data.agentPreset))))return refused('invalid_source')
  if(!paired(events))return refused('invalid_source')
  if(interruptedTurnClosers(events).length!==0)return refused('repair_required')
  if(events.some(event=>['schedule/change','goal/change','subagent/descriptor'].includes(event.type)||event.type.startsWith('tool-workflow/')))return refused('unsupported_background_state')
  const base=resolve(root,'node_modules/@deepseek-ai')
  for(const name of ['dsh-agent','dsh-agent-presets']){
    const pkg=JSON.parse(readFileSync(resolve(base,name,'package.json'),'utf8'))
    if(pkg.name!=='@deepseek-ai/'+name||pkg.version!=='0.1.0-rc.6')fail()
  }
  const [{Inbox},{resolveSessionPreset}]=await Promise.all(['dsh-agent','dsh-agent-presets'].map(name=>import(pathToFileURL(resolve(base,name,'lib/index.js')).href)))
  if(typeof Inbox!=='function'||typeof resolveSessionPreset!=='function')fail()
  const view=Object.freeze({header:meta,events,append:()=>fail()})
  const inbox=new Inbox(view,{inserted:()=>fail(),discarded:()=>fail(),claimed:()=>fail()})
  if(inbox.nextTurn.length||inbox.nextStep.length)return refused('pending_input')
  const preset=resolveSessionPreset(view)??null
  if(preset!==null&&(!opaque(preset)||/\s/u.test(preset)))fail()
  return {schema_version:1,mode:'resume_inspect',ok:true,header:meta,project_key:project(meta.cwd),session_key:segment(meta.id),
    effective_preset:preset,event_count:events.length,closed_log:true,repair_required:false,pending_next_turn:0,pending_next_step:0,ownership:'not_checked'}
}
async function main(){
  if(process.argv.length!==4||process.argv[2]!=='--package-root'||!isAbsolute(process.argv[3]))fail()
  const root=resolve(process.argv[3]),pkg=JSON.parse(readFileSync(resolve(root,'package.json'),'utf8'))
  const sessionRoot=resolve(root,'node_modules/@deepseek-ai/dsh-session')
  const sessionPkg=JSON.parse(readFileSync(resolve(sessionRoot,'package.json'),'utf8'))
  if(pkg.name!=='@deepseek-ai/dsh'||pkg.version!=='0.1.0-rc.6'||sessionPkg.name!=='@deepseek-ai/dsh-session'||sessionPkg.version!==pkg.version)fail()
  const {decodeStorageRecord,adoptSessionEvent,foldSurface,isAppendSurfaceEvent,deriveEventMessage,KNOWN_SESSION_EVENT_TYPES,interruptedTurnClosers}=await import(pathToFileURL(resolve(sessionRoot,'lib/index.js')).href)
  if([decodeStorageRecord,adoptSessionEvent,foldSurface,isAppendSurfaceEvent,deriveEventMessage].some(f=>typeof f!=='function')||!(KNOWN_SESSION_EVENT_TYPES instanceof Set))fail()
  let size=0;const chunks=[]
  for await(const chunk of process.stdin){size+=chunk.length;if(size>Math.ceil(MAX_FILE*4/3)+1024)fail();chunks.push(chunk)}
  const input=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(Buffer.concat(chunks)))
  if(!exact(input,['schema_version','compression','body'],['mode'])||input.schema_version!==1||Object.hasOwn(input,'mode')&&input.mode!=='resume_inspect'||!['none','zstd'].includes(input.compression)||typeof input.body!=='string'||/[^A-Za-z0-9+/=]/u.test(input.body))fail()
  const raw=Buffer.from(input.body,'base64');if(raw.length>MAX_FILE||raw.toString('base64')!==input.body)fail()
  // Strict complete-file mode: a torn zstd frame or partial JSONL tail rejects.
  const plain=input.compression==='zstd'?decompress(raw):raw
  if(plain.length>MAX_PLAIN||plain.at(-1)!==10)fail()
  const lines=new TextDecoder('utf-8',{fatal:true}).decode(plain).split('\n');lines.pop()
  if(lines.length<1||Buffer.byteLength(lines[0])>65536)fail()
  const meta=header(JSON.parse(lines[0]));const events=[],physical=[];let unknown=0,title=''
  for(let i=1;i<lines.length;i++){
    if(!lines[i]||Buffer.byteLength(lines[i])>MAX_LINE)fail()
    const decoded=decodeStorageRecord(JSON.parse(lines[i]));if(!Array.isArray(decoded)||events.length+decoded.length>MAX_EVENTS)fail()
    for(const event of decoded){
      if(!exact(event,['type','seq','time','data'],['ignorable','sourceEventSeqs','surfaceOp'])||!opaque(event.type)||event.seq!==events.length||!integer(event.time)||!object(event.data)||Object.hasOwn(event,'ignorable')&&event.ignorable!==true)fail()
      if(!KNOWN_SESSION_EVENT_TYPES.has(event.type)){
        if(input.mode==='resume_inspect'){process.stdout.write(JSON.stringify(refused('unsupported_events')));return}
        if(event.ignorable!==true||Object.hasOwn(event,'surfaceOp')||Object.hasOwn(event,'sourceEventSeqs'))fail();unknown++
      }
      adoptSessionEvent(event);events.push(event);physical.push(i+1)
      if(event.type==='session/title'&&typeof event.data.title==='string')title=event.data.title.slice(0,240)
    }
  }
  const folded=foldSurface(events),current=new Set(folded.nodes),rows=[],transforms={}
  if(input.mode==='resume_inspect'){
    if(typeof interruptedTurnClosers!=='function')fail()
    const output=JSON.stringify(await resumeInspection(root,meta,events,unknown,interruptedTurnClosers))
    if(Buffer.byteLength(output)>65536)fail();process.stdout.write(output);return
  }
  const counts={events:events.length,replacements:folded.replacements.length,unknown_ignorable_events:unknown,
    omitted_tool_blocks:0,omitted_thinking_blocks:0,omitted_image_blocks:0,unsupported_content_blocks:0,unknown_context_records:0,
    context_omitted_tool_blocks:0,context_omitted_thinking_blocks:0,context_omitted_image_blocks:0,text_messages:0,generated_context_messages:0}
  for(const event of events){
    if(event.type!=='user/message'&&event.type!=='assistant/message')continue
    const message=deriveEventMessage(event);if(message===null)continue
    if(!object(message)||!opaque(message.id)||!['user','assistant'].includes(message.role)||message.role!==(event.type==='user/message'?'user':'assistant')||!Array.isArray(message.content)||!object(message.source)||!opaque(message.source.kind))fail()
    const active=current.has(event.seq),append=isAppendSurfaceEvent(event),replacement=!append
    let text='';for(const block of message.content){
      if(!object(block)||typeof block.type!=='string')fail()
      if(block.type==='text'){if(typeof block.text!=='string')fail();text+=(text?'\n':'')+block.text}
      else{const key={'reasoning':'thinking','image':'image','tool-call':'tool','tool-result':'tool'}[block.type]
        if(key){counts['omitted_'+key+'_blocks']++;if(active)counts['context_omitted_'+key+'_blocks']++}else counts.unsupported_content_blocks++}
    }
    if(!append&&!active)continue
    const skillCatalog=message.source.kind==='skill-catalog'
    // Official dsh-tool-skill SkillCatalogSource is catalog-form runtime data.
    // Its entries remain metadata; only the original text block is indexed.
    if(skillCatalog && (message.role!=='user'||!exact(message.source,['kind','form','entries'],['update'])
      ||message.source.form!=='catalog'||Object.hasOwn(message.source,'update')&&message.source.update!==true
      ||!Array.isArray(message.source.entries)||message.source.entries.length>10000
      ||message.source.entries.some(entry=>!exact(entry,['name','description'])||typeof entry.name!=='string'||!entry.name||typeof entry.description!=='string'))){counts.unknown_context_records++;continue}
    if(!['user','plugin','model','skill-catalog'].includes(message.source.kind)){counts.unknown_context_records++;continue}
    const phase=skillCatalog?'custom_context':replacement?'compact_summary':message.role==='user'&&message.source.kind!=='user'?'custom_context':''
    if(!text.trim())continue
    if(rows.length>=MAX_ROWS)fail()
    const id='dsh:event:'+event.seq
    const row={id,native_message_id:message.id,seq:event.seq,role:message.role,phase,source_kind:message.source.kind,text,time:event.time,source_line:physical[event.seq]}
    rows.push(row);counts.text_messages++;if(phase)counts.generated_context_messages++
    if(active)transforms[id]={native_message_id:message.id,native_seq:event.seq,native_role:message.role,context_role:message.role,phase,source_kind:message.source.kind,replacement}
  }
  // Tool results are deliberately outside this text projection.
  for(const event of events)if(event.type==='tool/result'){counts.omitted_tool_blocks++;if(current.has(event.seq))counts.context_omitted_tool_blocks++}
  const bySeq=new Map(rows.map(row=>[row.seq,row.id])),ordered=folded.nodes.map(seq=>bySeq.get(seq)).filter(id=>id!==undefined)
  const coverage={...counts,source_version:0,context_mode:'text_projection',history_mode:'append_origin',continuable:ordered.length>0&&counts.unsupported_content_blocks===0&&counts.unknown_context_records===0,context_message_ids:ordered,context_transforms:transforms}
  const output=JSON.stringify({schema_version:1,header:meta,project_key:project(meta.cwd),session_key:segment(meta.id),title,rows,coverage})
  if(Buffer.byteLength(output)>MAX_OUTPUT)fail();process.stdout.write(output)
}
main().catch(()=>{process.stderr.write('dsh_history_decode_failed\n');process.exitCode=1})
