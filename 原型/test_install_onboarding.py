"""Installation preflight with owned fixtures; no real home, service or Agent."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import install_local as installer


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='rabbit-install-check-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.layout = installer.Layout(self.root / 'home')
        self.source = self.root / 'checkout' / '原型'
        self.source.mkdir(parents=True)
        (self.source / 'server.py').write_text('# fictional source\n')
        tools = self.source.parent / '工具'; tools.mkdir()
        (tools / 'fixture.py').write_text('# fictional helper\n')
        self.binary = self.root / 'fictional-executable'
        self.binary.write_text('not an actual program\n'); self.binary.chmod(0o700)
        self.config = self.root / 'connections.json'
        for name, value in (('RUNTIME_FILES', ('server.py',)), ('TOOL_FILES', ('fixture.py',))):
            context = patch.object(installer, name, value); context.start(); self.addCleanup(context.stop)
        for name in ('launchctl', 'activate', 'prepare_directories'):
            context = patch.object(installer, name, side_effect=AssertionError('preflight must not mutate/start'))
            context.start(); self.addCleanup(context.stop)
        context = patch.object(installer.subprocess, 'run', side_effect=AssertionError('must not execute a program'))
        context.start(); self.addCleanup(context.stop)
        context = patch.object(installer, 'ensure_macos'); context.start(); self.addCleanup(context.stop)
        context = patch.object(installer.shutil, 'which', return_value=None)
        self.which = context.start(); self.addCleanup(context.stop)

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def configure(self, connections):
        self.config.write_text(json.dumps({'schema_version': 1, 'connections': connections}))
        return self.config

    def codex(self):
        return {'codex': {'cli': str(self.binary)}}

    def check(self, config=None):
        before = self.snapshot()
        result = installer.check_installation(self.layout, self.source, native_config=config)
        self.assertEqual(self.snapshot(), before)
        return result

    def test_missing_configuration_is_not_mistaken_for_visible_cli_or_success(self):
        self.which.side_effect = lambda name: str(self.binary) if name == 'codex' else None
        value = self.check()
        self.assertEqual(value['status'], 'needs_configuration')
        self.assertTrue(value['source']['valid'])
        self.assertEqual(value['native_configuration']['source'], 'none')
        self.assertEqual(value['native_configuration']['agents'], [])
        self.assertEqual(value['visible_commands']['codex'], str(self.binary))
        self.assertFalse(value['installation']['metadata_present'])
        self.assertEqual(value['url'], 'http://127.0.0.1:17860/')

    def test_explicit_paths_only_mean_ready_for_installation_not_auth_or_real_chat(self):
        value = self.check(self.configure(self.codex()))
        self.assertEqual(value['status'], 'ready_for_installation')
        self.assertEqual(value['native_configuration']['source'], 'explicit')
        self.assertEqual(value['native_configuration']['agents'], ['codex'])
        self.assertEqual(value['authentication'], 'not_checked')
        self.assertEqual(value['real_chat'], 'not_checked')
        self.assertEqual(value['service'], 'not_checked')

    def test_saved_connections_are_validated_without_replacing_them(self):
        self.layout.data.mkdir(parents=True)
        self.layout.state.write_text(json.dumps({'source': str(self.source),
            'native_startup': {'schema_version': 1, 'connections': self.codex()}}))
        value = self.check()
        self.assertEqual(value['status'], 'ready_for_installation')
        self.assertEqual(value['native_configuration']['source'], 'saved')
        self.assertTrue(value['installation']['metadata_present'])
        self.assertFalse(value['installation']['entry_present'])
        self.assertEqual(value['installation']['entry'], str(self.layout.entry))

    def test_specialist_alone_is_not_a_configured_ordinary_agent(self):
        value = self.check(self.configure({'specialist': {'mcp_python': str(self.binary)}}))
        self.assertEqual(value['status'], 'needs_configuration')
        self.assertTrue(value['native_configuration']['specialist_enabled'])

    def test_invalid_source_or_configuration_is_reported_without_echoing_credentials(self):
        configured = self.configure(self.codex())
        (self.source / 'server.py').unlink()
        self.assertEqual(self.check(configured)['status'], 'invalid_source')
        (self.source / 'server.py').write_text('# restored fictional source\n')
        self.configure({'codex': {'cli': str(self.binary), 'api_key': 'PRIVATE_SENTINEL'}})
        value = self.check(self.config)
        self.assertEqual(value['status'], 'invalid_configuration')
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(value))

    def test_unsupported_environment_is_read_only(self):
        configured = self.configure(self.codex())
        with patch.object(installer, 'ensure_macos', side_effect=installer.InstallError('macOS only')), \
             patch.object(installer.sys, 'version_info', (3, 10, 1)):
            value = self.check(configured)
        self.assertEqual(value['status'], 'unsupported_environment')
        self.assertFalse(value['environment']['macos_supported'])
        self.assertFalse(value['environment']['python_supported'])

    def test_cli_check_emits_json_and_nonzero_when_no_agent_is_configured(self):
        def run(arguments):
            output = io.StringIO()
            with patch.object(installer.sys, 'argv', ['install_local.py', 'check', '--source-dir', str(self.source)] + arguments), \
                 patch.object(installer, 'Layout', return_value=self.layout), contextlib.redirect_stdout(output):
                code = installer.main()
            return code, json.loads(output.getvalue())
        code, value = run([])
        self.assertEqual(code, 1); self.assertEqual(value['status'], 'needs_configuration')
        code, value = run(['--native-config', str(self.configure(self.codex()))])
        self.assertEqual(code, 0); self.assertEqual(value['status'], 'ready_for_installation')

    def test_old_python_install_stops_before_deployment(self):
        error = io.StringIO()
        with patch.object(installer.sys, 'argv', ['install_local.py', 'install']), \
             patch.object(installer.sys, 'version_info', (3, 10, 1)), \
             patch.object(installer, 'install', side_effect=AssertionError('must not deploy')), \
             contextlib.redirect_stderr(error):
            self.assertEqual(installer.main(), 1)
        self.assertIn('Python 3.11', error.getvalue())

    def test_success_output_preserves_legacy_mock_result_and_names_next_entry(self):
        output = io.StringIO()
        with patch.object(installer.sys, 'argv', ['install_local.py', 'install']), \
             patch.object(installer, 'Layout', return_value=self.layout), \
             patch.object(installer, 'install', return_value={'entry': str(self.layout.entry)}), \
             contextlib.redirect_stdout(output):
            self.assertEqual(installer.main(), 0)
        self.assertIn('部署完成不等于真实聊天验收', output.getvalue())
        self.assertIn('下次打开：' + str(self.layout.entry), output.getvalue())


if __name__ == '__main__':
    unittest.main()
