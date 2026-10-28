"""Status analysis contract with a fake adapter; no CLI, model or listener."""
from copy import deepcopy
import json
from pathlib import Path
import threading
import unittest

from rabbit_specialist import status_analysis as analysis


CHECK_ID = 'manual-check-123'


def facts():
    return [dict(id=name, label=name, state='observed', source='program/' + name,
        observed_at=None, read_at='2026-10-27T12:00:00Z', identity={'version': 1},
        data={'available': True}, evidence_sha256=str(position) * 64)
        for position, name in enumerate(('configuration', 'catalogue', 'tools', 'memory'), 1)]


def report():
    return {'check_id': CHECK_ID, 'summary': '已核对四项程序资料，实际调用尚未验证。',
        'items': [dict(evidence_id=fact['id'], evidence_sha256=fact['evidence_sha256'],
            judgment='consistent', note='只确认所提供的程序事实。') for fact in facts()]}


class FakeAdapter:
    def __init__(self, text=None):
        self.route = {'backend': 'claude', 'model': 'synthetic-model',
                      'route_fingerprint': 'PRIVATE_FINGERPRINT', 'private': 'PRIVATE_ROUTE'}
        self.text = json.dumps(report(), ensure_ascii=False) if text is None else text
        self.capture_calls, self.calls = 0, []
        self.output_model, self.output_source = 'reported-synthetic', 'service_reported'

    def capture_route(self):
        self.capture_calls += 1
        return self.route

    def run(self, prompt, cwd, callback, cancel_event, route):
        self.calls.append((prompt, cwd, route))
        callback({'type': 'status', 'text': '连接中'})
        callback({'type': 'text', 'text': self.text})
        return dict(text=self.text, model=self.output_model, model_source=self.output_source,
                    private='PRIVATE_RESULT')


class StatusAnalysisTests(unittest.TestCase):
    def invoke(self, adapter=None, supplied=None, cancel=None):
        adapter = adapter or FakeAdapter()
        route, _ = analysis.prepare(adapter)
        return analysis.analyze(adapter, route, facts() if supplied is None else supplied,
                                CHECK_ID, Path('/synthetic'), cancel or threading.Event())

    def test_prepare_captures_once_and_returns_only_public_model_source(self):
        adapter = FakeAdapter()
        route, source = analysis.prepare(adapter)
        self.assertEqual(adapter.capture_calls, 1)
        self.assertEqual(source, dict(agent='claude', model='synthetic-model',
            model_source='requested_route', mode='tool_free_text'))
        route['private'] = 'changed copy'
        self.assertEqual(adapter.route['private'], 'PRIVATE_ROUTE')

    def test_exact_four_facts_one_adapter_call_no_private_route_in_prompt(self):
        adapter, supplied = FakeAdapter(), facts()
        before = deepcopy(supplied)
        value, source = self.invoke(adapter, supplied)
        self.assertEqual(value, report())
        self.assertEqual(len(adapter.calls), 1)
        prompt, cwd, route = adapter.calls[0]
        self.assertEqual(cwd, Path('/synthetic'))
        self.assertEqual(route, adapter.route)
        self.assertNotIn('PRIVATE_', prompt)
        self.assertNotIn('PRIVATE_', json.dumps(source))
        self.assertEqual(source['model'], 'reported-synthetic')
        self.assertEqual(source['model_source'], 'service_reported')
        for fact in supplied:
            self.assertIn(json.dumps(fact['id']), prompt)
            self.assertIn(fact['evidence_sha256'], prompt)
        self.assertEqual(supplied, before)

    def test_invalid_fact_scope_and_metadata_fail_before_model(self):
        variants = [facts()[:3], facts() + [facts()[0]]]
        for key, value in [('id', 'other'), ('state', 'ready'), ('identity', []),
                           ('data', []), ('evidence_sha256', 'wrong')]:
            variant = facts(); variant[0][key] = value; variants.append(variant)
        variant = facts(); variant[0]['raw_target_body'] = 'NOT_ALLOWED'; variants.append(variant)
        variant = facts(); variant[0]['data']['large'] = 'x' * analysis.MAX_FACT_BYTES; variants.append(variant)
        variant = facts(); variant[0]['data']['not_json'] = float('nan'); variants.append(variant)
        for supplied in variants:
            adapter = FakeAdapter()
            with self.subTest(supplied=str(supplied)[:80]), self.assertRaises(analysis.StatusAnalysisError):
                self.invoke(adapter, supplied)
            self.assertEqual(adapter.calls, [])

    def test_strict_json_never_extracts_markdown_or_trailing_text(self):
        raw = json.dumps(report())
        variants = ['[]', '```json\n' + raw + '\n```', raw + '\nfinished',
            '{"check_id":"a","check_id":"b"}', '{"items":NaN}', 'null']
        for text in variants:
            adapter = FakeAdapter(text)
            with self.subTest(text=text[:70]), self.assertRaises(analysis.StatusAnalysisError):
                self.invoke(adapter)
            self.assertEqual(len(adapter.calls), 1)

    def test_all_evidence_ids_hashes_and_bounded_fields_must_match(self):
        variants = []
        for key, value in [('check_id', 'other'), ('summary', 'x' * 301), ('extra', 'not accepted')]:
            item = report(); item[key] = value; variants.append(item)
        for key, value in [('evidence_id', 'other'), ('evidence_sha256', '0' * 64),
                           ('judgment', 'all_ok'), ('note', '字' * 601), ('extra', True)]:
            item = report(); item['items'][0][key] = value; variants.append(item)
        item = report(); item['items'][1] = deepcopy(item['items'][0]); variants.append(item)
        item = report(); item['items'].pop(); variants.append(item)
        for item in variants:
            adapter = FakeAdapter(json.dumps(item, ensure_ascii=False))
            with self.subTest(item=str(item)[:80]), self.assertRaises(analysis.StatusAnalysisError):
                self.invoke(adapter)
            self.assertEqual(len(adapter.calls), 1)

    def test_output_limit_catches_partial_snapshots_and_final_return(self):
        adapter = FakeAdapter('字' * (analysis.MAX_OUTPUT_BYTES // 3 + 1))
        with self.assertRaises(analysis.StatusAnalysisError) as caught:
            self.invoke(adapter)
        self.assertEqual(caught.exception.code, 'output_capacity')
        self.assertEqual(len(adapter.calls), 1)
        adapter = FakeAdapter()
        adapter.run = lambda *_: dict(text='x' * (analysis.MAX_OUTPUT_BYTES + 1))
        with self.assertRaises(analysis.StatusAnalysisError):
            self.invoke(adapter)

    def test_cancellation_before_or_after_analysis_never_retries(self):
        adapter, cancel = FakeAdapter(), threading.Event(); cancel.set()
        with self.assertRaises(InterruptedError):
            self.invoke(adapter, cancel=cancel)
        self.assertEqual(adapter.calls, [])
        cancel.clear(); original = adapter.run
        def cancelling(*args):
            value = original(*args); cancel.set(); return value
        adapter.run = cancelling
        with self.assertRaises(InterruptedError):
            self.invoke(adapter, cancel=cancel)
        self.assertEqual(len(adapter.calls), 1)

    def test_private_adapter_errors_are_not_echoed_or_retried(self):
        adapter = FakeAdapter()
        def fail(*_):
            raise ValueError('PRIVATE_TOKEN')
        adapter.capture_route = fail
        with self.assertRaises(analysis.StatusAnalysisError) as caught:
            analysis.prepare(adapter)
        self.assertNotIn('PRIVATE_TOKEN', str(caught.exception))
        adapter = FakeAdapter(); adapter.run = fail
        with self.assertRaises(analysis.StatusAnalysisError) as caught:
            self.invoke(adapter)
        self.assertEqual(caught.exception.code, 'analysis_failed')
        self.assertNotIn('PRIVATE_TOKEN', str(caught.exception))

    def test_unrecognized_reported_model_source_is_not_published(self):
        adapter = FakeAdapter(); adapter.output_source = 'verified_actual_model'
        with self.assertRaises(analysis.StatusAnalysisError):
            self.invoke(adapter)


if __name__ == '__main__':
    unittest.main()
