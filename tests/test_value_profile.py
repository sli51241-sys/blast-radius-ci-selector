import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.value_profile import (
    HOOK, Recorder, classify_context, instrument, single_predicate_change,
)


class ValueProfileTests(unittest.TestCase):
    def execute(self, source, values=None, recorder=None):
        recorder = recorder or Recorder()
        namespace = {HOOK: recorder.capture, **(values or {})}
        tree = instrument(ast.parse(source), 'pkg/core.py', recorder)
        exec(compile(tree, 'pkg/core.py', 'exec'), namespace)
        return namespace, recorder

    def test_comparison_preserves_custom_result_and_evaluation_order(self):
        events, marker = [], object()

        class Value:
            def __lt__(self, other):
                events.append('compare')
                return marker

        value = Value()
        def left():
            events.append('left')
            return value
        def right():
            events.append('right')
            return value
        namespace, recorder = self.execute('result = left() < right()', locals())
        self.assertIs(namespace['result'], marker)
        self.assertEqual(events, ['left', 'right', 'compare'])
        domains = next(iter(recorder.export()['sites'].values()))['contexts']['@collection']
        self.assertEqual(domains['left']['unknown'], 1)
        self.assertEqual(domains['right']['unknown'], 1)

    def test_short_circuit_and_chained_compare_do_not_eagerly_evaluate(self):
        events = []
        def fail():
            events.append('called')
            raise AssertionError('must stay short circuited')
        namespace, recorder = self.execute(
            'a = False and (fail() < 2)\nb = 3 < 2 < fail()\nc = True or not fail()', locals())
        self.assertFalse(namespace['a'])
        self.assertFalse(namespace['b'])
        self.assertTrue(namespace['c'])
        self.assertEqual(events, [])
        chained = [site for site in recorder.sites.values() if site['kind'] == 'chained_compare']
        self.assertEqual(len(chained), 1)
        self.assertFalse(chained[0]['supported'])
        self.assertEqual(chained[0]['contexts'], {})

    def test_operator_and_right_operand_exceptions_preserved(self):
        events = []
        def left():
            events.append('left')
            return 1
        def right():
            events.append('right')
            raise ValueError('right failed')
        with self.assertRaisesRegex(ValueError, 'right failed'):
            self.execute('left() < right()', locals())
        self.assertEqual(events, ['left', 'right'])
        with self.assertRaises(TypeError):
            self.execute('result = None < 1')

    def test_not_truth_tests_once_without_inspecting_unknown_object(self):
        events = []
        class Value:
            def __bool__(self):
                events.append('bool')
                return False
            def __repr__(self):
                raise AssertionError('profiler invoked repr')
            def __hash__(self):
                raise AssertionError('profiler invoked hash')
        value = Value()
        namespace, recorder = self.execute('result = not value', locals())
        self.assertTrue(namespace['result'])
        self.assertEqual(events, ['bool'])
        self.assertEqual(next(iter(recorder.export()['sites'].values()))[
            'contexts']['@collection']['operand']['unknown'], 1)

    def test_assignment_expression_keeps_original_scope(self):
        namespace, _ = self.execute('result = ((value := 3) < 4)\nassert value == 3')
        self.assertTrue(namespace['result'])
        self.assertEqual(namespace['value'], 3)

    def test_value_bounds_and_type_distinctions(self):
        recorder = Recorder(max_values=2, max_value_chars=3)
        tree = ast.parse('x < 2')
        key = recorder.register('x.py', tree.body[0].value, 'Lt')
        for value in (True, 1, 2, object(), 'long', [1]):
            self.assertIs(recorder.capture(key, 'left', value), value)
        domain = recorder.export()['sites'][key]['contexts']['@collection']['left']
        self.assertEqual([v['type'] for v in domain['values']], ['bool', 'int'])
        self.assertTrue(domain['overflow'])
        self.assertEqual(domain['unknown'], 3)
        self.assertNotEqual(recorder.encode(0.0), recorder.encode(-0.0))

    def test_record_and_event_budgets_turn_unknown(self):
        recorder = Recorder(max_records=1, max_events=2)
        key = recorder.register('x.py', ast.parse('not x').body[0].value, 'Not')
        recorder.context = 'test:1'
        recorder.capture(key, 'operand', 1)
        recorder.context = 'test:2'
        recorder.capture(key, 'operand', 2)
        recorder.capture(key, 'operand', 3)
        self.assertEqual(recorder.global_unknown,
                         {'record_budget_exhausted', 'event_budget_exhausted'})

    def test_hook_name_collision_declines_instrumentation(self):
        recorder = Recorder()
        namespace, _ = self.execute(f'{HOOK} = 7\nx = 1 < 2', recorder=recorder)
        self.assertTrue(namespace['x'])
        self.assertEqual(namespace[HOOK], 7)
        self.assertEqual(recorder.skipped_modules, {'pkg/core.py': 'instrumentation_name_collision'})

    def test_exception_and_pattern_bindings_cannot_shadow_injected_hook(self):
        for binding in (f'except Exception as {HOOK}:\n  pass',
                        f'except Exception:\n  match object():\n   case {HOOK}: pass'):
            source = f'def f():\n try:\n  return 1 < 2\n {binding}\nresult = f()'
            namespace, recorder = self.execute(source)
            self.assertTrue(namespace['result'])
            self.assertEqual(recorder.sites, {})
            self.assertIn('pkg/core.py', recorder.skipped_modules)

    def test_cli_wall_budget_disables_inference(self):
        tool = Path(__file__).resolve().parents[1] / 'tools/value_profile.py'
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'timeout.json'
            result = subprocess.run([sys.executable, '-B', str(tool), 'profile',
                '--source', temporary, '--catalog', str(Path(temporary) / 'unused.json'),
                '--output', str(output), '--wall-timeout', '0.001'],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
            profile = json.loads(output.read_text())
            self.assertFalse(profile['inference_allowed'])
            self.assertFalse(profile['baseline_healthy'])
            self.assertEqual(profile['global_unknown'], ['wall_time_budget_exhausted'])

    def profile_threshold(self, values):
        recorder = Recorder()
        recorder.context = 'test:17'
        for value in values:
            self.execute('result = x > 3', {'x': value}, recorder)
        exported = recorder.export()
        return next(iter(exported['sites'].values())), exported

    def test_evaluator_unknown_and_cross_product_are_conservative(self):
        site, profile = self.profile_threshold([1, 2, 5])
        self.assertEqual(classify_context(site, 'test:17', 'Gt', 'GtE', profile), 'observed_equivalent')
        self.assertEqual(classify_context(site, 'test:99', 'Gt', 'GtE', profile), 'unknown')
        self.assertEqual(classify_context(site, 'test:17', 'Is', 'IsNot', profile), 'unknown')
        profile['context_gaps']['test:17'] = {'subprocess.Popen': 1}
        self.assertEqual(classify_context(site, 'test:17', 'Gt', 'GtE', profile), 'unknown')
        site, profile = self.profile_threshold([3])
        self.assertEqual(classify_context(site, 'test:17', 'Gt', 'GtE', profile), 'potentially_changed')

    def test_recognizes_only_single_whole_program_predicate_change(self):
        def recognize(old, new):
            return single_predicate_change(ast.parse(old), ast.parse(new))
        self.assertEqual(recognize('x = n > 3', 'x = n >= 3')[1:], ('Gt', 'GtE'))
        self.assertEqual(recognize('if not key: pass', 'if key: pass')[1:], ('Not', 'Identity'))
        self.assertIsNone(recognize('x = n > 3', 'x = n >= 4'))
        self.assertIsNone(recognize('x = n > 3\ny = 1', 'x = n >= 3\ny = 2'))
        self.assertIsNone(recognize('x = n', 'x = not n'))

    def test_cli_healthy_catalog_collection_context_and_child_gaps(self):
        tool = Path(__file__).resolve().parents[1] / 'tools/value_profile.py'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            (source / 'sqlglot').mkdir(parents=True)
            (source / 'tests').mkdir()
            (source / 'sqlglot/__init__.py').write_text('')
            (source / 'sqlglot/core.py').write_text('COLLECTED = 1 < 2\ndef check(x):\n return x < 2\n')
            (source / 'tests/test_probe.py').write_text(
                'import subprocess, sys\nfrom sqlglot.core import check\n'
                'def test_one():\n assert check(1)\n'
                'def test_two():\n subprocess.run([sys.executable, "-c", "pass"], check=True)\n assert not check(3)\n')
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(source), '-c', 'user.name=Profile Fixture',
                            '-c', 'user.email=profile@example.invalid', 'commit', '-qm', 'baseline'], check=True)
            commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
            catalog = root / 'catalog.json'
            catalog.write_text(json.dumps({'tests': [
                {'id': f'tests/test_probe.py::{name}', 'number': number,
                 'file': 'tests/test_probe.py', 'seconds': .01}
                for name, number in [('test_one', 17), ('test_two', 43)]]}))
            output = root / 'profile.json'
            result = subprocess.run([sys.executable, '-B', str(tool), 'profile', '--source', str(source),
                '--catalog', str(catalog), '--output', str(output), '--commit', commit],
                capture_output=True, text=True, env={**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            profile = json.loads(output.read_text())
            self.assertTrue(profile['baseline_healthy'])
            self.assertEqual(profile['missing_tests'], [])
            self.assertEqual(profile['failed_tests'], [])
            self.assertEqual(profile['context_gaps']['test:43']['subprocess.Popen'], 1)
            contexts = [context for site in profile['sites'].values() for context in site['contexts']]
            self.assertIn('@collection', contexts)
            self.assertIn('test:17', contexts)
            self.assertIn('test:43', contexts)


if __name__ == '__main__':
    unittest.main()
