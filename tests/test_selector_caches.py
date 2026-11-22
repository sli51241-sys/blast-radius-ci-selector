"""Dependency results remain stable across callers and attribute-name queries."""

from pathlib import Path
import tempfile
import unittest

from selector import Selector


def full_attribute_users(selector, names):
    """Evaluate the original combined static/dynamic locations without caching."""
    locations = {}
    for name in names:
        for path, lines in selector.symbols.get('attributes', {}).get(name, {}).items():
            locations.setdefault(path, set()).update(lines)
    for path, lines in selector.symbols.get('dynamic_attributes', {}).items():
        locations.setdefault(path, set()).update(lines)

    result = set()
    for path, lines in locations.items():
        info = selector.files.get(path, {})
        for line in lines:
            result.update(selector.numbers(info.get('lines', {}).get(str(line), [])))
        if set(info.get('global', [])).intersection(lines):
            # Deliberately reconstruct file users rather than read its cache.
            result.update(selector.numbers(info.get('reads', [])))
            result.update(selector.by_file.get(path, set()))
            for indices in info.get('lines', {}).values():
                result.update(selector.numbers(indices))
            result.update(selector.import_users(path))
    return result


class SelectorCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Noncontiguous test numbers distinguish baseline indices from outputs.
        self.tests = [
            {'number': number, 'id': f'tests/test_{i}.py::case',
             'file': f'tests/test_{i}.py', 'seconds': .01}
            for i, number in enumerate([41, 7, 103, 29, 67, 11, 83, 97])
        ]
        self.selector = Selector(self.tests, Path(temporary.name), None)
        self.selector.map_numbers = {i: test['number'] for i, test in enumerate(self.tests)}

    def test_file_users_are_independent_mutable_sets(self):
        selector = self.selector
        path = 'tests/test_7.py'
        selector.files[path] = {
            'reads': [2], 'lines': {'2': [0, 999], '3': [1, 0]}, 'global': [],
        }
        expected = {41, 7, 103, 97}
        first = selector.file_tests(path)
        self.assertEqual(first, expected)
        first.clear()
        first.add(-1)
        second = selector.file_tests(path)
        self.assertEqual(second, expected)
        self.assertIsNot(first, second)
        second.remove(41)
        second.add(-2)
        self.assertEqual(selector.file_tests(path), expected)
        self.assertEqual(selector.by_file[path], {97})
        self.assertEqual(selector.files[path]['reads'], [2])
        self.assertEqual(selector.files[path]['lines']['2'], [0, 999])

    def test_empty_file_cache_does_not_share_callers_mutations(self):
        first = self.selector.file_tests('absent.py')
        self.assertEqual(first, set())
        first.add(41)
        second = self.selector.file_tests('absent.py')
        self.assertEqual(second, set())
        second.add(7)
        self.assertEqual(self.selector.file_tests('absent.py'), set())

    def attribute_fixture(self):
        selector = self.selector
        selector.files = {
            'pkg/runtime.py': {'lines': {'2': [0], '9': [6]}, 'reads': [], 'global': [9]},
            'pkg/meta.py': {'lines': {'10': [1], '11': [2]}, 'reads': [3], 'global': [10]},
            'pkg/static.py': {'lines': {'4': [6], '8': [7]}, 'reads': [], 'global': []},
            'pkg/lazy.py': {'lines': {'20': [5]}, 'reads': [], 'global': []},
        }
        selector.imports = {'tests/test_4.py': ['pkg/meta.py']}
        selector.runtime_imports = {'pkg/lazy.py': {'pkg/meta.py': [20]}}
        selector.symbols = {
            'attributes': {
                'alpha': {'pkg/static.py': [4], 'pkg/runtime.py': [2]},
                'beta': {'pkg/static.py': [8]},
                'global': {'pkg/runtime.py': [9]},
                'missing_source': {'pkg/absent.py': [1]},
            },
            'dynamic_attributes': {'pkg/runtime.py': [2], 'pkg/meta.py': [10]},
        }
        return selector

    def test_dynamic_union_matches_full_locations_across_name_queries(self):
        selector = self.attribute_fixture()
        queries = [set(), {'alpha'}, {'beta'}, {'alpha', 'beta'}, {'global'},
                   {'missing_source'}, {'unknown'}, set(), {'beta'}, {'alpha'}]
        expected = [full_attribute_users(selector, names) for names in queries]
        self.assertEqual(expected[0], {41, 7, 103, 29, 67, 11})
        self.assertEqual(expected[1] - expected[0], {83})
        self.assertEqual(expected[2] - expected[0], {97})
        for names, users in zip(queries, expected):
            with self.subTest(names=names):
                self.assertEqual(selector.attribute_users(names), users)

    def test_attribute_result_mutations_do_not_poison_later_queries(self):
        selector = self.attribute_fixture()
        expected = full_attribute_users(selector, set())
        result = selector.attribute_users({'alpha'})
        result.clear()
        result.add(-1)
        self.assertEqual(selector.attribute_users(set()), expected)
        result = selector.attribute_users({'beta'})
        self.assertEqual(result, expected | {97})
        result.discard(41)
        result.add(83)
        self.assertEqual(selector.attribute_users({'beta'}), expected | {97})
        self.assertEqual(selector.attribute_users(set()), expected)


if __name__ == '__main__':
    unittest.main()
