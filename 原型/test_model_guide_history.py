"""Guide observations only; no model call or real user data."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from chat_store import ChatStore
from native_profile_catalog import project_native_capabilities
from rabbit_specialist.model_guide_history import ModelGuideHistory
from rabbit_runtime.pi.profile_catalog import project_pi_capabilities, project_pi_catalogue
from rabbit_runtime.hermes.profile_catalog import project_hermes_capabilities, project_hermes_catalogue
from unittest.mock import patch, Mock


WORKSPACE = 'a' * 64
CHAT = '00000000-0000-4000-8000-000000000001'


def codex(time='2026-10-28T10:00:00Z', chat=None):
    return project_native_capabilities({'observed_at': time, 'source_chat_id': chat,
        'catalogue': {'data': [{'model': 'sample', 'displayName': 'Sample',
            'supportedReasoningEfforts': [{'reasoningEffort': 'high'}],
            'defaultReasoningEffort': 'high', 'secret': 'SHOULD_NOT_BE_STORED'}]}}, enabled=True)


def observation(agent, source):
    return {'agent': agent, 'observed_at': source['observed_at'], 'source_chat_id': source['source_chat_id']}


class ModelGuideHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = ChatStore(Path(self.tmp.name) / 'chat.sqlite')
        self.history = ModelGuideHistory(self.store, WORKSPACE)

    def save(self, sources, *agents):
        return self.history.save({'workspace_id': WORKSPACE,
            'observations': [observation(agent, sources[agent]) for agent in agents]}, sources)

    def test_manual_save_survives_new_store_but_is_a_separate_display_snapshot(self):
        sources = {'codex': codex()}
        self.assertEqual(self.history.read()['catalogues'], {})
        self.assertEqual(self.save(sources, 'codex')['saved'], ['codex'])
        again = ModelGuideHistory(ChatStore(self.store.path), WORKSPACE).read()['catalogues']
        self.assertEqual(again['codex'], sources['codex'])
        self.assertNotIn('SHOULD_NOT_BE_STORED', json.dumps(again))
        self.assertEqual(ModelGuideHistory(self.store, 'b' * 64).read()['catalogues'], {})

    def test_reordered_observations_cannot_replace_newer_saved_result(self):
        old = {'codex': codex('2026-10-28T09:00:00Z')}
        new = {'codex': codex('2026-10-28T10:00:00Z')}
        self.assertEqual(self.save(new, 'codex')['saved'], ['codex'])
        self.assertEqual(self.save(old, 'codex')['unchanged'], ['codex'])
        self.assertEqual(self.history.read()['catalogues']['codex']['observed_at'], new['codex']['observed_at'])

    def test_changed_source_and_unavailable_do_not_overwrite(self):
        source = codex(); self.save({'codex': source}, 'codex')
        changed = codex('2026-10-28T11:00:00Z', CHAT)
        result = self.history.save({'workspace_id': WORKSPACE, 'observations': [observation('codex', source)]}, {'codex': changed})
        self.assertEqual(result['unchanged'], ['codex'])
        changed['catalogue_available'] = False
        self.assertEqual(self.save({'codex': changed}, 'codex')['unchanged'], ['codex'])
        self.assertEqual(self.history.read()['catalogues']['codex'], source)

    def test_pi_hermes_projection_remains_compatible_and_omits_private_fields(self):
        snapshot = lambda catalogue: {'observed_at': '2026-10-28T10:00:00Z', 'source_chat_id': CHAT, 'catalogue': catalogue}
        raw_pi = {'models': [{'provider': 'invented', 'id': 'sample', 'name': 'Sample',
                              'headers': {'authorization': 'PRIVATE_TOKEN'}}]}
        pi = project_pi_catalogue(raw_pi, levels=[{'provider': 'invented', 'model': 'sample', 'thinking_levels': ['off', 'high']}])
        hermes = project_hermes_catalogue({'providers': [{'slug': 'invented', 'name': 'Invented',
            'models': ['sample'], 'api_key': 'PRIVATE_TOKEN'}]})
        sources = {'pi': project_pi_capabilities(snapshot(pi), enabled=True),
                   'hermes': project_hermes_capabilities(snapshot(hermes), enabled=True)}
        self.assertEqual(set(self.save(sources, 'pi', 'hermes')['saved']), {'pi', 'hermes'})
        self.assertEqual(self.history.read()['catalogues'], sources)
        self.assertNotIn('PRIVATE', json.dumps(self.history.read()))

    def test_invalid_input_and_corrupt_history_are_never_promoted(self):
        source = codex(); self.save({'codex': source}, 'codex')
        for body in ({'workspace_id': 'wrong', 'observations': []},
                     {'workspace_id': WORKSPACE, 'observations': [observation('codex', source)] * 2},
                     {'workspace_id': WORKSPACE, 'observations': [{'agent': 'codex', 'observed_at': 'bad', 'source_chat_id': None}]}):
            with self.assertRaises(ValueError): self.history.save(body, {'codex': source})
        bad = copy.deepcopy(source); bad['models'][0]['name'] = '/Users/private/file'
        self.assertEqual(self.save({'codex': bad}, 'codex')['unchanged'], ['codex'])
        with self.store._open() as db:
            db.execute('UPDATE model_guide_history SET snapshot_json=?', ('{invalid',))
        self.assertEqual(self.history.read()['catalogues'], {})

    def test_dsh_partial_guide_and_custom_strength_survive_without_native_profile(self):
        source = {'source':'dsh/session.models', 'observed_at':'2026-10-28T10:00:00Z',
                  'source_chat_id':CHAT, 'partial':True, 'models':[{'provider':'local',
                  'provider_name':'Local', 'model':'fictional', 'name':'Fictional',
                  'efforts':['brief', 'thorough']}]}
        self.assertEqual(self.save({'dsh':source}, 'dsh')['saved'], ['dsh'])
        self.assertEqual(ModelGuideHistory(ChatStore(self.store.path), WORKSPACE).read()['catalogues']['dsh'], source)
        invalid = copy.deepcopy(source); invalid['models'][0]['provider_name'] = '/Users/private/file'
        invalid['observed_at'] = '2026-10-28T11:00:00Z'
        self.assertEqual(self.save({'dsh':invalid}, 'dsh')['unchanged'], ['dsh'])
        self.assertEqual(self.history.read()['catalogues']['dsh'], source)

    def test_service_exposes_historical_records_without_seeding_native_validation(self):
        from server import Service
        base = Path(self.tmp.name) / 'service'
        service = Service(base / 'data', base / 'history.sqlite', adapters={}, memory_driver=None,
                          native_source_getter=None, file_adapters={})
        try:
            source = codex()
            request = {'workspace_id': service.workspace_id,
                       'observations': [observation('codex', source)]}
            with patch.object(service, '_native_profile_capabilities', return_value={'codex': source}):
                self.assertEqual(service.save_model_guide_history(request)['saved'], ['codex'])
            self.assertEqual(service.model_guide_history()['catalogues']['codex'], source)
            self.assertIsNone(service.native_chats)
            self.assertFalse(service._native_profile_capabilities()['codex']['catalogue_available'])
        finally:
            service.shutdown()

    def test_dsh_service_guide_and_saved_history_do_not_enter_profile_capabilities(self):
        from server import Service
        base = Path(self.tmp.name) / 'dsh-service'
        service = Service(base / 'data', base / 'history.sqlite', adapters={}, memory_driver=None,
                          native_source_getter=None, file_adapters={})
        try:
            snapshot = {'workspace_id':service.workspace_id, 'source':'dsh/session.models',
                        'observed_at':'2026-10-28T10:00:00Z', 'source_chat_id':CHAT,
                        'partial':False, 'models':[{'provider':'local','provider_name':'Local',
                        'model':'sample','name':'Sample','efforts':['thorough']}]}
            manager = Mock(); manager.catalogue_snapshot.return_value = snapshot
            with patch.object(service, 'dsh_native_chats', manager):
                guide = service.dsh_model_guide()
                self.assertEqual(guide['workspace_id'], service.workspace_id)
                self.assertNotIn('workspace_id', guide['catalogue'])
                request = {'workspace_id':service.workspace_id,'observations':[observation('dsh',snapshot)]}
                self.assertEqual(service.save_model_guide_history(request)['saved'], ['dsh'])
                self.assertEqual(service.model_guide_history()['catalogues']['dsh'], guide['catalogue'])
                self.assertNotIn('dsh', service._native_profile_capabilities())
                manager.catalogue_snapshot.return_value = dict(snapshot, workspace_id='other')
                with self.assertRaises(ValueError): service.dsh_model_guide()
        finally:
            service.shutdown()


if __name__ == '__main__':
    unittest.main()
