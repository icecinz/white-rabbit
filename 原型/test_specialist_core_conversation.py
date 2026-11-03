"""Invented protocol/IO failures only; these are not native model acceptance."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

from native_http_common import SpecialistHTTPError
from rabbit_specialist.core_conversation import SpecialistConversation, MAX_CONTEXT_BYTES


class Store:
    def __init__(self): self.rows = {}; self.pending = False
    def has_unfinished_operations(self, _): return self.pending
    def read_operation(self, client, **_): return deepcopy(self.rows.get(client))
    def lookup(self, client): return deepcopy(self.rows.get(client))
    def active_for_chat(self, _):
        return next((r for r in self.rows.values() if r['status'] not in ('completed','failed','interrupted','rejected')), None)
    def close(self): pass


class Codex:
    session_key = 'specialist/codex'
    def __init__(self):
        self.store = Store(); self.state = 'ready'; self.status = 'idle'; self.requests = []
        self.calls = []; self.open_calls = 0; self.rows = [
            {'turnId':'original-turn','item':{'id':'old-user','type':'userMessage','content':[{'type':'text','text':'虚构配置名是纸舟。'}]}},
            {'turnId':'original-turn','item':{'id':'old-tool','type':'mcpToolCall','arguments':{'command':'DO_NOT_REPLAY'}}},
            {'turnId':'original-turn','item':{'id':'old-answer','type':'agentMessage','text':'已记下虚构配置。'}}]
        self.paginate = False
    def view(self, **kw):
        self.calls.append(deepcopy(kw))
        rows = deepcopy(self.rows)
        if self.paginate: rows = rows[1:] if kw.get('cursor') else rows[:1]
        return {'connection':{'state':self.state,'epoch':'a'*32},
            'binding':{'thread_id':'original-codex-thread','cwd':'/invented'},
            'native':{'thread':{'status':{'type':self.status}}},
            'history_page':{'data':rows,'nextCursor':None if not self.paginate or kw.get('cursor') else 'next'},
            'turns_page':{'data':[],'nextCursor':None},'requests':deepcopy(self.requests),
            'requests_available':self.state=='ready','turn_state':None,'receipts':list(self.store.rows.values())}
    def open(self): self.open_calls += 1; self.state='ready'; return {'state':'ready'}
    def disconnect(self): self.state='not_open'; return {'disconnected':True,'background_tools_exit_verified':False}


class Inner:
    def __init__(self, holder): self.holder=holder; self.sent=[]; self.answer=[]
    def dispatch(self, method, path, *, query='', body=None):
        if path=='/api/specialist' and method=='GET':
            return self.holder.view(**({'cursor':'next'} if query else {}))
        if path.endswith('/open'): return self.holder.open()
        if path.endswith('/disconnect'): return self.holder.disconnect()
        if path.endswith('/receipt'):
            return {'receipt':self.holder.store.read_operation(query.split('=',1)[1])}
        if path.endswith('/send'):
            self.sent.append(deepcopy(body)); turn='new-turn-'+str(len(self.sent))
            receipt={'client_id':body['client_id'],'kind':'turn_start','status':'completed',
                'session_key':'specialist/codex','thread_id':'original-codex-thread','turn_id':turn,'request_sha256':'c'*64}
            self.holder.store.rows[body['client_id']]=receipt
            self.holder.rows += [{'turnId':turn,'item':{'id':turn+'u','type':'userMessage','clientId':body['client_id'],'content':body['inputs']}},
                {'turnId':turn,'item':{'id':turn+'a','type':'agentMessage','text':'Codex续接结果。'}}]
            return {'receipt':receipt}
        raise AssertionError((method,path))


class Dsh:
    identity='00000000-0000-4000-8000-000000000001'
    def __init__(self):
        self.store=Store(); self.open_calls=0; self.sent=[]; self.responses=[]
        self.fail=False; self.events=[]; self.pending=[]; self.state='not_open'
        self.send_status='completed'; self._info={'running':False}; self._epoch='b'*32
    def open(self):
        self.open_calls += 1
        if self.fail: raise RuntimeError('invented connection failure')
        self.state='ready'; return self.view()
    def view(self):
        return {'connection':{'state':self.state,'problem':''},'epoch':self._epoch,
            'binding':{'sessionId':'original-dsh-session','cwd':'/invented','agentPreset':None,'persisted':True},
            'activity':{'state':'idle','client_id':None},'pending':deepcopy(self.pending),
            'history':{'events':deepcopy(self.events),'hasMore':False},'frames':[],
            'operations':list(self.store.rows.values())}
    def _refresh_info(self): pass
    def send(self, client, message, **_):
        self.sent.append((client,message))
        receipt={'client_id':client,'status':self.send_status,'session_id':'original-dsh-session',
            'native_turn':1 if self.send_status=='completed' else None,'request_sha256':'d'*64}
        self.store.rows[client]=receipt
        if self.send_status=='completed': self.events.append({'event':{'seq':len(self.events),'type':'assistant/message','surfaceOp':'append',
            'data':{'message':{'role':'assistant','content':[{'type':'text','text':'DSH已经修改并读回虚构配置。'},
                {'type':'tool-call','name':'DO_NOT_REPLAY'}]}}}})
        return {'operation':receipt}
    def respond(self,*args): self.responses.append(args);return {'submitted':True}
    def disconnect(self): self.state='closed';return {'disconnected':True}
    def close(self): self.state='closed'


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='rabbit-specialist-two-core-fiction-')
        self.path=Path(self.temp.name).resolve()/'conversation.json'
        self.codex=Codex();self.inner=Inner(self.codex);self.dsh=Dsh();self.factories=0
        def factory(): self.factories+=1;return self.dsh,self.dsh.store
        self.factory=factory
        self.conversation=SpecialistConversation(self.inner,self.path,workspace_id='e'*64,dsh_factory=factory)
    def tearDown(self): self.temp.cleanup()
    def switch(self,core,operation=None): return self.conversation.switch(core,operation or str(uuid.uuid4()))
    def send(self,text,client=None):
        client=client or 'specialist/ui/'+str(uuid.uuid4())
        return client,self.conversation.dispatch('POST','/api/specialist/send',body={'client_id':client,'inputs':[{'type':'text','text':text}]})
    def refresh(self,query=''): return self.conversation.dispatch('GET','/api/specialist',query=query)

    def test_constructor_and_passive_view_never_open_either_core(self):
        self.refresh()
        self.assertEqual((self.codex.open_calls,self.dsh.open_calls,self.factories),(0,0,0))
        self.assertEqual(self.conversation.current_core,'codex')

    def test_partial_first_page_cannot_seed_out_of_order_history(self):
        self.codex.paginate=True
        result=self.refresh('cursor=next')
        messages=[r['text'] for r in result['conversation']['entries']]
        self.assertEqual(messages,['虚构配置名是纸舟。','已记下虚构配置。'])
        self.assertIn({},self.codex.calls)
        self.assertIn({'cursor':'next'},self.codex.calls)

    def test_round_trip_missing_prose_only_unique_markers_and_stable_sessions(self):
        first=str(uuid.uuid4());self.switch('dsh',first)
        self.switch('dsh',first)
        client,_=self.send('请修改刚才那份配置并读回。')
        self.refresh();self.switch('codex')
        self.refresh()  # Merely reopening old Codex cannot acknowledge DSH prose.
        _,_=self.send('请接上刚才结果。')
        text=self.inner.sent[-1]['inputs'][0]['text']
        self.assertIn('DSH已经修改并读回虚构配置。',text)
        self.assertIn('请修改刚才那份配置并读回。',text)
        self.assertNotIn('DO_NOT_REPLAY',text)
        self.assertNotIn('虚构配置名是纸舟。',text)
        self.assertIn('虚构配置名是纸舟。',self.dsh.sent[0][1])
        markers=[r for r in self.conversation.state['entries'] if r['kind']=='core_switch']
        self.assertEqual([r['text'] for r in markers],['核心已切换为 DSH','核心已切换为 Codex'])
        self.assertEqual(self.dsh.open_calls,1)
        restored=SpecialistConversation(self.inner,self.path,workspace_id='e'*64,dsh_factory=self.factory)
        self.assertEqual(restored.public(),self.conversation.public())

    def test_switch_second_write_failure_keeps_old_memory_and_disk_authority(self):
        original_replace=__import__('os').replace;calls=0
        def fail_second(src,dst):
            nonlocal calls
            calls+=1
            if calls==2: raise OSError('invented disk full')
            return original_replace(src,dst)
        operation=str(uuid.uuid4())
        with patch('rabbit_specialist.core_conversation.os.replace',side_effect=fail_second):
            with self.assertRaises(OSError): self.switch('dsh',operation)
        disk=json.loads(self.path.read_text())
        self.assertEqual(self.conversation.current_core,'codex')
        self.assertEqual(disk['current_core'],'codex')
        self.assertFalse(any(r['kind']=='core_switch' for r in self.conversation.state['entries']))
        with self.assertRaises(SpecialistHTTPError): self.switch('dsh',operation)
        with self.assertRaises(SpecialistHTTPError): self.switch('dsh')
        self.assertEqual(self.dsh.open_calls,1)

    def test_unknown_then_rejected_does_not_append_or_advance_received_cursor(self):
        self.switch('dsh');cursor=self.conversation.state['cores']['dsh']['received_seq']
        self.dsh.send_status='submitted_unknown'
        client,_=self.send('未知输入不能冒充已收到。')
        native=self.conversation.state['sends'][client]['native_client_id']
        self.assertEqual(self.conversation.state['cores']['dsh']['received_seq'],cursor)
        self.assertFalse(any(r.get('text')=='未知输入不能冒充已收到。' for r in self.conversation.state['entries']))
        with self.assertRaises(SpecialistHTTPError):self.switch('codex')
        self.dsh.store.rows[native]['status']='rejected'
        self.conversation._receipt(client)
        self.assertEqual(self.conversation.state['cores']['dsh']['received_seq'],cursor)
        self.assertFalse(any(r.get('text')=='未知输入不能冒充已收到。' for r in self.conversation.state['entries']))

    def test_capacity_rejects_complete_history_before_target_open_without_truncation(self):
        self.codex.rows[0]['item']['content'][0]['text']='字'*MAX_CONTEXT_BYTES
        with self.assertRaises(SpecialistHTTPError) as error:self.switch('dsh')
        self.assertEqual(error.exception.code,'specialist_core_context_capacity')
        self.assertEqual(self.factories,0)
        self.assertEqual(self.conversation.current_core,'codex')

    def test_target_failure_keeps_original_core_history_and_no_marker(self):
        self.dsh.fail=True
        with self.assertRaises(SpecialistHTTPError): self.switch('dsh')
        self.assertEqual(self.conversation.current_core,'codex')
        self.assertFalse(any(r['kind']=='core_switch' for r in self.conversation.state['entries']))
        self.assertEqual(self.codex.rows[0]['item']['id'],'old-user')

    def test_native_approval_execution_or_unknown_blocks_switch_before_new_owner(self):
        for state in ('approval','active','unknown'):
            self.codex.requests=[{'token':'real-human-only'}] if state=='approval' else []
            self.codex.status='active' if state=='active' else 'idle'
            self.codex.store.pending=state=='unknown'
            with self.assertRaises(SpecialistHTTPError): self.switch('dsh')
        self.assertEqual(self.factories,0)

    def test_receipt_maps_original_native_identity_without_faking_turn_uuid(self):
        self.switch('dsh');client,result=self.send('一次虚构发送。')
        receipt=result['receipt'];self.assertEqual(receipt['client_id'],client)
        self.assertEqual(receipt['thread_id'],'original-dsh-session')
        self.assertEqual(receipt['turn_id'],'1');self.assertEqual(receipt['native_turn'],1)
        self.assertNotEqual(receipt['native_client_id'],receipt['turn_id'])
        again=self.conversation.dispatch('GET','/api/specialist/receipt',query='client_id='+client)
        self.assertEqual(again['receipt'],receipt);self.assertEqual(len(self.dsh.sent),1)

    def test_dsh_native_answer_is_forwarded_once_with_unchanged_envelope(self):
        self.switch('dsh')
        response={'type':'client-response','rpcId':'native-request','result':{'ok':False,'error':{'code':'cancelled'}}}
        self.conversation.dispatch('POST','/api/specialist/answer',body={'token':'pending-human-token','result':{'epoch':'b'*32,'response':response}})
        self.assertEqual(self.dsh.responses,[('pending-human-token','b'*32,response)])

    def test_actual_browser_target_codec_is_folded_but_every_user_text_part_survives(self):
        asset=Path(__file__).parent/'rabbit_ui/assets/specialist_context.js'
        script="const fs=require('fs'),vm=require('vm');global.window={};vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));process.stdout.write(window.RabbitSpecialistContext.block({schema_version:2,workspace_id:'e'.repeat(64),target:{kind:'page',page:'conversations',section:null},observed:null}));"
        block=subprocess.run(['node','-e',script,str(asset)],check=True,capture_output=True,text=True).stdout
        client='specialist/ui/'+str(uuid.uuid4())
        parts=[{'type':'text','text':'用户第一段。'},{'type':'text','text':'用户第二段。'},{'type':'text','text':block}]
        self.conversation.dispatch('POST','/api/specialist/send',body={'client_id':client,'inputs':parts})
        self.assertEqual(self.inner.sent[-1]['inputs'],parts)
        user=next(r for r in self.conversation.state['entries'] if r.get('source_id')=='send:'+client)
        self.assertEqual(user['text'],'用户第一段。\n\n用户第二段。')
        # A user-typed lookalike or a different workspace is complete user text.
        lookalike='[小白兔当前工作对象 v2]\n这是用户写的例子。'
        client2,_=self.send('请保留我的示例。')
        client3='specialist/ui/'+str(uuid.uuid4())
        self.conversation.dispatch('POST','/api/specialist/send',body={'client_id':client3,
            'inputs':[{'type':'text','text':'正文。'},{'type':'text','text':lookalike}]})
        user3=next(r for r in self.conversation.state['entries'] if r.get('source_id')=='send:'+client3)
        self.assertIn(lookalike,user3['text'])

    def test_dsh_only_conversation_has_no_codex_holder_or_fictional_codex_binding(self):
        path=Path(self.temp.name)/'dsh-only.json'
        conversation=SpecialistConversation(None,path,workspace_id='f'*64,dsh_factory=self.factory)
        value=conversation.dispatch('GET','/api/specialist')
        self.assertIsNone(conversation.holder)
        self.assertIsNone(value['binding'])
        self.assertEqual(value['conversation']['available_cores'],['dsh'])
        self.assertEqual(value['conversation']['current_core'],'dsh')
        self.assertIsNone(value['conversation']['cores']['codex']['session_id'])
        self.assertEqual(self.dsh.open_calls,0)
        with self.assertRaises(SpecialistHTTPError):conversation.switch('codex',str(uuid.uuid4()))

    def test_disconnect_both_cores_retains_exact_installer_receipt(self):
        self.switch('dsh')
        value=self.conversation.dispatch('POST','/api/specialist/disconnect',body={})
        self.assertEqual(value,{'disconnected':True,'background_tools_exit_verified':False})
        self.assertEqual(self.codex.state,'not_open')
        self.assertEqual(self.dsh.state,'closed')

    def test_settlement_during_view_confirms_user_before_native_assistant(self):
        self.switch('dsh');self.dsh.send_status='submitted_unknown'
        client,_=self.send('本轮用户正文必须在回答前。')
        native=self.conversation.state['sends'][client]['native_client_id']
        original_view=self.dsh.view
        def settle_view():
            self.dsh.store.rows[native].update(status='completed',native_turn=1)
            self.dsh.events=[{'event':{'seq':1,'type':'assistant/message','surfaceOp':'append',
                'data':{'message':{'content':[{'type':'text','text':'本轮助手正文。'}]}}}}]
            return original_view()
        with patch.object(self.dsh,'view',side_effect=settle_view):result=self.refresh()
        rows=[r for r in result['conversation']['entries'] if r.get('core')=='dsh' and r['kind']=='message']
        self.assertEqual([(r['role'],r['text']) for r in rows],
            [('user','本轮用户正文必须在回答前。'),('assistant','本轮助手正文。')])

    def test_dsh_only_live_connection_releases_without_opening_cold_old_codex(self):
        self.switch('dsh');self.codex.state='not_open'
        result=self.conversation.dispatch('POST','/api/specialist/disconnect',body={})
        self.assertEqual(result,{'disconnected':True,'background_tools_exit_verified':False})
        self.assertEqual(self.codex.open_calls,0)

    def test_explicit_dsh_reconcile_reads_only_the_original_client_without_resending(self):
        self.switch('dsh');self.dsh.send_status='submitted_unknown'
        client,_=self.send('只核对原请求。')
        body={'action':'reconcile','client_id':client}
        pending=self.conversation.dispatch('POST','/api/specialist/control',body=body)
        self.assertEqual(pending['receipt']['status'],'submitted_unknown')
        native=self.conversation.state['sends'][client]['native_client_id']
        self.dsh.store.rows[native].update(status='completed',native_turn=1)
        completed=self.conversation.dispatch('POST','/api/specialist/control',body=body)
        self.assertEqual(completed['receipt']['status'],'completed')
        self.assertEqual(completed['receipt']['client_id'],client)
        different=self.conversation.dispatch('POST','/api/specialist/control',body={
            'action':'reconcile','client_id':'specialist/ui/'+str(uuid.uuid4())})
        self.assertIsNone(different['receipt'])
        self.assertEqual(len(self.dsh.sent),1)

    def test_codex_reader_settlement_between_idle_check_and_full_view_keeps_order(self):
        original_dispatch=self.inner.dispatch
        def unknown_send(method,path,**options):
            result=original_dispatch(method,path,**options)
            if path.endswith('/send'):
                client=options['body']['client_id'];self.codex.store.rows[client]['status']='submitted_unknown'
                result['receipt']=deepcopy(self.codex.store.rows[client])
            return result
        with patch.object(self.inner,'dispatch',side_effect=unknown_send):client,_=self.send('新用户必须在新助手前。')
        # Even though native history already has a reply, an unknown ledger
        # cannot promote the application received cursor or import that reply.
        first=self.refresh()
        self.assertFalse(any(r.get('text')=='Codex续接结果。' for r in first['conversation']['entries']))
        def settle_during_idle(_):
            self.codex.store.rows[client]['status']='completed';return False
        with patch.object(self.codex.store,'has_unfinished_operations',side_effect=settle_during_idle):value=self.refresh()
        rows=value['conversation']['entries'];texts=[r.get('text') for r in rows]
        self.assertLess(texts.index('新用户必须在新助手前。'),texts.index('Codex续接结果。'))
        self.assertEqual(len(self.inner.sent),1)

    def test_post_native_capacity_failure_is_unknown_never_proven_unsent(self):
        import rabbit_specialist.core_conversation as module
        original_save=self.conversation._save;limit=None
        def capacity_after_preparation(candidate=None):
            nonlocal limit
            proposed=candidate if candidate is not None else self.conversation.state
            if limit is None and proposed['sends']:
                limit=len((module._json(proposed)+'\n').encode())
            with patch.object(module,'MAX_STATE_BYTES',limit or module.MAX_STATE_BYTES):return original_save(candidate)
        client='specialist/ui/'+str(uuid.uuid4())
        with patch.object(self.conversation,'_save',side_effect=capacity_after_preparation):
            with self.assertRaises(SpecialistHTTPError) as failure:self.send('原生已收到后，本机保存容量不足不能说未发。',client)
        self.assertIsNone(failure.exception.accepted)
        self.assertTrue(failure.exception.result_unknown)
        self.assertEqual(len(self.inner.sent),1)
        self.assertEqual(self.codex.store.rows[client]['status'],'completed')
        self.assertEqual(self.conversation.state['sends'][client]['status'],'prepared_unknown')
        result=self.conversation._receipt(client)
        self.assertEqual(result['receipt']['status'],'completed')
        self.assertEqual(len(self.inner.sent),1)

    def test_old_codex_core_disappearing_preserves_readable_history_and_controlled_refusal(self):
        self.refresh()
        restored=SpecialistConversation(None,self.path,workspace_id='e'*64,dsh_factory=self.factory)
        view=restored.dispatch('GET','/api/specialist')
        self.assertEqual(view['current_core'],'codex')
        self.assertIn('core_unavailable',view)
        self.assertEqual([r['text'] for r in view['conversation']['entries']],['虚构配置名是纸舟。','已记下虚构配置。'])
        with self.assertRaises(SpecialistHTTPError) as failure:restored.switch('dsh',str(uuid.uuid4()))
        self.assertEqual(failure.exception.code,'specialist_core_source_unavailable')
        self.assertEqual(self.dsh.open_calls,0)

    def test_actual_browser_directory_metadata_reaches_core_without_entering_prose(self):
        asset=Path(__file__).parent/'rabbit_ui/assets/specialist_context.js'
        script="const fs=require('fs'),vm=require('vm');global.window={};vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));process.stdout.write(window.RabbitSpecialistContext.block({schema_version:2,workspace_id:'e'.repeat(64),target:JSON.parse(process.argv[2]),observed:null}));"
        for target in ({'kind':'agent_instance','agent_instance_id':'local/dsh'},
                       {'kind':'model','model_id':'local/dsh/deepseek-v4-flash'}):
            with self.subTest(target=target):
                block=subprocess.run(['node','-e',script,str(asset),json.dumps(target)],check=True,capture_output=True,text=True).stdout
                client='specialist/ui/'+str(uuid.uuid4())
                parts=[{'type':'text','text':'请只读这个已选对象。'},{'type':'text','text':block}]
                self.conversation.dispatch('POST','/api/specialist/send',body={'client_id':client,'inputs':parts})
                self.assertEqual(self.inner.sent[-1]['inputs'],parts)
                row=next(r for r in self.conversation.state['entries'] if r.get('source_id')=='send:'+client)
                self.assertEqual(row['text'],'请只读这个已选对象。')

    def test_cold_dsh_unknown_creation_blocks_switch_and_maintenance_without_opening(self):
        from rabbit_runtime.dsh.session_store import DSHNativeStore
        from rabbit_runtime.maintenance import COUNT_NAMES
        root=self.path.parent;work=root/'old-dsh-work';work.mkdir()
        with DSHNativeStore(root/'specialist-dsh-native.sqlite') as store:
            identity=str(uuid.uuid4());store.bind(identity,work);store.freeze_preset(identity,None)
            store.begin_create(identity,str(uuid.uuid4()),'c'*32)
        with self.assertRaises(SpecialistHTTPError):self.switch('dsh')
        self.assertEqual(self.factories,0)
        self.codex.maintenance_state=lambda:{'complete':True,'counts':dict.fromkeys(COUNT_NAMES,0)}
        value=self.conversation.maintenance_state()
        self.assertEqual(value['counts']['pending_creations'],1)
        self.assertEqual(self.factories,0)


class DshOnlyServiceTests(unittest.TestCase):
    def test_no_codex_binary_creates_only_lazy_dsh_specialist_and_no_codex_ledger(self):
        from server import Service, INDEXER
        import sqlite3
        from rabbit_specialist.specialist_connection import CodexSpecialistConnection
        from rabbit_runtime.dsh.connection import DshNativeConnection
        with tempfile.TemporaryDirectory(prefix='rabbit-dsh-only-service-') as folder:
            root=Path(folder).resolve();package=root/'package';(package/'lib').mkdir(parents=True)
            cli=package/'lib/bin.js';cli.write_text('// never executed')
            node=root/'node';node.write_text('#!/bin/sh\nexit 99\n');node.chmod(0o700)
            tool=root/'mcp.py';tool.write_text('# never executed')
            with sqlite3.connect(root/'index.sqlite') as db:INDEXER.create_schema(db)
            management=CodexSpecialistConnection(port=17899,mcp_python=sys.executable,tool_script=tool)
            connection=DshNativeConnection(node,cli,package)
            with patch('shutil.which',return_value=None),patch.object(DshNativeConnection,'create_rpc',side_effect=AssertionError('passive startup cannot open DSH')):
                service=Service(root/'data',root/'index.sqlite',adapters={},file_adapters={},
                    memory_driver=None,native_source_getter=None,specialist_connection=management,dsh_native_connection=connection)
                try:
                    self.assertIsNone(service.specialist)
                    self.assertIsNone(service._specialist_store)
                    self.assertFalse((root/'data/specialist-native.sqlite').exists())
                    result=service.specialist_request('GET','/api/specialist')
                    self.assertEqual(result['current_core'],'dsh')
                    self.assertFalse(any(row['agent']=='codex' and row['role']=='specialist' for row in service.work_status()['works']))
                finally:service.shutdown()


if __name__=='__main__':unittest.main()
