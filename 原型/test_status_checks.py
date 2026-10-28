"""Manual report storage only: invented facts, no model, tools or HTTP server."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from rabbit_specialist.status_checks import FACT_IDS, StatusCheckError, StatusCheckStore, make_fact


WORKSPACE = 'a' * 64
TARGET = {'agent': 'codex', 'ai_profile_id': '11111111-1111-4111-8111-111111111111',
          'memory_profile_id': 'none'}
OBSERVED = '2026-10-27T01:02:03Z'


def facts(*, unverified=None, revision=1, read_at='2026-10-27T02:00:00Z'):
    return [make_fact(key, label=key, state='unverified' if key == unverified else 'observed',
                      source='invented/' + key, observed_at=OBSERVED, read_at=read_at,
                      identity={'revision': revision if key == 'configuration' else 1},
                      data={'count': 0}) for key in FACT_IDS]


def analysis(check_id, rows):
    return {'check_id': check_id, 'summary': '虚构状态检查。',
            'items': [{'evidence_id': row['id'], 'evidence_sha256': row['evidence_sha256'],
                       'judgment': 'unverified' if row['state'] == 'unverified' else 'consistent',
                       'note': '本次未核实。' if row['state'] == 'unverified' else ''} for row in rows]}


class StatusChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve() / 'chat.sqlite'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE existing_chat (text TEXT)')
            db.execute("INSERT INTO existing_chat VALUES ('原有资料')")
        self.store = StatusCheckStore(self.path, workspace_id=WORKSPACE, owner_generation='generation-1')
        self.check_id = str(uuid.uuid4())

    def ready(self, rows=None):
        rows = facts() if rows is None else rows
        self.store.create(self.check_id, TARGET)
        self.store.set_facts(self.check_id, rows)
        return rows

    def test_reads_are_lazy_and_do_not_create_or_modify_tables(self):
        self.assertIsNone(self.store.get(self.check_id))
        self.assertIsNone(self.store.latest())
        with sqlite3.connect(self.path) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='status_checks'").fetchone())
            self.assertEqual(db.execute('SELECT text FROM existing_chat').fetchone()[0], '原有资料')
        missing = StatusCheckStore(Path(self.tmp.name).resolve() / 'absent.sqlite', workspace_id=WORKSPACE,
                                   owner_generation='generation-1')
        self.assertIsNone(missing.latest())
        self.assertFalse(missing.path.exists())

    def test_two_callers_merge_scope_and_only_one_claim_grants_submission(self):
        other = StatusCheckStore(self.path, workspace_id=WORKSPACE, owner_generation='generation-1')
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda pair: pair[0].create(pair[1], TARGET),
                                    [(self.store, self.check_id), (other, str(uuid.uuid4()))]))
        self.assertEqual(len({row['check_id'] for row in replies}), 1)
        self.assertEqual(sum(row['created'] for row in replies), 1)
        active_id = replies[0]['check_id']
        self.store.set_facts(active_id, facts())
        with ThreadPoolExecutor(max_workers=2) as pool:
            granted = list(pool.map(lambda store: store.claim_analysis(active_id), [self.store, other]))
        self.assertEqual(sum(granted), 1)
        self.assertFalse(self.store.claim_analysis(active_id))
        self.assertEqual(self.store.get(active_id)['stage'], 'analyzing')

    def test_fact_failure_is_partial_without_overwriting_other_facts(self):
        rows = self.ready(facts(unverified='tools'))
        self.assertTrue(self.store.claim_analysis(self.check_id))
        report = self.store.publish(self.check_id, analysis(self.check_id, rows),
                                    source='invented/no-tools-adapter', current_facts=rows)
        self.assertEqual(report['stage'], 'partial')
        self.assertEqual(report['facts_json'], rows)
        self.assertEqual(report['analysis_json']['items'][2]['judgment'], 'unverified')
        self.assertTrue(all(row['applicable'] for row in report['analysis_json']['items']))
        self.assertIsNone(report['error_code'])

    def test_changed_version_invalidates_only_its_own_comment_and_preserves_history(self):
        rows = self.ready()
        self.store.claim_analysis(self.check_id)
        current = facts(revision=2)
        report = self.store.publish(self.check_id, analysis(self.check_id, rows),
                                    source={'adapter': 'claude', 'tools': False}, current_facts=current)
        self.assertEqual(report['stage'], 'partial')
        self.assertEqual([row['applicable'] for row in report['analysis_json']['items']],
                         [False, True, True, True])
        self.assertEqual(report['facts_json'][0]['identity']['revision'], 1)
        # Projection may mark a later changed source stale without writing history.
        changed = deepcopy(current)
        changed[2] = make_fact('tools', label='tools', state='observed', source='invented/tools',
                               observed_at=OBSERVED, identity={'epoch': 'new-generation'}, data={})
        projected = self.store.get(self.check_id, current_facts=changed)
        self.assertFalse(projected['analysis_json']['items'][2]['applicable'])
        self.assertTrue(self.store.get(self.check_id)['analysis_json']['items'][2]['applicable'])

    def test_publish_response_loss_is_recoverable_without_another_analysis_claim(self):
        rows = self.ready()
        self.store.claim_analysis(self.check_id)
        original = self.store.publish(self.check_id, analysis(self.check_id, rows),
                                     source='invented/no-tools-adapter', current_facts=rows)
        reopened = StatusCheckStore(self.path, workspace_id=WORKSPACE, owner_generation='generation-1')
        self.assertEqual(reopened.get(self.check_id), original)
        self.assertFalse(reopened.claim_analysis(self.check_id))
        repeated = reopened.publish(self.check_id, analysis(self.check_id, rows),
                                    source='invented/no-tools-adapter', current_facts=rows)
        self.assertEqual(repeated, original)
        changed = analysis(self.check_id, rows)
        changed['summary'] = '不可覆盖原发布。'
        with self.assertRaisesRegex(StatusCheckError, 'analysis_conflict'):
            reopened.publish(self.check_id, changed, source='invented', current_facts=rows)
        self.assertEqual(reopened.get(self.check_id), original)

    def test_restart_projects_active_unknown_until_explicit_new_check(self):
        rows = self.ready()
        self.store.claim_analysis(self.check_id)
        restarted = StatusCheckStore(self.path, workspace_id=WORKSPACE, owner_generation='generation-2')
        self.assertEqual(restarted.get(self.check_id)['stage'], 'unconfirmed')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT stage FROM status_checks').fetchone()[0], 'analyzing')
        with self.assertRaisesRegex(StatusCheckError, 'owner_changed'):
            restarted.claim_analysis(self.check_id)
        replacement = restarted.create(str(uuid.uuid4()), TARGET)
        self.assertTrue(replacement['created'])
        self.assertEqual(replacement['stage'], 'collecting')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT stage FROM status_checks WHERE check_id=?',
                                        (self.check_id,)).fetchone()[0], 'unconfirmed')
        self.assertEqual(restarted.get(self.check_id)['facts_json'], rows)

    def test_invalid_ai_evidence_cannot_publish_or_change_facts(self):
        rows = self.ready(facts(unverified='memory'))
        self.store.claim_analysis(self.check_id)
        good = analysis(self.check_id, rows)
        variants = []
        for field, value in [('check_id', str(uuid.uuid4())), ('facts', rows), ('summary', '长' * 301)]:
            bad = deepcopy(good)
            bad[field] = value
            variants.append(bad)
        for field, value in [('evidence_id', 'other'), ('evidence_sha256', 'f' * 64),
                             ('judgment', 'model_available'), ('note', '长' * 601), ('source', 'AI')]:
            bad = deepcopy(good)
            bad['items'][0][field] = value
            variants.append(bad)
        bad = deepcopy(good)
        bad['items'][3]['judgment'] = 'consistent'
        variants.append(bad)
        bad = deepcopy(good)
        bad['items'].pop()
        variants.append(bad)
        bad = deepcopy(good)
        bad['items'][3] = deepcopy(bad['items'][0])
        variants.append(bad)
        for value in variants:
            with self.subTest(value=value):
                with self.assertRaises(StatusCheckError):
                    self.store.publish(self.check_id, value, source='invented', current_facts=rows)
                record = self.store.get(self.check_id)
                self.assertEqual(record['stage'], 'analyzing')
                self.assertEqual(record['facts_json'], rows)
                self.assertIsNone(record['analysis_json'])

    def test_fact_hash_excludes_read_time_and_rejects_tampering(self):
        rows = self.ready()
        reread = facts(read_at='2026-10-27T03:00:00Z')
        self.assertEqual([row['evidence_sha256'] for row in rows],
                         [row['evidence_sha256'] for row in reread])
        self.assertEqual(self.store.set_facts(self.check_id, reread)['facts_json'], rows)
        tampered = deepcopy(rows)
        tampered[0]['data']['count'] = 1
        with self.assertRaisesRegex(StatusCheckError, 'evidence_conflict'):
            self.store.set_facts(self.check_id, tampered)
        self.assertEqual(self.store.get(self.check_id)['facts_json'], rows)

    def test_failed_analysis_preserves_facts_and_cannot_be_reclaimed(self):
        rows = self.ready()
        self.store.claim_analysis(self.check_id)
        failed = self.store.fail(self.check_id, 'invalid_adapter_output')
        self.assertEqual(failed['stage'], 'analysis_failed')
        self.assertEqual(failed['error_code'], 'invalid_adapter_output')
        self.assertEqual(failed['facts_json'], rows)
        self.assertFalse(self.store.claim_analysis(self.check_id))
        self.assertEqual(self.store.latest(), failed)


if __name__ == '__main__':
    unittest.main()
