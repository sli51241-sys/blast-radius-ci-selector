#!/usr/bin/env python3
"""Build allowed baseline coverage/fixture/import maps, never patch answer maps.

Usage (from any directory, in the supplied pinned-dependency environment):
    python tools/build_coverage.py --source ../sqlglot --output maps/coverage.json.gz

All tests run with SKIP_INTEGRATION=0.  Optional pytest arguments follow ``--``;
for example ``-- tests/test_time.py`` is a short instrumentation smoke check.

Compact output schema, version 1:
  tests: ordered pytest nodeids; every integer index below addresses this array.
  files[path].lines: {decimal_line: [test_index, ...]} for executed baseline lines.
  files[path].global: lines executed during import/collection, retained separately.
  files[path].reads: tests that opened this file for reading, including fixtures.
  files[path].global_read: an import/collection read occurred outside a test.
  files[path].read_owners: baseline Python files on read callers' stacks.
  imports: module/class-scope runtime import edges (excludes TYPE_CHECKING).
  runtime_imports: {owner_path: {function_import_target: [import_lineno, ...]}}.
  outcomes: pytest setup/call/teardown outcomes, useful to audit baseline health.

Collection lines are also assigned to the tests in the collecting module.
Shared pytest fixture setup is assigned to every collected consumer.  Global
lines and static imports let a selector conservatively cover modules imported
before test execution.  Read maps show opens, not every use of a cached value.
Process-pool tasks run synchronously with pickle round trips during this
offline instrumentation so their execution stays attributed to the test.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _scope_typing_aliases(nodes, modules=(), flags=(), arguments=()):
    """Recognize known typing aliases, conservatively ignoring rebound names."""
    modules, flags = set(modules), set(flags)
    rebound = set(arguments)
    imports = []

    def bindings(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            rebound.add(node.name)
            return
        if isinstance(node, ast.Lambda):
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            rebound.add(node.id)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".", 1)[0]
                typing_module = isinstance(node, ast.Import) and alias.name in ("typing", "typing_extensions")
                typing_flag = isinstance(node, ast.ImportFrom) and node.module in ("typing", "typing_extensions") and alias.name == "TYPE_CHECKING"
                if not typing_module and not typing_flag:
                    rebound.add(name)
        for child in ast.iter_child_nodes(node):
            bindings(child)

    for node in nodes:
        bindings(node)
        # Only unconditional imports establish a known alias in this scope.
        if isinstance(node, ast.Import):
            imports.extend(("module", alias.asname or alias.name) for alias in node.names if alias.name in ("typing", "typing_extensions"))
        elif isinstance(node, ast.ImportFrom) and node.module in ("typing", "typing_extensions"):
            imports.extend(("flag", alias.asname or alias.name) for alias in node.names if alias.name == "TYPE_CHECKING")
    modules.difference_update(rebound)
    flags.difference_update(rebound)
    for kind, name in imports:
        if name not in rebound:
            (modules if kind == "module" else flags).add(name)
    return modules, flags


def _runtime_import_nodes(tree):
    """Yield imports with function depth, omitting proven typing-only branches."""
    found = []

    class Visitor(ast.NodeVisitor):
        depth = 0

        def __init__(self):
            self.modules, self.flags = _scope_typing_aliases(tree.body)

        def guard(self, node):
            if isinstance(node, ast.Name) and node.id in self.flags:
                return False
            if isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING" and isinstance(node.value, ast.Name) and node.value.id in self.modules:
                return False
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                value = self.guard(node.operand)
                return None if value is None else not value
            if isinstance(node, ast.BoolOp):
                values = [self.guard(value) for value in node.values]
                if isinstance(node.op, ast.And):
                    return False if False in values else True if all(value is True for value in values) else None
                return True if True in values else False if all(value is False for value in values) else None
            return None

        def visit_If(self, node):
            value = self.guard(node.test)
            if value is not False:
                for child in node.body:
                    self.visit(child)
            if value is not True:
                for child in node.orelse:
                    self.visit(child)

        def visit_Import(self, node):
            found.append((node, self.depth))

        visit_ImportFrom = visit_Import

        def visit_FunctionDef(self, node):
            old_modules, old_flags = self.modules, self.flags
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            if node.args.vararg:
                args.append(node.args.vararg)
            if node.args.kwarg:
                args.append(node.args.kwarg)
            self.modules, self.flags = _scope_typing_aliases(node.body, old_modules, old_flags, (arg.arg for arg in args))
            self.depth += 1
            for child in node.body:
                self.visit(child)
            self.depth -= 1
            self.modules, self.flags = old_modules, old_flags

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            old_modules, old_flags = self.modules, self.flags
            self.modules, self.flags = _scope_typing_aliases(node.body, old_modules, old_flags)
            for child in node.body:
                self.visit(child)
            self.modules, self.flags = old_modules, old_flags

    Visitor().visit(tree)
    return found


def import_graphs(source: Path) -> tuple[dict[str, list[str]], dict[str, dict[str, list[int]]]]:
    """Separate import-time dependencies from covered function-local imports."""
    paths = sorted(path for path in source.rglob("*.py") if ".git" not in path.parts and "__pycache__" not in path.parts)
    modules = {}
    for path in paths:
        relative = path.relative_to(source).as_posix()
        module = relative[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        modules[module] = relative

    graph, runtime_graph = {}, {}
    for path in paths:
        relative = path.relative_to(source).as_posix()
        module = relative[:-3].replace("/", ".")
        package = module.removesuffix(".__init__") if path.name == "__init__.py" else module.rpartition(".")[0]
        try:
            tree = ast.parse(path.read_bytes(), filename=relative)
        except (OSError, SyntaxError, UnicodeError):
            continue
        edges = set()
        runtime_edges = {}
        for node, depth in _runtime_import_nodes(tree):
            names = set()
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = package.split(".") if package else []
                    if node.level - 1 > len(parts):
                        continue
                    base = ".".join(parts[: len(parts) - node.level + 1])
                    name = ".".join(filter(None, (base, node.module)))
                else:
                    name = node.module or ""
                if name:
                    names.add(name)
                    names.update(name + "." + alias.name for alias in node.names if alias.name != "*")
            for name in names:
                parts = name.split(".")
                for length in range(1, len(parts) + 1):
                    candidate = modules.get(".".join(parts[:length]))
                    if candidate and candidate != relative:
                        if depth:
                            runtime_edges.setdefault(candidate, set()).add(node.lineno)
                        else:
                            edges.add(candidate)
        graph[relative] = sorted(edges)
        if runtime_edges:
            runtime_graph[relative] = {target: sorted(lines) for target, lines in sorted(runtime_edges.items())}
    return graph, runtime_graph


def import_graph(source: Path) -> dict[str, list[str]]:
    """Backward-compatible access to import-time dependencies only."""
    return import_graphs(source)[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--native-process-pools", action="store_true", help="Disable offline inline-process instrumentation (worker coverage will be absent)")
    parser.add_argument("--class-usage", action="store_true", help="Observe qualified runtime class/attribute users with baseline-only AST instrumentation")
    parser.add_argument("--test-timeout", type=int, default=90, help="Per-test instrumentation timeout in seconds")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    source = options.source.resolve()
    output = options.output.resolve()
    if not (source / "sqlglot").is_dir() or not (source / "tests").is_dir():
        parser.error("--source must be the baseline sqlglot checkout containing sqlglot/ and tests/")
    output.parent.mkdir(parents=True, exist_ok=True)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTHONPATH"] = str(source) + os.pathsep + os.environ.get("PYTHONPATH", "")
    # Some baseline tests intentionally spawn ``python`` by name.  Invoking
    # this builder through an absolute venv interpreter need not activate it.
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", os.defpath)
    # Independent of whichever pytest plugins happen to be in a developer's
    # environment. Timeout is loaded explicitly to match the labeled setup.
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    if options.class_usage:
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))
    os.chdir(source)

    import coverage

    cov = coverage.Coverage(source=[str(source)], data_file=None, config_file=False, context=None, concurrency=["thread"])
    cov.start()
    from coverage_plugin import CoveragePlugin, GLOBAL_CONTEXT, inline_process_pools

    plugin = CoveragePlugin(cov, source, class_usage=options.class_usage)
    plugin.switch(GLOBAL_CONTEXT)
    if not options.native_process_pools:
        inline_process_pools()
    import pytest

    arguments = options.pytest_args
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    if not arguments:
        arguments = ["tests"]
    start = time.monotonic()
    exit_code = int(pytest.main(["-q", "-p", "pytest_timeout", f"--timeout={options.test_timeout}", "-o", "addopts=", *arguments], plugins=[plugin]))
    elapsed = time.monotonic() - start
    cov.stop()
    data = cov.get_data()
    indices = {node: index for index, node in enumerate(plugin.tests)}
    context_tests = {context: plugin.consumers(context, indices) for context in data.measured_contexts()}
    files = {}
    for filename in sorted(data.measured_files()):
        relative = plugin.relative(filename)
        if not relative:
            continue
        lines = {}
        global_lines = []
        for line, contexts in data.contexts_by_lineno(filename).items():
            test_set = set()
            global_line = False
            for context in contexts:
                test_set.update(context_tests.get(context, ()))
                if context in ("", GLOBAL_CONTEXT) or context.startswith("@collect:"):
                    global_line = True
            if test_set:
                lines[str(line)] = sorted(test_set)
            if global_line:
                global_lines.append(line)
        files[relative] = {"lines": lines, "global": sorted(global_lines)}
    for path, contexts in plugin.read_contexts.items():
        entry = files.setdefault(path, {"lines": {}, "global": []})
        readers = set()
        for context in contexts:
            readers.update(plugin.consumers(context, indices))
        entry["reads"] = sorted(readers)
        entry["global_read"] = any(context in ("", GLOBAL_CONTEXT) or context.startswith("@collect:") for context in contexts)
        entry["read_owners"] = sorted(plugin.read_owners[path])
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        commit = None
    imports, runtime_imports = import_graphs(source)
    result = {
        "version": 1,
        "commit": commit,
        "tests": plugin.tests,
        "files": files,
        "imports": imports,
        "runtime_imports": runtime_imports,
        "graph_generation": {"version": 2, "typing_guards": "runtime branches only", "function_imports": "coverage gated"},
        "outcomes": dict(plugin.outcomes),
        "exit_code": exit_code,
        "elapsed_seconds": round(elapsed, 3),
        "environment": {"python": sys.version, "pytest": pytest.__version__, "coverage": coverage.__version__, "SKIP_INTEGRATION": "0"},
        "instrumentation": {"process_pool": "native-untraced" if options.native_process_pools else "inline-pickle-roundtrip"},
    }
    if plugin.class_usage:
        result.update(plugin.class_usage.export(indices))
        result["instrumentation"]["class_usage"] = {
            "version": 1,
            "strategy": "ast-method-receivers-and-qualified-attribute-reads",
            "mro_inclusive": True,
            "source_files_unchanged": True,
            "modules_instrumented": len(plugin.class_usage.loaded_modules),
            "attribute_names_observed": len(plugin.class_usage.attribute_names),
        }
    raw = json.dumps(result, separators=(",", ":"), ensure_ascii=True).encode()
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("wb") as stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0, compresslevel=9) as archive:
            archive.write(raw)
    temporary.replace(output)
    print(f"Coverage map: {len(plugin.tests)} tests, {len(files)} files, {len(raw):,} JSON bytes, {output.stat().st_size:,} compressed bytes, pytest exit {exit_code}", file=sys.stderr)
    print(f"Wrote {output}", file=sys.stderr)
    # A baseline failure is not hidden: keep its map and return pytest's status.
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
