#!/usr/bin/env python3
"""Development-only, bounded operand observations from a pinned baseline.

Never consumes failure labels or public sample patches. This is an offline
experiment, not a selector map: incomplete or unobserved domains stay unknown.
Only operands are wrapped, so Python still performs each original operation.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import copy
import hashlib
import importlib.abc
import importlib.machinery
import json
import operator
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading


PIN = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
HOOK = '__rwx_baseline_operand_capture_73915__'
CHILD_EVENTS = {'subprocess.Popen', 'os.fork', 'os.forkpty', 'os.posix_spawn', 'os.system'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def site_id(path, node):
    return f'{path}:{node.lineno}:{node.col_offset}:{type(node).__name__}'


class Recorder:
    def __init__(self, *, max_sites=20000, max_records=200000, max_values=8,
                 max_value_chars=256, max_events=50000000):
        self.limits = dict(max_sites=max_sites, max_records=max_records, max_values=max_values,
                           max_value_chars=max_value_chars, max_events=max_events)
        if any(value < 1 for value in self.limits.values()):
            raise ValueError('profiling limits must be positive')
        self.sites = {}
        self.source_sha256 = {}
        self.skipped_modules = {}
        self.gaps = {}
        self.global_unknown = set()
        self.context = '@collection'
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.events = self.records = 0

    def register(self, path, node, kind, supported=True):
        key = site_id(path, node)
        if key not in self.sites:
            if len(self.sites) >= self.limits['max_sites']:
                self.global_unknown.add('site_budget_exhausted')
                return None
            self.sites[key] = {'path': path, 'line': node.lineno, 'column': node.col_offset,
                               'kind': kind, 'supported': supported, 'contexts': {}}
        return key

    def audit(self, event, args):
        if event in CHILD_EVENTS:
            gaps = self.gaps.setdefault(self.context, Counter())
            gaps[event] += 1

    def encode(self, value):
        kind = type(value)
        if value is None:
            return {'type': 'NoneType', 'value': None}
        if value is Ellipsis:
            return {'type': 'ellipsis', 'value': None}
        if kind is bool:
            return {'type': 'bool', 'value': value}
        if kind is int and value.bit_length() <= 256:
            return {'type': 'int', 'value': str(value)}
        if kind is float:
            return {'type': 'float', 'value': value.hex()}
        if kind is complex:
            return {'type': 'complex', 'value': [value.real.hex(), value.imag.hex()]}
        if kind is str and len(value) <= self.limits['max_value_chars']:
            return {'type': 'str', 'value': value}
        if kind is bytes and len(value) <= self.limits['max_value_chars']:
            return {'type': 'bytes', 'value': value.hex()}
        return None

    def capture(self, key, lane, value):
        # Never inspect an unknown object's repr, attributes, hash or truthiness.
        # Returning the same object preserves operator results and exceptions.
        try:
            if os.getpid() != self.pid:
                return value
            if threading.get_ident() != self.thread:
                self.global_unknown.add('background_thread_context_unattributed')
                return value
            self.events += 1
            if self.events > self.limits['max_events']:
                self.global_unknown.add('event_budget_exhausted')
                return value
            contexts = self.sites[key]['contexts']
            if self.context not in contexts:
                if self.records >= self.limits['max_records']:
                    self.global_unknown.add('record_budget_exhausted')
                    return value
                contexts[self.context] = {}
                self.records += 1
            domain = contexts[self.context].setdefault(lane, {'observations': 0, 'values': {},
                                                            'unknown': 0, 'overflow': False})
            domain['observations'] += 1
            encoded = self.encode(value)
            if encoded is None:
                domain['unknown'] += 1
            else:
                token = json.dumps(encoded, sort_keys=True, separators=(',', ':'))
                if token in domain['values'] or len(domain['values']) < self.limits['max_values']:
                    domain['values'][token] = encoded
                else:
                    domain['overflow'] = True
        except Exception:
            self.global_unknown.add('capture_failed')
        return value

    def export(self):
        sites = copy.deepcopy(self.sites)
        for site in sites.values():
            for context in site['contexts'].values():
                for domain in context.values():
                    domain['values'] = list(domain['values'].values())
        return {'version': 1, 'limits': self.limits, 'events': self.events,
                'records': self.records, 'sites': sites, 'source_sha256': self.source_sha256,
                'skipped_modules': self.skipped_modules, 'global_unknown': sorted(self.global_unknown),
                'context_gaps': {context: dict(events) for context, events in self.gaps.items()},
                'child_process_observations_merged': False,
                'unobserved_domain_policy': 'unknown',
                'domain_policy': 'exact builtin scalars only; independent operand overapproximation'}


def instrument(tree, path, recorder):
    # A lexical collision is declined rather than replacing a user binding.
    if any(any(name in {'id', 'arg', 'name', 'asname', 'rest'} and value == HOOK
               for name, value in ast.iter_fields(node))
           or (isinstance(node, (ast.Global, ast.Nonlocal)) and HOOK in node.names)
           or (isinstance(node, ast.alias) and node.name.split('.')[0] == HOOK)
           for node in ast.walk(tree)):
        recorder.skipped_modules[path] = 'instrumentation_name_collision'
        return tree

    class Instrument(ast.NodeTransformer):
        def wrapped(self, value, key, lane):
            return ast.copy_location(ast.Call(func=ast.Name(id=HOOK, ctx=ast.Load()),
                args=[ast.Constant(value=key), ast.Constant(value=lane), value], keywords=[]), value)

        def visit_Compare(self, node):
            self.generic_visit(node)
            if len(node.ops) != 1:
                recorder.register(path, node, 'chained_compare', supported=False)
                return node
            key = recorder.register(path, node, type(node.ops[0]).__name__)
            if key:
                node.left = self.wrapped(node.left, key, 'left')
                node.comparators[0] = self.wrapped(node.comparators[0], key, 'right')
            return node

        def visit_UnaryOp(self, node):
            self.generic_visit(node)
            if isinstance(node.op, ast.Not):
                key = recorder.register(path, node, 'Not')
                if key:
                    node.operand = self.wrapped(node.operand, key, 'operand')
            return node

    return ast.fix_missing_locations(Instrument().visit(tree))


class BaselineFinder(importlib.abc.MetaPathFinder):
    def __init__(self, source, recorder, package='sqlglot'):
        self.source, self.recorder, self.package = Path(source).resolve(), recorder, package

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self.package and not fullname.startswith(self.package + '.'):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if not spec or not spec.origin or not spec.origin.endswith('.py'):
            return None
        file = Path(spec.origin).resolve()
        if not file.is_relative_to(self.source):
            return None
        relative = file.relative_to(self.source).as_posix()
        recorder = self.recorder

        class Loader(importlib.machinery.SourceFileLoader):
            def get_code(self, name):
                raw = self.get_data(str(file))
                recorder.source_sha256[relative] = digest(raw)
                tree = instrument(ast.parse(raw, filename=str(file)), relative, recorder)
                return compile(tree, str(file), 'exec', dont_inherit=True)

            def exec_module(self, module):
                module.__dict__[HOOK] = recorder.capture
                super().exec_module(module)

        spec.loader = Loader(fullname, str(file))
        return spec


def run_profile(args):
    source = args.source.resolve()
    args.output = args.output.resolve()
    actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != args.commit:
        raise ValueError('source is not the requested pinned commit')
    if subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=no'], text=True):
        raise ValueError('tracked baseline source files are modified')
    raw_catalog = args.catalog.read_bytes()
    catalog = json.loads(raw_catalog)['tests']
    by_id = {test['id']: test['number'] for test in catalog}
    recorder = Recorder(max_sites=args.max_sites, max_records=args.max_records,
                        max_values=args.max_values, max_value_chars=args.max_value_chars,
                        max_events=args.max_events)
    if any(name == args.package or name.startswith(args.package + '.') for name in sys.modules):
        recorder.global_unknown.add('source_package_preloaded')
    finder = BaselineFinder(source, recorder, args.package)
    sys.meta_path.insert(0, finder)
    sys.path.insert(0, str(source))
    sys.addaudithook(recorder.audit)
    os.chdir(source)
    os.environ['SKIP_INTEGRATION'] = '0'
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ['PATH'] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')
    import pytest

    class Plugin:
        def __init__(self):
            self.collected, self.reported, self.failures = set(), set(), set()
            self.collection_errors = []

        def pytest_collection_modifyitems(self, session, config, items):
            selected = [item for item in items if item.nodeid in by_id]
            deselected = [item for item in items if item.nodeid not in by_id]
            items[:] = selected
            config.hook.pytest_deselected(items=deselected)
            self.collected = {item.nodeid for item in selected}

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            previous = recorder.context
            recorder.context = f'test:{by_id[item.nodeid]}'
            try:
                yield
            finally:
                recorder.context = previous

        def pytest_collectreport(self, report):
            if report.failed:
                self.collection_errors.append(str(report.longrepr)[-3000:])

        def pytest_runtest_logreport(self, report):
            self.reported.add(report.nodeid)
            if report.failed:
                self.failures.add(report.nodeid)

    plugin = Plugin()
    code = 3
    try:
        code = int(pytest.main(['tests', '-q', '--tb=short', '--disable-warnings',
                               '-p', 'no:cacheprovider'], plugins=[plugin]))
    finally:
        result = recorder.export()
        result.update({'source_commit': actual, 'catalog_sha256': digest(raw_catalog),
                       'catalog': catalog, 'pytest_exit_code': code,
                       'baseline_healthy': code == 0 and not plugin.failures and not plugin.collection_errors
                           and set(by_id) <= plugin.collected and set(by_id) <= plugin.reported,
                       'missing_tests': sorted(set(by_id) - plugin.reported),
                       'failed_tests': sorted(plugin.failures), 'collection_errors': plugin.collection_errors})
        result['inference_allowed'] = result['baseline_healthy'] and not result['global_unknown']
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, sort_keys=True) + '\n')
        sys.meta_path.remove(finder)
    return 0 if result['baseline_healthy'] else 2


def run_bounded_profile(args):
    """Keep the observer's wall budget outside the code being observed."""
    if args.wall_timeout <= 0:
        raise ValueError('wall timeout must be positive')
    output = args.output.resolve()
    command = [sys.executable, '-B', str(Path(__file__).resolve()), '_profile-child',
               '--source', str(args.source.resolve()), '--catalog', str(args.catalog.resolve()),
               '--output', str(output), '--commit', args.commit, '--package', args.package]
    for name in Recorder().limits:
        command.extend(['--' + name.replace('_', '-'), str(getattr(args, name))])
    process = subprocess.Popen(command, start_new_session=True)
    try:
        return process.wait(timeout=args.wall_timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({'version': 1, 'baseline_healthy': False,
            'inference_allowed': False, 'global_unknown': ['wall_time_budget_exhausted'],
            'wall_timeout': args.wall_timeout, 'sites': {}, 'source_commit': args.commit,
            'unobserved_domain_policy': 'unknown'}) + '\n')
        return 124


def decode(value):
    kind, raw = value['type'], value['value']
    if kind == 'NoneType': return None
    if kind == 'ellipsis': return Ellipsis
    if kind == 'bool': return raw
    if kind == 'int': return int(raw)
    if kind == 'float': return float.fromhex(raw)
    if kind == 'complex': return complex(*(float.fromhex(part) for part in raw))
    if kind == 'str': return raw
    if kind == 'bytes': return bytes.fromhex(raw)
    raise ValueError('unsupported operand encoding')


OPERATORS = {'Eq': operator.eq, 'NotEq': operator.ne, 'Lt': operator.lt,
             'LtE': operator.le, 'Gt': operator.gt, 'GtE': operator.ge,
             'In': lambda left, right: left in right,
             'NotIn': lambda left, right: left not in right}


def classify_context(site, context, old_operator, new_operator, profile):
    if profile['global_unknown'] or profile['context_gaps'].get('@collection') or profile['context_gaps'].get(context):
        return 'unknown'
    if not site or not site['supported']:
        return 'unknown'
    domains = site['contexts'].get(context, {})
    lanes = ('operand',) if old_operator == 'Not' else ('left', 'right')
    if any(not domains.get(lane) or domains[lane]['unknown'] or domains[lane]['overflow']
           or not domains[lane]['values'] for lane in lanes):
        return 'unknown'
    if old_operator == 'Not' and new_operator == 'Identity':
        # Removing not always changes the operand's truth value. We make no
        # claim that a different truth value necessarily fails a test.
        return 'potentially_changed'
    if old_operator not in OPERATORS or new_operator not in OPERATORS:
        return 'unknown'
    changed = False
    for left in domains['left']['values']:
        for right in domains['right']['values']:
            try:
                a, b = decode(left), decode(right)
                old, new = OPERATORS[old_operator](a, b), OPERATORS[new_operator](a, b)
            except Exception:
                return 'unknown'
            changed |= old != new
    return 'potentially_changed' if changed else 'observed_equivalent'


def _dump(node):
    return ast.dump(node, include_attributes=False)


def single_predicate_change(before, after):
    """Recognize only a whole-program single operator edit or not removal."""
    positions = {}
    for node in ast.walk(after):
        if hasattr(node, 'lineno'):
            positions.setdefault((node.lineno, node.col_offset), []).append(node)

    def rest_equal(old, new):
        class Replace(ast.NodeTransformer):
            def visit(self, node):
                if node is old:
                    return new
                return super().visit(node)
        # Copy before substitution; the mapper pairs originals to copied IDs.
        clone = copy.deepcopy(before)
        old_nodes, cloned_nodes = list(ast.walk(before)), list(ast.walk(clone))
        copied_old = cloned_nodes[next(i for i, node in enumerate(old_nodes) if node is old)]
        old = copied_old
        return _dump(Replace().visit(clone)) == _dump(after)

    for old in ast.walk(before):
        if not hasattr(old, 'lineno'):
            continue
        for new in positions.get((old.lineno, old.col_offset), []):
            if (isinstance(old, ast.Compare) and isinstance(new, ast.Compare)
                    and len(old.ops) == len(new.ops) == 1
                    and type(old.ops[0]) is not type(new.ops[0])
                    and _dump(old.left) == _dump(new.left)
                    and _dump(old.comparators[0]) == _dump(new.comparators[0])
                    and rest_equal(old, new)):
                return old, type(old.ops[0]).__name__, type(new.ops[0]).__name__
            if (isinstance(old, ast.UnaryOp) and isinstance(old.op, ast.Not)
                    and _dump(old.operand) == _dump(new) and rest_equal(old, new)):
                return old, 'Not', 'Identity'
    return None


def evaluate(args):
    profile = json.loads(args.profile.read_text())
    if not profile.get('baseline_healthy'):
        raise ValueError('a healthy complete catalog baseline is required')
    manifest = json.loads(args.manifest.read_text())
    if manifest['source_commit'] != profile['source_commit']:
        raise ValueError('profile and frozen corpus use different source commits')
    if manifest['catalog_sha256'] != profile['catalog_sha256']:
        raise ValueError('profile and frozen corpus use different test catalogs')
    cases = []
    for case in manifest['cases']:
        path = case['path']
        row = {'id': case['id'], 'path': path, 'status': 'unknown', 'reads_failure_labels': False}
        if not path.endswith('.py'):
            cases.append(row)
            continue
        original = (args.source / path).read_text()
        if digest(original.encode()) != case['original_sha256']:
            raise ValueError('frozen baseline source drifted')
        modified = original.replace(case['before'], case['after'], 1) if case['before'] else original + case['after']
        if digest(modified.encode()) != case['modified_sha256']:
            raise ValueError('frozen mutation differs from manifest')
        change = single_predicate_change(ast.parse(original), ast.parse(modified))
        if (profile['global_unknown'] or not change
                or profile['source_sha256'].get(path) != case['original_sha256']):
            cases.append(row)
            continue
        node, old, new = change
        key = site_id(path, node)
        site = profile['sites'].get(key)
        contexts = {'@collection': classify_context(site, '@collection', old, new, profile)}
        for test in profile['catalog']:
            context = f"test:{test['number']}"
            contexts[context] = classify_context(site, context, old, new, profile)
        row.update({'status': 'evaluated', 'site': key, 'old_operator': old, 'new_operator': new,
                    'contexts': contexts, 'counts': dict(Counter(contexts.values()))})
        cases.append(row)
    output = {'source_commit': profile['source_commit'], 'baseline_healthy': True,
              'reads_failure_labels': False, 'changes_runtime_selection': False,
              'warning': 'observed equivalence is experimental; every missing or incomplete domain stays unknown',
              'scope': 'operand-domain evidence only; shared fixture/cache effects require separate dependency proof',
              'cases': cases}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + '\n')
    print(json.dumps({'cases': len(cases), 'recognized': sum(row['status'] == 'evaluated' for row in cases)}))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('profile', '_profile-child'):
        profile = commands.add_parser(command)
        profile.add_argument('--source', type=Path, required=True)
        profile.add_argument('--catalog', type=Path, required=True)
        profile.add_argument('--output', type=Path, required=True)
        profile.add_argument('--commit', default=PIN)
        profile.add_argument('--package', default='sqlglot')
        profile.add_argument('--wall-timeout', type=float, default=900)
        for name, default in Recorder().limits.items():
            profile.add_argument('--' + name.replace('_', '-'), type=int, default=default)
    compare = commands.add_parser('evaluate')
    compare.add_argument('--source', type=Path, required=True)
    compare.add_argument('--profile', type=Path, required=True)
    compare.add_argument('--manifest', type=Path, required=True)
    compare.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'profile':
        return run_bounded_profile(args)
    return run_profile(args) if args.command == '_profile-child' else evaluate(args)


if __name__ == '__main__':
    raise SystemExit(main())
