#!/usr/bin/env python3
"""Index baseline attribute reads without importing or changing the project.

Usage:
    python tools/build_symbols.py --source ../sqlglot --output maps/symbols.json.gz

Version 1 schema:
  attributes[name][path] -> sorted attribute-read line numbers.
  dynamic_attributes[path] -> getattr/hasattr sites with unknown names.
  class_assignments[name][path] -> class-body assignment line numbers.
  module_assignments[name][path] -> module-body assignment line numbers.

The map overapproximates dependencies by attribute name. It does not attempt to
infer which object a read will target. It reads only the original Python source,
never patches, test outcomes, or labeled sample answers.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


def _nested_index():
    return defaultdict(lambda: defaultdict(set))


def _targets(node: ast.AST):
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            yield from _targets(element)
    elif isinstance(node, ast.Starred):
        yield from _targets(node.value)


class SymbolVisitor(ast.NodeVisitor):
    def __init__(self, path: str, attributes, dynamic, class_assignments, module_assignments):
        self.path = path
        self.attributes = attributes
        self.dynamic = dynamic
        self.class_assignments = class_assignments
        self.module_assignments = module_assignments
        self.scopes: list[str] = []
        self.lookup_names = {"getattr", "hasattr"}
        self.builtin_modules = {"builtins", "__builtin__"}

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name in {"builtins", "__builtin__"}:
                self.builtin_modules.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module in {"builtins", "__builtin__"}:
            for alias in node.names:
                if alias.name in {"getattr", "hasattr"}:
                    self.lookup_names.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load):
            self.attributes[node.attr][self.path].add(node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node):
        function = node.func
        lookup = isinstance(function, ast.Name) and function.id in self.lookup_names
        lookup = lookup or (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id in self.builtin_modules
            and function.attr in {"getattr", "hasattr"}
        )
        if lookup:
            name = node.args[1] if len(node.args) > 1 else None
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                self.attributes[name.value][self.path].add(node.lineno)
            else:
                self.dynamic[self.path].add(node.lineno)
        self.generic_visit(node)

    def _scope(self, node, kind):
        self.scopes.append(kind)
        self.generic_visit(node)
        self.scopes.pop()

    def visit_ClassDef(self, node):
        self._scope(node, "class")

    def visit_FunctionDef(self, node):
        self._scope(node, "function")

    def visit_AsyncFunctionDef(self, node):
        self._scope(node, "function")

    def visit_Lambda(self, node):
        self._scope(node, "function")

    def _assignment(self, node, targets):
        if not self.scopes:
            index = self.module_assignments
        elif self.scopes[-1] == "class":
            index = self.class_assignments
        else:
            index = None
        if index is not None:
            for target in targets:
                for name in _targets(target):
                    index[name][self.path].add(node.lineno)
        self.generic_visit(node)

    def visit_Assign(self, node):
        self._assignment(node, node.targets)

    def visit_AnnAssign(self, node):
        self._assignment(node, [node.target])

    def visit_AugAssign(self, node):
        # Augmented attribute assignment both reads and writes its target.
        if isinstance(node.target, ast.Attribute):
            self.attributes[node.target.attr][self.path].add(node.target.lineno)
        self._assignment(node, [node.target])


def _plain_nested(index):
    return {
        name: {path: sorted(lines) for path, lines in sorted(paths.items())}
        for name, paths in sorted(index.items())
    }


def build_symbols(source: Path) -> dict:
    """Return an index of all readable Python source under ``source``."""
    source = source.resolve()
    attributes = _nested_index()
    dynamic = defaultdict(set)
    class_assignments = _nested_index()
    module_assignments = _nested_index()
    failures = {}
    file_count = 0
    for path in sorted(source.rglob("*.py")):
        relative_path = path.relative_to(source)
        if any(part in {".git", "__pycache__", ".venv", "venv", "node_modules"} for part in relative_path.parts):
            continue
        relative = relative_path.as_posix()
        file_count += 1
        try:
            tree = ast.parse(path.read_bytes(), filename=relative)
        except (OSError, SyntaxError, UnicodeError) as error:
            failures[relative] = f"{type(error).__name__}: {error}"
            continue
        visitor = SymbolVisitor(relative, attributes, dynamic, class_assignments, module_assignments)
        # Function bodies can reference aliases imported later in the module.
        # Collect aliases before visiting calls; shadowing may overapproximate
        # reads, which is appropriate for this conservative dependency index.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in {"builtins", "__builtin__"}:
                        visitor.builtin_modules.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module in {"builtins", "__builtin__"}:
                for alias in node.names:
                    if alias.name in {"getattr", "hasattr"}:
                        visitor.lookup_names.add(alias.asname or alias.name)
        visitor.visit(tree)
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.SubprocessError):
        commit = None
    return {
        "version": 1,
        "commit": commit,
        "file_count": file_count,
        "attributes": _plain_nested(attributes),
        "dynamic_attributes": {path: sorted(lines) for path, lines in sorted(dynamic.items())},
        "class_assignments": _plain_nested(class_assignments),
        "module_assignments": _plain_nested(module_assignments),
        "unparsed": failures,
    }


def write_symbols(data: dict, output: Path) -> tuple[int, int]:
    """Write reproducible gzip bytes: no filename, timestamps, or absolute paths."""
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("wb") as stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0, compresslevel=9) as archive:
            archive.write(raw)
    temporary.replace(output)
    return len(raw), output.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    options = parser.parse_args()
    source = options.source.resolve()
    if not source.is_dir():
        parser.error("--source must be a directory containing the pinned baseline")
    data = build_symbols(source)
    raw_size, compressed_size = write_symbols(data, options.output.resolve())
    print(
        f"Symbols: {data['file_count']} files, {len(data['attributes'])} attributes, "
        f"{sum(len(lines) for lines in data['dynamic_attributes'].values())} dynamic sites, "
        f"{len(data['unparsed'])} unparsed files, {raw_size:,} JSON bytes, "
        f"{compressed_size:,} compressed bytes",
        file=sys.stderr,
    )
    return 1 if data["unparsed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
