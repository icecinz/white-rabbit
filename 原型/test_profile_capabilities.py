"""Synthetic pure-function tests: no cache, credential, DB or model access."""
import copy
import json
import unittest

from profile_capabilities import (MAX_REASONING_LEVEL_ROWS, model_effort_capability,
                                  validate_profile_effort, public_effort_capability)


class ProfileCapabilityTests(unittest.TestCase):
    def test_snake_cache_explicit_ultra_and_max_are_actual_declared_values(self):
        row = {'slug': 'synthetic-sol', 'supported_reasoning_levels': [
            {'effort': 'medium'}, {'effort': 'ultra'}, {'effort': 'max'}],
            'default_reasoning_level': 'ultra'}
        before = copy.deepcopy(row)
        capability = model_effort_capability('codex', row)
        self.assertEqual(capability['supported_efforts'], ['medium', 'ultra', 'max'])
        self.assertEqual(capability['default_effort'], 'ultra')
        self.assertTrue(capability['explicit_effort_supported'])
        self.assertEqual(validate_profile_effort(capability, 'ultra'), 'ultra')
        self.assertEqual(validate_profile_effort(capability, 'max'), 'max')
        self.assertIsNone(validate_profile_effort(capability, None))
        self.assertEqual(row, before)

    def test_camel_cache_shape_and_consistent_aliases(self):
        row = {'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'max'}],
               'defaultReasoningEffort': 'max'}
        capability = model_effort_capability('codex', row)
        self.assertEqual(capability['supported_efforts'], ['low', 'max'])
        self.assertEqual(capability['default_effort'], 'max')
        row.update(supported_reasoning_levels=[{'effort': 'max'}, {'effort': 'low'}], default_reasoning_level='max')
        both = model_effort_capability('codex', row)
        self.assertEqual(both['supported_efforts'], ['max', 'low'])
        self.assertTrue(both['explicit_effort_supported'])

    def test_missing_row_empty_or_unknown_list_does_not_invent_global_levels(self):
        for row in (None, [], 'PRIVATE_INPUT_PATH', {}, {'supported_reasoning_levels': []},
                    {'supported_reasoning_levels': [{'effort': 'future'}]}, {'supported_reasoning_levels': [{'effort': ''}]}):
            capability = model_effort_capability('codex', row)
            self.assertEqual(capability['supported_efforts'], [])
            self.assertFalse(capability['explicit_effort_supported'])
            self.assertIsNone(validate_profile_effort(capability, None))
            with self.assertRaises(ValueError):
                validate_profile_effort(capability, 'medium')

    def test_declared_default_is_informative_not_authorization_for_explicit_override(self):
        capability = model_effort_capability('codex', {'default_reasoning_level': 'high'})
        self.assertEqual(capability['default_effort'], 'high')
        self.assertEqual(capability['supported_efforts'], [])
        self.assertFalse(capability['explicit_effort_supported'])
        self.assertIsNone(validate_profile_effort(capability, None))
        with self.assertRaises(ValueError):
            validate_profile_effort(capability, 'high')

    def test_hermes_cannot_reuse_codex_effort_controls(self):
        capability = model_effort_capability('hermes', {'supported_reasoning_levels': [{'effort': 'ultra'}],
                                                      'default_reasoning_level': 'ultra'})
        self.assertEqual(capability['supported_efforts'], [])
        self.assertIsNone(capability['default_effort'])
        self.assertFalse(capability['explicit_effort_supported'])
        self.assertIn('Agent 默认', capability['effort_source'])
        with self.assertRaises(ValueError):
            validate_profile_effort(capability, 'ultra')

    def test_duplicate_levels_collapse_in_declaration_order(self):
        capability = model_effort_capability('codex', {'supported_reasoning_levels': [
            {'effort': 'max'}, {'effort': 'low'}, {'effort': 'max'}, {'effort': 'low'}]})
        self.assertEqual(capability['supported_efforts'], ['max', 'low'])
        self.assertTrue(capability['explicit_effort_supported'])

    def test_explicit_none_wire_value_is_distinct_from_agent_default_null(self):
        capability = model_effort_capability('codex', {'supported_reasoning_levels': [
            {'effort': 'none'}, {'effort': 'minimal'}, {'effort': 'low'}]})
        self.assertEqual(capability['supported_efforts'], ['none', 'minimal', 'low'])
        self.assertEqual(validate_profile_effort(capability, 'none'), 'none')
        self.assertIsNone(validate_profile_effort(capability, None))

    def test_unknown_values_are_skipped_without_promoting_them_or_other_globals(self):
        capability = model_effort_capability('codex', {'supported_reasoning_levels': [
            {'effort': 'future'}, {'effort': 'MAX'}, {'effort': ' max '}, {'effort': 'max'}]})
        self.assertEqual(capability['supported_efforts'], ['max'])
        for effort in ('ultra', 'MAX', ' max ', 'future'):
            with self.assertRaises(ValueError):
                validate_profile_effort(capability, effort)

    def test_malformed_or_overlong_arrays_fail_closed_without_truncating_prefix(self):
        values = (True, {'effort': 'max'}, 'max', [{'effort': 'max'}, None],
                  ['max'], [{'description': 'PRIVATE_DESCRIPTION'}], [{'effort': []}],
                  [{'effort': 1}], [{'effort': True}], [{'effort': 'x' * 17}],
                  [{'effort': 'max', 'reasoningEffort': 'ultra'}],
                  [{'effort': 'max'}] * (MAX_REASONING_LEVEL_ROWS + 1))
        for levels in values:
            capability = model_effort_capability('codex', {'supported_reasoning_levels': levels,
                                                         'default_reasoning_level': 'max'})
            self.assertEqual(capability['supported_efforts'], [])
            self.assertIsNone(capability['default_effort'])
            self.assertFalse(capability['explicit_effort_supported'])
            with self.assertRaises(ValueError):
                validate_profile_effort(capability, 'max')

    def test_conflicting_alias_lists_are_not_unioned_into_new_capabilities(self):
        capability = model_effort_capability('codex', {'supported_reasoning_levels': [{'effort': 'high'}],
            'supportedReasoningEfforts': [{'reasoningEffort': 'ultra'}]})
        self.assertEqual(capability['supported_efforts'], [])
        self.assertFalse(capability['explicit_effort_supported'])
        self.assertIn('冲突', capability['effort_source'])

    def test_invalid_absent_or_conflicting_default_does_not_override_declared_support(self):
        for default in (False, 1, [], {}, '', 'future', 'ultra', 'high '):
            capability = model_effort_capability('codex', {'supported_reasoning_levels': [{'effort': 'high'}],
                                                        'default_reasoning_level': default})
            self.assertEqual(capability['supported_efforts'], ['high'])
            self.assertTrue(capability['explicit_effort_supported'])
            self.assertIsNone(capability['default_effort'])
        capability = model_effort_capability('codex', {'supported_reasoning_levels': [{'effort': 'high'}, {'effort': 'max'}],
            'default_reasoning_level': 'high', 'defaultReasoningEffort': 'max'})
        self.assertIsNone(capability['default_effort'])

    def test_public_result_is_fixed_and_does_not_copy_private_or_unrelated_fields(self):
        capability = model_effort_capability('codex', {'supported_reasoning_levels': [
            {'effort': 'max', 'description': 'PRIVATE_TOKEN_DESCRIPTION'}],
            'api_key': 'PRIVATE_KEY', 'path': '/PRIVATE_PATH', 'slug': 'PRIVATE_MODEL', 'default_reasoning_level': 'max'})
        self.assertEqual(set(capability), {'supported_efforts', 'default_effort', 'explicit_effort_supported', 'effort_source'})
        self.assertNotIn('PRIVATE', json.dumps(capability))

    def test_validation_does_not_coerce_bad_selection_or_trust_malformed_capability(self):
        good = model_effort_capability('codex', {'supported_reasoning_levels': [{'effort': 'max'}]})
        for selected in ('', False, 1, [], {}, 'MAX', ' max ', 'Agent默认', 'x' * 1000):
            with self.assertRaises(ValueError):
                validate_profile_effort(good, selected)
        for capability in (None, {}, {'explicit_effort_supported': 'true', 'supported_efforts': ['max']},
                           {'explicit_effort_supported': True, 'supported_efforts': 'max'},
                           {'explicit_effort_supported': True, 'supported_efforts': ['max', 'max']},
                           {'explicit_effort_supported': True, 'supported_efforts': ['max', 'future']},
                           {'explicit_effort_supported': True, 'supported_efforts': ['max'] * 9}):
            with self.assertRaises(ValueError):
                validate_profile_effort(capability, 'max')
        # The validator accepts a full public catalog row too, without reading
        # or returning provider/name/availability fields.
        self.assertEqual(validate_profile_effort(dict(good, name='Synthetic public model'), 'max'), 'max')

    def test_invalid_backend_is_not_a_new_supported_adapter(self):
        for backend in ('Codex', 'mem0', '', None, False):
            with self.assertRaises(ValueError):
                model_effort_capability(backend, {})

    def test_public_projection_bounds_fields_and_never_copies_private_source_diagnostics(self):
        valid = model_effort_capability('codex', {'supported_reasoning_levels': [{'effort': 'ultra'}]})
        projected = public_effort_capability('codex', dict(valid, api_key='PRIVATE_KEY'))
        self.assertEqual(projected, valid)
        projected['supported_efforts'].append('max')
        self.assertEqual(valid['supported_efforts'], ['ultra'])
        for malformed in (dict(valid, supported_efforts=['ultra'] * 9),
                          dict(valid, supported_efforts=['ultra', 'PRIVATE_EFFORT']),
                          dict(valid, explicit_effort_supported='true'),
                          dict(valid, explicit_effort_supported=False)):
            result = public_effort_capability('codex', malformed)
            self.assertFalse(result['explicit_effort_supported'])
            self.assertEqual(result['supported_efforts'], [])
            self.assertNotIn('PRIVATE', json.dumps(result))
        for source in ('PRIVATE_SECRET_SOURCE', {'key': 'PRIVATE_KEY'}, 'x' * 1000):
            result = public_effort_capability('codex', dict(valid, effort_source=source))
            self.assertEqual(result['supported_efforts'], ['ultra'])
            self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertFalse(public_effort_capability('hermes', valid)['explicit_effort_supported'])


if __name__ == '__main__':
    unittest.main()
