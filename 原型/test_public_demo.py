"""Public entry's temporary fake data, context hand-off and cleanup; no HTTP."""
from contextlib import ExitStack, contextmanager
import importlib.util
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

DEMO_PATH = Path(__file__).resolve().parent.parent / 'demo.py'
SPEC = importlib.util.spec_from_file_location('rabbit_public_demo', DEMO_PATH)
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


class PublicDemoTests(unittest.TestCase):
    @contextmanager
    def isolated(self):
        with ExitStack() as stack:
            stack.enter_context(patch('subprocess.Popen', side_effect=AssertionError('No real Agent.')))
            stack.enter_context(patch('socket.socket', side_effect=AssertionError('No HTTP or network.')))
            stack.enter_context(patch('history_sources.HistorySources.refresh', side_effect=AssertionError('No native refresh.')))
            stack.enter_context(patch('history_sources.HistorySources.bootstrap_from', side_effect=AssertionError('No legacy bootstrap.')))
            stack.enter_context(patch.object(demo.INDEXER, 'refresh_index', side_effect=AssertionError('No native ingestion.')))
            stack.enter_context(patch('server.read_native_source', side_effect=AssertionError('No native memory.')))
            yield

    def wait(self, service, run):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            value = service.store.run(run['id'])
            if value['status'] not in ('queued', 'running'):
                self.assertEqual(value['status'], 'completed', value.get('error'))
                return value
            time.sleep(.01)
        self.fail('Simulated reply did not finish.')

    def test_construct_refresh_and_inventory_are_isolated(self):
        with self.isolated(), patch('platform.node', return_value='PRIVATE-HOSTNAME'), demo.demo_session() as service:
            self.assertEqual(set(service.adapters), {'codex', 'hermes'})
            self.assertIsNone(service.memory_runner)
            self.assertIsNone(service.native_chats); self.assertIsNone(service.pi_native_chats)
            self.assertIsNone(service.hermes_native_chats); self.assertIsNone(service.specialist)
            status = service.status()
            self.assertTrue(status['demo_mode']); self.assertEqual(status['profiles']['memory'], 'none')
            self.assertEqual(service.file_adapters, {}); self.assertIsNone(service.publications)
            before = service.index_path.read_bytes()
            self.assertTrue(service.refresh()['demo_mode'])
            self.assertEqual(service.index_path.read_bytes(), before)
            inventory = service.management_inventory(fresh=True)
            self.assertEqual(inventory['device']['name'], '演示设备')
            self.assertNotIn('PRIVATE-HOSTNAME', json.dumps(inventory))
            self.assertTrue(all('模拟' in p['name'] for p in service.profile_inventory()['ai_profiles']))
            preview = service.local_document({'path': '演示说明.md'})
            self.assertIn('所有聊天和回答都是虚构的', preview['content'])
            self.assertTrue(str(service.native_history_materials.codex_root).startswith(str(service.data_dir.parent)))
            self.assertTrue(str(service.native_history_materials.hermes_db).startswith(str(service.data_dir.parent)))

    def test_same_chat_switches_simulated_agents_with_actual_context_and_saved_replies(self):
        with self.isolated(), demo.demo_session() as service:
            chat = service.store.list()['chats'][0]['id']
            profiles = {p['agent_instance_id'].split('/')[-1]: p for p in service.profile_inventory()['ai_profiles']}
            for backend, text in (('codex', '另一个虚构代号是纸船8426。请复述全部代号。'),
                                  ('hermes', '换个助手，只凭前文复述这两个代号。')):
                profile = profiles[backend]
                old_selection = service.profile_selection(chat)['profile_selection']
                selection = service.set_profile_selection(chat, {'revision': old_selection['revision'],
                    'ai_profile_id': profile['id'], 'memory_profile_id': 'none'})['profile_selection']
                body = {'text': text, 'client_id': 'demo-test-'+backend,
                        'profile_selection_revision': selection['revision'],
                        'ai_profile_id': profile['id'], 'ai_profile_revision': profile['revision'],
                        'memory_profile_id': 'none'}
                self.wait(service, service.submit(chat, body)['run'])
            messages = service.store.get(chat)['messages']
            self.assertEqual(len(messages), 6)
            self.assertEqual([m['backend'] for m in messages[-4:] if m['role'] == 'assistant'], ['codex','hermes'])
            for message in (messages[3], messages[5]):
                self.assertTrue(message['content'].startswith('【模拟回答】'))
                self.assertIn('纸船7318', message['content']); self.assertIn('纸船8426', message['content'])
                self.assertIn('没有调用真实模型', message['content'])
            # A brand-new chat has no paper-boat fact. No hardcoded mock memory.
            empty = service.create({'title': '空白模拟对话'})['chat']['id']
            self.wait(service, service.submit(empty, {'backend':'hermes','client_id':'empty','text':'前文代号是什么？'})['run'])
            self.assertIn('没有纸船代号', service.store.get(empty)['messages'][-1]['content'])
            self.assertNotIn('7318', service.store.get(empty)['messages'][-1]['content'])

    def test_context_cleanup_on_normal_exit_and_keyboard_interrupt(self):
        with self.isolated():
            retained = []
            with demo.demo_session() as service:
                root = service.data_dir.parent; retained.append(service)
                self.assertTrue(root.exists())
            self.assertFalse(root.exists()); self.assertIsNone(retained[-1]._lock_fd)
            with self.assertRaises(KeyboardInterrupt):
                with demo.demo_session() as service:
                    root = service.data_dir.parent; retained.append(service)
                    raise KeyboardInterrupt
            self.assertFalse(root.exists()); self.assertIsNone(retained[-1]._lock_fd)
            self.assertTrue(all(not worker.is_alive() for s in retained for worker, cancel in s.workers.values()))


if __name__ == '__main__':
    unittest.main()
