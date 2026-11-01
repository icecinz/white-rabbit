"""DSH advisory catalogue projection uses fictional data only."""
import copy
import json
import unittest

from rabbit_runtime.dsh.model_catalog import MAX_EFFORTS, project, validate_projection


def sample():
    return {'current': {'provider': 'example', 'model': 'alpha', 'password': 'PRIVATE'},
        'routable': True, 'groups': [{'id': 'example', 'name': 'Example', 'models': [
            {'id': 'alpha', 'name': 'Alpha', 'description': 'PRIVATE',
             'reasoning': {'efforts': [{'id': 'off', 'name': 'Off', 'description': 'PRIVATE'},
                                       {'id': 'extra-deep', 'name': 'Deep'}], 'defaultEffort': 'off'}},
            {'id': 'beta', 'name': 'Beta'}]}],
        'failures': [{'id': 'failed', 'name': 'Failed', 'message': 'PRIVATE /Users/someone/secret'}]}


class DshModelCatalogTests(unittest.TestCase):
    def test_project_only_safe_display_fields_and_preserve_partial_custom_strength(self):
        result = project(sample())
        self.assertEqual(result, {'models': [
            {'provider': 'example', 'provider_name': 'Example', 'model': 'alpha', 'name': 'Alpha', 'efforts': ['off', 'extra-deep']},
            {'provider': 'example', 'provider_name': 'Example', 'model': 'beta', 'name': 'Beta', 'efforts': None}], 'partial': True})
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(validate_projection(result), result)
        result['models'][0]['efforts'].append('changed')
        self.assertEqual(project(sample())['models'][0]['efforts'], ['off', 'extra-deep'])

    def test_empty_valid_catalogue_is_distinct_from_failure(self):
        value = {'current': {}, 'routable': False, 'groups': [], 'failures': []}
        self.assertEqual(project(value), {'models': [], 'partial': False})
        value['failures'] = [{'message': 'private'}]
        self.assertEqual(project(value), {'models': [], 'partial': True})

    def test_reject_malformed_native_result_and_duplicates(self):
        cases = []
        for key, val in (('groups', None), ('failures', 'secret'), ('routable', 1), ('current', None)):
            value = sample(); value[key] = val; cases.append(value)
        value = sample(); value['groups'].append(copy.deepcopy(value['groups'][0])); cases.append(value)
        value = sample(); value['groups'][0]['models'].append(copy.deepcopy(value['groups'][0]['models'][0])); cases.append(value)
        value = sample(); value['groups'][0]['models'][1]['reasoning'] = None; cases.append(value)
        value = sample(); value['groups'][0]['models'][0]['reasoning']['efforts'] *= 2; cases.append(value)
        value = sample(); value['groups'][0]['models'][0]['reasoning']['efforts'] = [{'id': str(n)} for n in range(MAX_EFFORTS + 1)]; cases.append(value)
        for case in cases:
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, 'DSH 模型目录'):
                project(case)

    def test_reject_sensitive_or_corrupt_persisted_fields_without_echoing(self):
        for bad in ('/Users/name/secret', 'sk-123456789012345678', 'bad\u202evalue', ''):
            value = project(sample()); value['models'][0]['name'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError) as result:
                validate_projection(value)
            self.assertNotIn(bad, str(result.exception)) if bad else None
        value = project(sample()); value['models'][0]['password'] = 'PRIVATE'
        with self.assertRaises(ValueError): validate_projection(value)
        value = project(sample()); value['partial'] = 1
        with self.assertRaises(ValueError): validate_projection(value)


if __name__ == '__main__':
    unittest.main()
