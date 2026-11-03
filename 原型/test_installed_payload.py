"""Install this real source tree into a temporary HOME and exercise its payload.

The child uses isolated Python, installed modules only, no Agent adapters, no
native processes and one bounded loopback HTTP service. This proves packaging
and the disabled entry, not real Agent execution or MCP SDK availability.
"""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import install_local as installer


_PAYLOAD_PROBE = r'''
import base64
import hashlib
from http.client import HTTPConnection
import importlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
from unittest.mock import patch

app, home = (Path(value).resolve() for value in sys.argv[1:3])
assert Path.home().resolve() == home
assert not list(app.rglob('__pycache__'))
sys.path[:0] = [str(app / '原型'), str(app / '工具')]
owned_port = [None]
attempts = {'process': 0, 'external_connection': 0}
original_connect = socket.socket.connect
original_getaddrinfo = socket.getaddrinfo

def deny_process(*args, **kwargs):
    attempts['process'] += 1
    raise AssertionError('Processes are forbidden in the installed-payload probe.')

def loopback_connect(sock, address):
    if not (isinstance(address, tuple) and len(address) >= 2
            and address[0] == '127.0.0.1' and address[1] == owned_port[0]):
        attempts['external_connection'] += 1
        raise AssertionError('Only the probe-owned HTTP port may be contacted.')
    return original_connect(sock, address)

def loopback_addresses(host, port, *args, **kwargs):
    if host != '127.0.0.1' or port != owned_port[0]:
        attempts['external_connection'] += 1
        raise AssertionError('External address discovery is forbidden.')
    return original_getaddrinfo(host, port, *args, **kwargs)

with patch.object(subprocess, 'Popen', side_effect=deny_process), \
        patch.object(socket.socket, 'connect', loopback_connect), \
        patch.object(socket, 'getaddrinfo', loopback_addresses):
    package_exports = {
        'rabbit_content': ('ImageStore', 'FileTaskFiles', 'FileTaskStore', 'ProjectPublishStore',
            'MarkdownChatRegistry', 'MaterialCapacityError', 'FileTaskError', 'FileTaskStoreError',
            'ProjectPublishStoreError', 'MarkdownChatRegistryError'),
        'rabbit_preparation': ('build_context', 'ContextCapacityError', 'prepare_file_context',
            'validate_attachment_ids', 'validate_native_attachments', 'prepare_text_file_messages'),
        'rabbit_memory': ('MemoryProfiles', 'MemoryProfileConflict', 'HindsightMemory',
            'MemoryServiceError', 'NativeMemoryBindings', 'NativeHindsightReadOnly',
            'configured_url', 'read_native_source', 'MemoryWriteJobs', 'MemoryJobConflict',
            'enqueue_completed_in', 'MemoryWriteRunner'),
        'rabbit_runtime': ('CodexRpc', 'NativeSessionStore', 'NativeSessionLease',
            'CodexNativeSession', 'CodexNativeConnection', 'SpecialistService',
            'canonical_metadata', 'validate_model_options'),
        'rabbit_specialist': ('CodexSpecialistConnection', 'prepare_workspace',
            'InterfaceTasks', 'InterfaceTaskCoordinator'),
    }
    loaded = {}
    for package_name, exports in package_exports.items():
        package = importlib.import_module(package_name)
        assert Path(package.__file__).resolve() == app / '原型' / package_name / '__init__.py'
        loaded[package_name] = str(Path(package.__file__).resolve().relative_to(app))
        for name in exports:
            value = getattr(package, name)
            assert callable(value), (package_name, name)
            if package_name == 'rabbit_preparation' and name in (
                    'validate_attachment_ids', 'validate_native_attachments'):
                canonical = importlib.import_module('rabbit_content.attachments')
                assert value.__module__ == 'rabbit_content.attachments', (package_name, name)
                assert value is getattr(canonical, name), (package_name, name)
            else:
                assert value.__module__.startswith(package_name + '.'), (package_name, name)
    retired_flat_modules = {
        'materials', 'file_task_files', 'file_task_store', 'project_publish_files',
        'project_publish_store', 'markdown_chat_document', 'markdown_chat_directory',
        'markdown_chat_registry', 'markdown_chat_sync', 'markdown_sync_files',
        'history_markdown_reference', 'history_records',
        'context', 'text_file_context', 'file_task_context',
        'file_task_intent',
        'native_handoff',
        'memory_profiles', 'hindsight_memory', 'native_memory_bindings',
        'native_hindsight_memory', 'memory_management', 'memory_connection_check',
        'memory_jobs', 'memory_write_runner',
        'codex_rpc',
        'native_rpc_transport',
        'native_json',
        'native_session_store',
        'native_session_lease',
        'codex_native_session',
        'specialist_tool_status',
        'codex_connection',
        'specialist_service',
        'specialist_connection',
        'specialist_workspace',
        'interface_tasks',
        'interface_task_coordinator',
        'pi_connection',
        'pi_rpc',
        'pi_material_protocol',
        'pi_profile_catalog',
        'pi_model_capabilities',
        'pi_native_store',
        'pi_session_file',
        'pi_message_view',
        'dsh_connection',
        'dsh_rpc',
        'dsh_native_store',
        'agent_process',
        'claude_connection',
        'claude_rpc',
        'claude_native_store',
        'claude_material_protocol',
        'claude_message_view',
        'claude_history_source',
        'claude_session_file',
        'claude_native_worker',
        'hermes_connection',
        'hermes_rpc',
        'hermes_native_store',
        'hermes_profile_catalog',
        'hermes_message_view',
        'hermes_native_bootstrap',
        'hermes_session_settings',
        'hermes_image_queue',
    }
    assert not retired_flat_modules.intersection(sys.modules)
    assert not any((app / '原型' / (name + '.py')).exists() for name in retired_flat_modules)
    assert 'rabbit_preparation.attachments' not in sys.modules
    assert not (app / '原型/rabbit_preparation/attachments.py').exists()
    import server
    import rabbit_management_client
    modules = ['server', 'app_management', 'app_settings', 'rabbit_runtime.codex_rpc', 'rabbit_runtime.native_session_store',
               'rabbit_runtime.native_session_lease', 'rabbit_runtime.codex_native_session', 'rabbit_runtime.specialist_service', 'rabbit_runtime.specialist_tool_status',
               'specialist_http', 'rabbit_specialist.specialist_workspace', 'rabbit_specialist.specialist_connection', 'rabbit_runtime.codex_connection',
               'rabbit_management_client', 'native_chats', 'native_catalogue', 'native_http_common', 'native_profile_intents', 'native_profile_catalog', 'rabbit_runtime.pi.rpc', 'rabbit_runtime.pi.connection',
               'pi_native', 'rabbit_runtime.pi.session_store', 'rabbit_runtime.pi.session_file', 'rabbit_runtime.pi.profile_catalog',
               'pi_profile_intents', 'rabbit_runtime.pi.model_capabilities']
    modules += ['rabbit_runtime.hermes.connection', 'rabbit_runtime.hermes.rpc', 'rabbit_runtime.hermes.session_store', 'rabbit_runtime.hermes.profile_catalog', 'hermes_profile_intents', 'hermes_profile_application', 'rabbit_runtime.hermes.session_settings', 'rabbit_runtime.hermes.image_queue', 'rabbit_runtime.hermes.native_bootstrap', 'rabbit_runtime.hermes.result_cache']
    modules += ['rabbit_runtime.claude.connection', 'rabbit_runtime.claude.rpc', 'rabbit_runtime.claude.native_worker', 'claude_native', 'rabbit_runtime.claude.session_store', 'rabbit_runtime.claude.session_file']
    modules += ['rabbit_runtime.native_json', 'rabbit_runtime.claude.material_protocol', 'claude_material_inputs', 'claude_material_intents', 'rabbit_runtime.claude.message_view', 'rabbit_runtime.claude.history_source']
    modules += ['rabbit_runtime.process', 'rabbit_runtime.dsh.connection', 'rabbit_runtime.dsh.rpc', 'dsh_native', 'rabbit_runtime.dsh.session_store', 'dsh_native_interactions']
    modules += ['native_startup', 'admission_gate', 'service_admission', 'service_maintenance', 'native_maintenance', 'native_materials', 'rabbit_runtime.pi.material_protocol', 'pi_material_inputs', 'rabbit_runtime.pi.message_view']
    modules += ['hermes_material_inputs', 'rabbit_runtime.hermes.message_view', 'hermes_material_cache', 'hermes_attachment_transactions', 'hermes_attachment_view']
    modules += ['rabbit_specialist.interface_tasks', 'rabbit_specialist.interface_task_coordinator', 'rabbit_preparation.material_references', 'rabbit_preparation.handoff']
    modules += ['rabbit_memory.write_jobs', 'rabbit_memory.write_runner']
    modules += ['rabbit_content.attachments', 'rabbit_content.file_intent']
    for name in modules:
        module = importlib.import_module(name)
        loaded[name] = str(Path(module.__file__).resolve().relative_to(app))
    assert Path(server.ROOT).resolve() == app / '原型'
    data = home / '.imcodes/one-ai'
    index = data / 'history.sqlite'
    with sqlite3.connect(index) as db:
        server.INDEXER.create_schema(db)
        db.execute("INSERT INTO meta VALUES('built_at','2026-01-01T00:00:00Z')")
        db.execute("INSERT INTO meta VALUES('stats','{}')")
    service = server.Service(data, index, adapters={},
        codex_root=home / '.codex', hermes_db=home / '.hermes/absent.db',
        memory_driver=None, native_source_getter=None, file_adapters={})
    assert service._memory_enabled is False and service._memory_thread is None
    http = None
    thread = None
    try:
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        http.service = service
        http.daemon_threads = True
        owned_port[0] = http.server_address[1]
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        def get(path):
            connection = HTTPConnection('127.0.0.1', owned_port[0], timeout=5)
            try:
                connection.request('GET', path)
                response = connection.getresponse()
                return response.status, response.getheader('Content-Type', ''), response.read()
            finally:
                connection.close()
        def maintenance(action, body):
            connection = HTTPConnection('127.0.0.1', owned_port[0], timeout=5)
            try:
                connection.request('POST', '/api/maintenance/' + action, json.dumps(body),
                    {'Content-Type':'application/json', 'X-OneAI-CSRF':service.csrf_token})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        root_status, root_type, root_body = get('/')
        assert root_status == 200 and 'text/html' in root_type
        assert b'id="specialist-composer-card"' in root_body
        assets = {}
        for name in ('specialist_tools.js', 'specialist.js', 'specialist.css', 'markdown_chats.js', 'conversation_groups.js', 'native_materials.js', 'native_chats.js', 'native_index_text.js', 'pi_native_history.js', 'pi_native.js', 'hermes_native_history.js', 'hermes_attachment_state.js', 'hermes_native.js', 'claude_native.js', 'dsh_native.js'):
            status, content_type, body = get('/' + name)
            assert status == 200 and body == (app / '原型/rabbit_ui/assets' / name).read_bytes()
            assert base64.b64encode(body) in root_body
            assets[name] = {'status': status, 'sha256': hashlib.sha256(body).hexdigest()}
        context_status, context_type, context_body = get('/api/management/context')
        context = json.loads(context_body)
        assert context_status == 200 and 'application/json' in context_type
        assert context == {'workspace_id': service.workspace_id, 'csrf_token': service.csrf_token}
        status_code, _, status_body = get('/api/status')
        assert status_code == 200 and json.loads(status_body)['specialist'] == {'enabled': False}
        assert json.loads(status_body)['native_chats'] == {'enabled': False, 'max_connections': 2}
        assert b'id="native-chats-panel"' in root_body
        assert service.native_chats is None
        assert service.pi_native_chats is None
        assert service.claude_native_chats is None
        assert service.dsh_native_chats is None
        assert json.loads(status_body)['claude_native_chats'] == {'enabled': False, 'max_connections': 1}
        claude_status, _, claude_body = get('/api/claude-chats')
        assert claude_status == 404 and json.loads(claude_body)['code'] == 'claude_native_disabled'
        assert json.loads(status_body)['pi_native_chats'] == {'enabled': False, 'max_connections': 1}
        pi_status, _, pi_body = get('/api/pi-chats')
        assert pi_status == 404 and json.loads(pi_body)['code'] == 'pi_native_disabled'
        native_status, _, native_body = get('/api/native-chats')
        assert native_status == 404 and json.loads(native_body)['code'] == 'native_chats_disabled'
        assert service.specialist is None and service._specialist_store is None
        specialist_status, _, specialist_body = get('/api/specialist')
        assert specialist_status == 404
        assert json.loads(specialist_body)['code'] == 'specialist_disabled'
        client = rabbit_management_client.ManagementClient(port=owned_port[0], workspace_id=service.workspace_id)
        inventory = client.list_profiles()
        assert inventory['ai_profiles'] == []
        assert inventory['workspace_id'] == service.workspace_id
        assert inventory['connection_checked'] is False
        settings_before = client.read_settings()
        assert settings_before['scope'] == 'application_preferences'
        assert settings_before['inference_performed'] is False
        saved_before = service.preferences.read()
        settings_saved = client.update_settings(settings_before['revision'],
            {'theme': 'dark', 'markdown': False})
        assert settings_saved['settings']['theme'] == 'dark'
        assert settings_saved['settings']['markdown'] is False
        assert settings_saved['revision'] != settings_before['revision']
        assert settings_saved['outcome'] == 'acknowledged'
        assert settings_saved['readback_state'] == 'confirmed'
        settings_snapshot = settings_saved['current_settings']
        assert service.preferences.read()['favorites'] == saved_before['favorites']
        try:
            client.update_settings(settings_before['revision'], {'theme':'light'})
        except rabbit_management_client.ManagementClientError as error:
            assert error.code == 'settings_version_conflict'
            assert error.result()['current_settings'] == settings_snapshot
        else:
            raise AssertionError('A stale settings write must fail.')
        assert client.read_settings() == settings_snapshot
        assert {row['key'] for row in settings_saved['catalogue']['fields']
            if row['surface'] == 'ai_only'} == {'markdown', 'show_internal'}
        assert service.store.list()['chats'] == [] and service.store.active_count() == 0
        maintenance_code, _, maintenance_body = get('/api/maintenance')
        maintenance_view = json.loads(maintenance_body)
        assert maintenance_code == 200 and maintenance_view['maintenance']['state'] == 'running'
        identity = {'instance_id':maintenance_view['maintenance']['instance_id'],
            'maintenance_id':'installed-owned-check', 'token':'a'*64, 'generation':1}
        prepared_code, prepared = maintenance('prepare', identity)
        assert prepared_code == 200 and prepared['maintenance']['state'] == 'prepared'
        refused_code, _, refused_body = get('/api/status')
        assert refused_code == 503 and json.loads(refused_body)['code'] == 'maintenance_closed'
        assert 'accepted' not in json.loads(refused_body)
        cancelled_code, cancelled = maintenance('cancel', identity)
        assert cancelled_code == 200 and cancelled['maintenance']['state'] == 'running'
        assert not retired_flat_modules.intersection(sys.modules)
        result = {'status': 'PASS', 'loaded_from_installed_tree': loaded,
            'package_public_exports': package_exports,
            'retired_flat_modules_absent': sorted(retired_flat_modules),
            'root_http': root_status, 'root_contains_specialist_assets': True,
            'assets': assets, 'management_context_http': context_status,
            'installed_settings_client': {'read': True, 'write': True,
                'stale_revision_rejected': True, 'unrelated_preferences_preserved': True,
                'ai_only_fields': ['markdown', 'show_internal']},
            'management_context_exact_fields': True, 'specialist_enabled': False,
            'specialist_disabled_http': specialist_status,
            'native_chats_enabled': False, 'native_chats_disabled_http': native_status,
            'pi_native_chats_enabled': False, 'pi_native_chats_disabled_http': pi_status,
            'claude_native_chats_enabled': False, 'claude_native_chats_disabled_http': claude_status,
            'installed_management_client_profiles': len(inventory['ai_profiles']),
            'native_process_attempts': attempts['process'],
            'external_connection_attempts': attempts['external_connection'],
            'maintenance_prepare_http':prepared_code, 'maintenance_business_refused_http':refused_code,
            'maintenance_cancel_http':cancelled_code,
            'mcp_sdk_execution_checked': False}
    finally:
        if http is not None:
            http.shutdown()
            http.server_close()
        if thread is not None:
            thread.join(timeout=3)
        service.shutdown()
    assert thread is None or not thread.is_alive()
    assert attempts == {'process': 0, 'external_connection': 0}
    assert not list(app.rglob('__pycache__'))
    result['http_thread_joined'] = True
    result['service_ownership_released'] = service._lock_fd is None
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
'''


class InstalledPayloadTests(unittest.TestCase):
    def test_real_source_installs_complete_payload_and_serves_without_agents(self):
        source = Path(__file__).resolve().parent
        originals = installer.source_files(source)
        expected = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for name, path in originals.items()}
        with tempfile.TemporaryDirectory(prefix='rabbit-installed-payload-', dir='/private/tmp') as temporary:
            root = Path(temporary)
            layout = installer.Layout(root / 'isolated-home')
            with patch.object(installer, 'python_executable', return_value=sys.executable), \
                    patch.object(installer.MaintenanceClient, 'probe', return_value=None), \
                    patch.object(installer, 'launchctl', side_effect=AssertionError('No launchd activation.')):
                outcome = installer.install(layout, source, activate_service=False)
            self.assertTrue(outcome['changed'])
            plist = plistlib.loads(layout.plist.read_bytes())
            self.assertNotIn('--specialist-mcp-python', plist['ProgramArguments'])
            self.assertNotIn('--native-chat-mcp-python', plist['ProgramArguments'])
            self.assertNotIn('--native-codex-cli', plist['ProgramArguments'])
            self.assertNotIn('--native-pi-node', plist['ProgramArguments'])
            self.assertNotIn('--native-pi-cli', plist['ProgramArguments'])
            self.assertNotIn('--native-hermes-session-settings', plist['ProgramArguments'])
            manifest = json.loads((layout.app / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest, expected)
            copied = {str(path.relative_to(layout.app)): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in layout.app.rglob('*') if path.is_file() and path.name != 'manifest.json'}
            self.assertEqual(copied, expected)
            environment = {key: value for key, value in os.environ.items()
                           if key not in {'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP'}}
            environment['HOME'] = str(layout.home)
            child = subprocess.run([sys.executable, '-I', '-B', '-c', _PAYLOAD_PROBE,
                                    str(layout.app), str(layout.home)],
                                   cwd=root, env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(child.returncode, 0, child.stderr)
            result = json.loads(child.stdout)
            self.assertEqual(result['status'], 'PASS')
            self.assertEqual(child.stderr, '')
            self.assertTrue(result['service_ownership_released'])
            result['manifest_files'] = len(manifest)
            result['manifest_sha256'] = hashlib.sha256((layout.app / 'manifest.json').read_bytes()).hexdigest()
            result['source_payload_sha256'] = expected
            result['temporary_home_only'] = True
            result['launch_agent_specialist_flag_present'] = False
            result['launch_agent_pi_flags_present'] = False
            # Preserve concrete child evidence in the test runner's stdout;
            # do not emit the random workspace identity or CSRF nonce.
            print('INSTALLED_PAYLOAD_RESULT ' + json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    unittest.main()
