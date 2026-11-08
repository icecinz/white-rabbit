"""Test exact MiniMax credentials, display meanings and private persistence."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import dashboard_minimax_usage as minimax
import dashboard_usage as usage


ACCOUNT_KEY = 'sk-api-fictional-account-key'
SUBSCRIPTION_KEY = 'sk-cp-fictional-subscription-key'


class MiniMaxUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='rabbit-test-minimax-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = self.root / 'dashboard-minimax.json'
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'MINIMAX_API_KEY': '', 'MINIMAX_REGION': ''}).start()

    def test_manual_only_usage_source_reads_each_key_on_its_own_official_endpoint(self):
        minimax.save_configuration(self.root, {
            'region': 'global', 'account_key': ACCOUNT_KEY, 'subscription_key': SUBSCRIPTION_KEY,
        })
        service = usage.UsageSources.from_connections(self.root, None, None, None)
        with patch.object(minimax, '_get_json', side_effect=[
            {'available_amount': '12.34', 'cash_balance': '8.90', 'credit_balance': '3.44'},
            {'model_remains': [{'model_name': 'general', 'current_interval_total_count': 0,
                                'current_interval_usage_count': 0,
                                'current_interval_remaining_percent': 42.75,
                                'current_weekly_remaining_percent': 68}]},
        ]) as read:
            before = next(card for card in service.snapshot() if card['id'] == usage.MINIMAX)
            read.assert_not_called()
            after = service.refresh(usage.MINIMAX)
            self.assertEqual([call.args for call in read.call_args_list], [
                ('https://api.minimax.io/account/query_balance', ACCOUNT_KEY),
                ('https://api.minimax.io/v1/token_plan/remains', SUBSCRIPTION_KEY),
            ])
        self.assertEqual(after['status'], 'ready')
        self.assertEqual(before['configuration'], {'region': 'global', 'account_configured': True,
                                                   'subscription_configured': True})
        self.assertEqual(before['entry']['url'], 'https://platform.minimax.io/console/home')
        metrics = {row['label']: (row['value'], row['unit']) for row in after['metrics']}
        self.assertEqual(metrics['可用金额'], ('12.34', 'USD'))
        self.assertEqual(metrics['本期剩余'], (42.75, '%'))
        self.assertEqual(metrics['本期已用'], (57.25, '%'))
        self.assertNotIn(ACCOUNT_KEY, json.dumps(after))
        self.assertNotIn(SUBSCRIPTION_KEY, json.dumps(after))
        self.assertNotIn(ACCOUNT_KEY, (self.root / 'dashboard-usage.json').read_text())

    def test_save_preserves_other_key_only_in_same_region(self):
        minimax.save_configuration(self.root, {
            'region': 'cn', 'account_key': ACCOUNT_KEY, 'subscription_key': '',
        })
        combined = minimax.save_configuration(self.root, {
            'region': 'cn', 'account_key': '', 'subscription_key': SUBSCRIPTION_KEY,
        })
        self.assertTrue(combined['account_configured'])
        self.assertTrue(combined['subscription_configured'])
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)
        changed = minimax.save_configuration(self.root, {
            'region': 'global', 'account_key': '', 'subscription_key': SUBSCRIPTION_KEY,
        })
        self.assertFalse(changed['account_configured'])
        self.assertNotIn(ACCOUNT_KEY, self.settings.read_text())
        with self.assertRaisesRegex(ValueError, 'sk-api-'):
            minimax.save_configuration(self.root, {
                'region': 'cn', 'account_key': SUBSCRIPTION_KEY, 'subscription_key': '',
            })
        self.assertEqual(minimax.context(self.root).presentation['configuration'], changed)

    def test_refuses_symlink_and_exposed_configuration(self):
        foreign = self.root / 'unrelated.txt'
        foreign.write_text('untouched', encoding='utf-8')
        self.settings.symlink_to(foreign)
        body = {'region': 'cn', 'account_key': ACCOUNT_KEY, 'subscription_key': ''}
        with self.assertRaisesRegex(ValueError, '保存失败'):
            minimax.save_configuration(self.root, body)
        self.assertEqual(minimax.context(self.root).status, 'needs_configuration')
        self.assertEqual(foreign.read_text(encoding='utf-8'), 'untouched')
        self.settings.unlink()
        self.settings.write_text(json.dumps(body), encoding='utf-8')
        self.settings.chmod(0o644)
        self.assertIsNone(minimax.context(self.root).read)

    def test_unconfigured_and_demo_sources_never_read_credentials(self):
        context = minimax.context(self.root)
        self.assertEqual(context.status, 'needs_configuration')
        self.assertIsNone(context.read)
        with patch.object(minimax, 'context', side_effect=AssertionError('demo credential read')):
            demo = usage.UsageSources.from_connections(self.root, None, None, None, allow_minimax=False)
            self.assertNotIn(usage.MINIMAX, [card['id'] for card in demo.snapshot()])


if __name__ == '__main__':
    unittest.main()
