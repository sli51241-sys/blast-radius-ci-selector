#!/usr/bin/env python3
"""Experimental, evaluation-only initialization dependency graph.

Reads pinned source, baseline maps and unlabeled patch/selection inputs only.
No answer/ground-truth argument exists. Supports direct module/class assignments
and If control dependencies. Unsupported effects preserve current selections.
This is a measured feasibility prototype, NOT exhaustive dynamic cache tracking
or a runtime selector. Nothing here updates the selector or baseline maps.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from class_dependencies import changed_bindings, class_bindings, generated_classes, only_binding_values_changed
from class_projection import _site_key_only
from factory_analysis import _scope_bindings
from patch_analysis import apply_patch_to_text, parse_patch
from scheduler import cost_ms, schedule
from selector import Selector

UNKNOWN = object()


@dataclass(frozen=True, order=True)
class Node:
    path: str
    owner: str
    name: str

    def label(self):
        return f"{self.path}:{self.owner + '.' if self.owner else ''}{self.name}"


@dataclass(frozen=True)
class EnumIdentity:
    """Source member reference; distinct names may alias, so equality is not guessed."""
    path: str
    owner: str
    member: str


@dataclass
class Assignment:
    expression: ast.AST
    guards: tuple
    dependencies: set
    errors: set


def _same(left, right):
    return left is not UNKNOWN and right is not UNKNOWN and type(left) is type(right) and left == right


def _truth(value):
    return bool(value) if value is not UNKNOWN and type(value) in (type(None), bool, int, str) else UNKNOWN


def _encoded(value):
    if value is UNKNOWN:
        return {"exact": False}
    if isinstance(value, EnumIdentity):
        return {"exact": True, "type": "enum_identity", "value": f"{value.path}:{value.owner}.{value.member}"}
    return {"exact": True, "type": type(value).__name__, "value": value}


class CacheGraph:
    """Conservative AST graph, qualified with observed baseline read sites."""
    def __init__(self, source: Path, metadata: dict):
        self.source, self.metadata = source.resolve(), metadata
        self.trees, self.parents, self.module_names = {}, {}, {}
        self.records = defaultdict(list)
        self.edges = defaultdict(set)
        self.nodes, self.classes, self.enum_classes = set(), set(), set()
        self.imports, self.bound = {}, {}
        self.class_bound = {}
        self.reverse = defaultdict(set)
        self.measured_sites = set()
        self.covered_sites, self.bad_sites = set(), defaultdict(set)
        self.unsupported_bare = defaultdict(set)
        for path, owners in metadata.get("sites", {}).items():
            for owner, attributes in owners.items():
                for attribute, sites in attributes.items():
                    for site in sites:
                        node = Node(path, owner, attribute)
                        self.reverse[(site["path"], site["line"], attribute)].add(node)
                        self.measured_sites.add((node, site["path"], site["line"], attribute))
        for path, digest in sorted(metadata.get("source_sha256", {}).items()):
            content = (self.source / path).read_bytes()
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError(f"source digest mismatch: {path}")
            tree = ast.parse(content, filename=path, type_comments=True)
            self.trees[path] = tree
            self.parents[path] = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            self.module_names[path[:-3].replace("/", ".").removesuffix(".__init__")] = path
            self.bound[path] = set(_scope_bindings(tree.body))
            aliases = {}
            for statement in tree.body:
                if isinstance(statement, ast.ImportFrom) and not statement.level and statement.module:
                    for alias in statement.names:
                        aliases[alias.asname or alias.name] = statement.module + "." + alias.name
                elif isinstance(statement, ast.Import):
                    for alias in statement.names:
                        aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
            self.imports[path] = aliases
            for binding in class_bindings(tree):
                self.nodes.add(Node(path, binding.qualname, binding.attribute))
            for name, definitions in _scope_bindings(tree.body).items():
                if any(isinstance(item, ast.Name) for item in definitions):
                    self.nodes.add(Node(path, "", name))
            self._inventory_classes(path, tree.body)
        self.class_index = {(path, owner): cls for path, owner, cls in self.classes}
        self.module_prefixes = sorted(self.module_names.items(), key=lambda item: -len(item[0]))
        # A single source-proven enum inheritance edge is sufficient for
        # identity-only abstraction. Numeric enum values are never guessed.
        for _ in range(len(self.classes) + 1):
            before = len(self.enum_classes)
            for path, owner, cls in self.classes:
                for base in cls.bases:
                    parts = self._parts(base)
                    resolved = self._class_ref(path, parts) if parts else None
                    canonical = self.imports[path].get(parts[0], parts[0]) if parts else ""
                    if canonical in {"enum.Enum", "enum.IntEnum", "enum.StrEnum", "enum.Flag", "enum.IntFlag"} or resolved in self.enum_classes:
                        self.enum_classes.add((path, owner))
            if len(self.enum_classes) == before:
                break
        for path, tree in self.trees.items():
            self._scan(path, "", tree.body, ())

    def _inventory_classes(self, path, statements, prefix=""):
        for statement in statements:
            if isinstance(statement, ast.ClassDef):
                owner = prefix + statement.name
                self.classes.add((path, owner, statement))
                self.class_bound[(path, owner)] = set(_scope_bindings(statement.body))
                self._inventory_classes(path, statement.body, owner + ".")
            elif isinstance(statement, ast.If):
                self._inventory_classes(path, statement.body, prefix)
                self._inventory_classes(path, statement.orelse, prefix)

    @staticmethod
    def _parts(expression):
        parts = []
        while isinstance(expression, ast.Attribute):
            parts.append(expression.attr)
            expression = expression.value
        return [expression.id, *reversed(parts)] if isinstance(expression, ast.Name) else None

    def _class_ref(self, path, parts):
        if not parts:
            return None
        local = ".".join(parts)
        if (path, local) in self.class_index:
            return path, local
        expanded = self.imports[path].get(parts[0], parts[0])
        full = ".".join([expanded, *parts[1:]])
        for module, module_path in self.module_prefixes:
            if full.startswith(module + "."):
                owner = full[len(module) + 1:]
                if (module_path, owner) in self.class_index:
                    return module_path, owner
        return None

    def _builtin(self, path, owner, name):
        return name not in self.bound[path] and name not in self.class_bound.get((path, owner), ())

    def _enum(self, path, expression):
        parts = self._parts(expression)
        resolved = self._class_ref(path, parts[:-1]) if parts else None
        if resolved in self.enum_classes:
            cls = self.class_index[resolved]
            if any(isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == parts[-1] for t in s.targets) for s in cls.body):
                return EnumIdentity(*resolved, parts[-1])
        return UNKNOWN

    def references(self, path, owner, expression):
        if isinstance(expression, ast.Name):
            local = Node(path, owner, expression.id)
            module = Node(path, "", expression.id)
            return ({local} if owner and local in self.nodes else {module} if module in self.nodes else set())
        if isinstance(expression, ast.Attribute):
            result = {node for node in self.reverse.get((path, expression.lineno, expression.attr), ()) if node in self.nodes}
            parts = self._parts(expression)
            resolved = self._class_ref(path, parts[:-1]) if parts else None
            if resolved:
                candidate = Node(*resolved, parts[-1])
                if candidate in self.nodes:
                    result.add(candidate)
            return result
        return set()

    def _dependencies(self, path, owner, expression):
        dependencies, errors, sites = set(), set(), set()
        def walk(node):
            if isinstance(node, ast.Attribute):
                if self._enum(path, node) is not UNKNOWN:
                    return
                refs = self.references(path, owner, node)
                if refs:
                    dependencies.update(refs)
                    sites.update((ref, path, node.lineno, node.attr) for ref in refs)
                    return
                parts = self._parts(node)
                if parts and self._class_ref(path, parts[:-1]) in self.enum_classes:
                    return
                errors.add("unresolved attribute")
                return
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                refs = self.references(path, owner, node)
                if refs:
                    dependencies.update(refs)
                else:
                    errors.add("unresolved name")
                return
            if isinstance(node, ast.Call):
                if not (isinstance(node.func, ast.Name) and node.func.id in ("bool", "str", "int")
                        and self._builtin(path, owner, node.func.id) and len(node.args) == 1 and not node.keywords):
                    errors.add("unsupported call or mutation")
                for argument in [*node.args, *(kw.value for kw in node.keywords)]:
                    walk(argument)
                return
            supported = (ast.Constant, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd, ast.BoolOp, ast.And,
                         ast.Or, ast.Compare, ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.Lt, ast.LtE,
                         ast.Gt, ast.GtE, ast.IfExp, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Load)
            if not isinstance(node, supported):
                errors.add(f"unsupported expression {type(node).__name__}")
            for child in ast.iter_child_nodes(node):
                walk(child)
        walk(expression)
        return dependencies, errors, sites

    def _mark_bare(self, path, owner, expression, reason):
        for part in ast.walk(expression):
            if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Load):
                for reference in self.references(path, owner, part):
                    self.unsupported_bare[reference].add(reason)

    def _scan(self, path, owner, statements, guards):
        for statement in statements:
            if isinstance(statement, ast.ClassDef):
                for header in [*statement.bases, *statement.decorator_list, *(kw.value for kw in statement.keywords)]:
                    self._mark_bare(path, owner, header, "class definition header dependency unsupported")
                self._scan(path, owner + "." + statement.name if owner else statement.name, statement.body, guards)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for header in [statement.args, *statement.decorator_list, statement.returns]:
                    if header is not None:
                        self._mark_bare(path, owner, header, "function default/annotation dependency unsupported")
            elif isinstance(statement, ast.If):
                self._scan(path, owner, statement.body, (*guards, (owner, statement.test, True)))
                self._scan(path, owner, statement.orelse, (*guards, (owner, statement.test, False)))
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
                if isinstance(statement, ast.AnnAssign):
                    self._mark_bare(path, owner, statement.annotation, "annotation dependency unsupported")
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                if not all(isinstance(target, ast.Name) for target in targets):
                    self._mark_bare(path, owner, statement, "attribute/subscript/destructuring write unsupported")
                    continue
                dependencies, errors, sites = self._dependencies(path, owner, statement.value)
                for guard_owner, guard, _ in guards:
                    deps, errs, read_sites = self._dependencies(path, guard_owner, guard)
                    dependencies.update(deps); errors.update(errs); sites.update(read_sites)
                for target in targets:
                    destination = Node(path, owner, target.id)
                    self.nodes.add(destination)
                    self.records[destination].append(Assignment(statement.value, guards, dependencies, errors))
                    for dependency in dependencies:
                        self.edges[dependency].add(destination)
                for site in sites:
                    if errors:
                        self.bad_sites[site].update(errors)
                    else:
                        self.covered_sites.add(site)
            else:
                self._mark_bare(path, owner, statement, "bare dependency in unsupported statement/control flow")
            # Function/metaclass bodies, loops, try/with, destructuring and
            # attribute/subscript writes are deliberately not certified.

    def value(self, node, overrides, stack=()):
        if node in overrides:
            return overrides[node]
        if node in stack:
            return UNKNOWN
        selected = None
        for record in self.records.get(node, []):
            active = True
            for owner, guard, expected in record.guards:
                truth = _truth(self.expression(node.path, owner, guard, overrides, (*stack, node)))
                if truth is UNKNOWN:
                    return UNKNOWN
                active = active and truth == expected
            if active:
                selected = record
        if selected is None or selected.errors:
            return UNKNOWN
        return self.expression(node.path, node.owner, selected.expression, overrides, (*stack, node))

    def expression(self, path, owner, expression, overrides, stack=()):
        if isinstance(expression, ast.Constant):
            return expression.value if type(expression.value) in (type(None), bool, int, str) else UNKNOWN
        if isinstance(expression, (ast.Name, ast.Attribute)):
            enum = self._enum(path, expression)
            if enum is not UNKNOWN:
                return enum
            refs = self.references(path, owner, expression)
            if refs:
                values = [self.value(ref, overrides, stack) for ref in refs]
                return values[0] if all(_same(values[0], other) for other in values) else UNKNOWN
            return UNKNOWN
        if isinstance(expression, ast.UnaryOp):
            value = self.expression(path, owner, expression.operand, overrides, stack)
            if isinstance(expression.op, ast.Not):
                truth = _truth(value)
                return not truth if truth is not UNKNOWN else UNKNOWN
            if type(value) is int and isinstance(expression.op, (ast.USub, ast.UAdd)):
                return -value if isinstance(expression.op, ast.USub) else value
        if isinstance(expression, ast.IfExp):
            condition = _truth(self.expression(path, owner, expression.test, overrides, stack))
            return UNKNOWN if condition is UNKNOWN else self.expression(path, owner, expression.body if condition else expression.orelse, overrides, stack)
        if isinstance(expression, ast.BoolOp):
            for item in expression.values:
                value = self.expression(path, owner, item, overrides, stack)
                truth = _truth(value)
                if truth is UNKNOWN:
                    return UNKNOWN
                if isinstance(expression.op, ast.And) and not truth or isinstance(expression.op, ast.Or) and truth:
                    return value
            return value
        if isinstance(expression, ast.Compare) and len(expression.ops) == 1:
            left = self.expression(path, owner, expression.left, overrides, stack)
            right = self.expression(path, owner, expression.comparators[0], overrides, stack)
            if left is UNKNOWN or right is UNKNOWN:
                return UNKNOWN
            op = expression.ops[0]
            if isinstance(left, EnumIdentity) or isinstance(right, EnumIdentity):
                return True if _same(left, right) and isinstance(op, (ast.Eq, ast.Is)) else UNKNOWN
            if isinstance(op, (ast.Is, ast.IsNot)):
                if type(left) not in (type(None), bool) or type(right) not in (type(None), bool):
                    return UNKNOWN
                return (left is right) if isinstance(op, ast.Is) else (left is not right)
            try:
                operations = {ast.Eq: lambda: left == right, ast.NotEq: lambda: left != right,
                              ast.Lt: lambda: left < right, ast.LtE: lambda: left <= right,
                              ast.Gt: lambda: left > right, ast.GtE: lambda: left >= right}
                return operations[type(op)]() if type(op) in operations else UNKNOWN
            except TypeError:
                return UNKNOWN
        if isinstance(expression, ast.BinOp):
            left = self.expression(path, owner, expression.left, overrides, stack)
            right = self.expression(path, owner, expression.right, overrides, stack)
            if type(left) is int and type(right) is int and abs(left) < 10**9 and abs(right) < 10**9:
                if isinstance(expression.op, ast.Add): return left + right
                if isinstance(expression.op, ast.Sub): return left - right
                if isinstance(expression.op, ast.Mult): return left * right
            if type(left) is str and type(right) is str and isinstance(expression.op, ast.Add) and len(left) + len(right) < 10000:
                return left + right
        if isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name) and expression.func.id in ("bool", "str", "int") and self._builtin(path, owner, expression.func.id) and len(expression.args) == 1 and not expression.keywords:
            value = self.expression(path, owner, expression.args[0], overrides, stack)
            if value is not UNKNOWN and type(value) in (type(None), bool, int, str):
                try:
                    return {"bool": bool, "str": str, "int": int}[expression.func.id](value)
                except (ValueError, TypeError):
                    pass
        return UNKNOWN

    def blockers(self, node):
        reasons = set(self.unsupported_bare.get(node, ()))
        observed = self.metadata.get("sites", {}).get(node.path, {}).get(node.owner, {})
        for attribute in (node.name, "__dict__"):
            for site in observed.get(attribute, []):
                key = (node, site["path"], site["line"], attribute)
                if attribute == "__dict__" and _site_key_only(self.trees[site["path"]], site["line"], attribute):
                    continue
                if key in self.covered_sites and key not in self.bad_sites:
                    continue
                if self.bad_sites.get(key):
                    reasons.update(self.bad_sites[key])
                    continue
                tree = self.trees[site["path"]]
                containers = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno <= site["line"] <= n.end_lineno]
                reasons.add("function/metaclass cache read unsupported" if containers else "read not covered by direct assignment graph")
        return reasons

    def propagate(self, old, new):
        reached, unchanged, blockers = set(), set(), set()
        todo = list(old)
        while todo:
            node = todo.pop()
            if node in reached or node in unchanged:
                continue
            before, after = self.value(node, old), self.value(node, new)
            if _same(before, after):
                unchanged.add(node)
                continue
            reached.add(node)
            if before is UNKNOWN or after is UNKNOWN:
                blockers.add("unknown derived type/value")
            blockers.update(self.blockers(node))
            todo.extend(self.edges.get(node, ()))
        return reached, unchanged, blockers


def _read_json(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def evaluate(source, inputs, current_output, maps):
    metadata = _read_json(maps / "import-sites.json.gz")
    if not metadata.get("complete") or not metadata.get("collection_complete"):
        raise ValueError("complete pinned import-and-collection metadata required")
    graph = CacheGraph(source, metadata)
    tests = _read_json(inputs / "tests.json")["tests"]
    selector = Selector(tests, source, maps / "coverage.json.gz")
    if selector.source_commit != metadata["pinned_commit"]:
        raise ValueError("baseline map commits do not match")
    by_number = {test["number"]: test for test in tests}
    current = {item["commit"]: item["workers"] for item in map(json.loads, current_output.read_text().splitlines())}
    cases, predictions, reasons_count = [], [], Counter()
    for patch in sorted((inputs / "commits").glob("*.patch")):
        reasons, old_values, new_values, seeds = set(), {}, {}, []
        changes = parse_patch(patch.read_text())
        if len(changes) != 1:
            reasons.add("requires single-file class-value edit")
        else:
            change = changes[0]
            path = change.get("old_path")
            if not path or path != change.get("new_path") or not path.endswith(".py") or any(change.get(flag) for flag in ("malformed", "is_binary", "is_new", "is_deleted", "is_rename", "mode_changed")):
                reasons.add("unsupported file structure")
            else:
                try:
                    text, tree = selector.original(path)
                    new_tree = ast.parse(apply_patch_to_text(text, change), type_comments=True)
                    bindings = changed_bindings(tree, new_tree, set(change["old_lines"]), set(change["new_lines"]))
                    if not only_binding_values_changed(tree, new_tree, bindings): reasons.add("not isolated class binding values")
                    if generated_classes(tree): reasons.add("generated class module")
                    if selector.files.get(path, {}).get("global_read"): reasons.add("global raw-source reader")
                    for (owner, name), (before, after) in bindings.items():
                        if len(before) != 1 or len(after) != 1 or name.startswith("__"):
                            reasons.add("unsupported repeated/special binding"); continue
                        node = Node(path, owner, name)
                        if node not in graph.records:
                            reasons.add("binding outside supported initialization graph")
                        old = graph.expression(path, owner, before[0].value, {})
                        new = graph.expression(path, owner, after[0].value, {})
                        seeds.append({"node": node.label(), "old": _encoded(old), "new": _encoded(new)})
                        old_values[node], new_values[node] = old, new
                        if old is UNKNOWN or new is UNKNOWN: reasons.add("unsupported changed type/value")
                    if not bindings: reasons.add("no changed class binding")
                except (OSError, SyntaxError, ValueError) as error:
                    reasons.add("patch/source analysis failed: " + type(error).__name__)
        reached, unchanged, blockers = graph.propagate(old_values, new_values)
        reasons.update(blockers)
        consumers = set()
        for node in reached:
            if node.owner and node.path.startswith("sqlglot/"):
                owners = [node.owner]
                parts = node.owner.split(".")
                owners += [".".join(parts[:index]) for index in range(1, len(parts))]
                users = selector.class_users(node.path, owners)
                if users is None: reasons.add("missing qualified class users")
                else: consumers.update(users)
            else:
                consumers.update(selector.file_tests(node.path) | selector.import_users(node.path))
            consumers.update(selector.readers(node.path))
        workers = current[patch.stem]
        current_numbers = {number for worker in workers for number in worker}
        if old_values:
            for node in old_values: consumers.update(selector.readers(node.path))
        candidate = consumers | selector.unmapped_numbers
        proposed = workers if reasons else schedule([by_number[number] for number in sorted(candidate)])
        reasons_count.update(reasons)
        cases.append({"commit": patch.stem, "status": "fallback_current" if reasons else "experimental_graph_candidate",
                      "reasons": sorted(reasons), "changed_values": seeds,
                      "reached_bindings": sorted(node.label() for node in reached),
                      "unchanged_derived_bindings": sorted(node.label() for node in unchanged),
                      "graph_consumer_count": len(consumers), "current_selected_count": len(current_numbers),
                      "experimental_selected_count": sum(map(len, proposed)),
                      "current_cost_ms": cost_ms(workers, tests), "experimental_cost_ms": cost_ms(proposed, tests),
                      "selected_tests_preserved": {number for worker in proposed for number in worker} == current_numbers,
                      "schedule_preserved": proposed == workers,
                      "graph_only_count": len(consumers - current_numbers), "current_only_count": len(current_numbers - consumers)})
        predictions.append({"commit": patch.stem, "workers": proposed})
    return {"scope": "offline experimental graph feasibility; no labels read; not exhaustive dynamic dependency proof",
            "evaluation_labels_read": False,
            "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "current_output_sha256": hashlib.sha256(current_output.read_bytes()).hexdigest(),
            "input_files_sha256": {str(path.relative_to(inputs)): hashlib.sha256(path.read_bytes()).hexdigest()
                                    for path in [inputs / "tests.json", *sorted((inputs / "commits").glob("*.patch"))]},
            "pinned_commit": metadata["pinned_commit"], "input_count": len(cases),
            "graph": {"assignment_nodes": len(graph.records), "dependency_edges": sum(map(len, graph.edges.values())),
                      "measured_qualified_read_sites": len(graph.measured_sites),
                      "covered_qualified_read_sites": len(graph.covered_sites & graph.measured_sites),
                      "unsupported_assignment_read_sites": len(set(graph.bad_sites) & graph.measured_sites)},
            "graph_candidate_count": sum(not case["reasons"] for case in cases),
            "fallback_count": sum(bool(case["reasons"]) for case in cases),
            "fallback_reasons": dict(reasons_count),
            "current_total_seconds": sum(case["current_cost_ms"] for case in cases) / 1000,
            "experimental_total_seconds": sum(case["experimental_cost_ms"] for case in cases) / 1000,
            "selected_test_sets_preserved": all(case["selected_tests_preserved"] for case in cases),
            "schedules_preserved": all(case["schedule_preserved"] for case in cases),
            "cases": cases}, predictions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--current-output", type=Path, required=True)
    parser.add_argument("--maps", type=Path, default=ROOT / "maps")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    args = parser.parse_args()
    report, predictions = evaluate(args.source.resolve(), args.inputs.resolve(), args.current_output.resolve(), args.maps.resolve())
    prediction_text = "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in predictions)
    report["predictions_sha256"] = hashlib.sha256(prediction_text.encode()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    args.predictions.write_text(prediction_text)
    print(json.dumps({key: report[key] for key in ("input_count", "graph_candidate_count", "fallback_count", "fallback_reasons", "current_total_seconds", "experimental_total_seconds")}))


if __name__ == "__main__":
    main()
