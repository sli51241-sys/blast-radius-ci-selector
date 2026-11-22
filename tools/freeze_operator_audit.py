#!/usr/bin/env python3
"""Freeze an independent, label-free audit of function comparison operators."""
from __future__ import annotations

import argparse
import ast
import copy
import difflib
import hashlib
import io
import json
from pathlib import Path
import random
import shutil
import subprocess
import tokenize

try:
    from .mutation_eval import PIN, ROOT, public_paths
except ImportError:
    from mutation_eval import PIN, ROOT, public_paths

SEED = "phase2-audit-v2-20261121"
OPERATORS = {ast.Gt: ">=", ast.GtE: ">", ast.Lt: "<=", ast.LtE: "<",
             ast.Eq: "!=", ast.NotEq: "==", ast.Is: "==", ast.IsNot: "!="}
TOKENS = {ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<=",
          ast.Eq: "==", ast.NotEq: "!=", ast.Is: "is", ast.IsNot: "is not"}
NEW_AST = {">=": ast.GtE, ">": ast.Gt, "<=": ast.LtE, "<": ast.Lt,
           "!=": ast.NotEq, "==": ast.Eq}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def offset(lines, line, byte_column):
    """Translate AST UTF-8 columns into Python string offsets."""
    return sum(len(value) for value in lines[:line - 1]) + len(lines[line - 1].encode()[:byte_column].decode())


def operator_span(text, node):
    lines = text.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    left = offset(lines, node.left.end_lineno, node.left.end_col_offset)
    right = offset(lines, node.comparators[0].lineno, node.comparators[0].col_offset)
    positions = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        start = starts[token.start[0] - 1] + token.start[1]
        end = starts[token.end[0] - 1] + token.end[1]
        if start >= left and end <= right and token.type in (tokenize.NAME, tokenize.OP):
            positions.append((token.string, start, end))
    wanted = TOKENS[type(node.ops[0])].split()
    matches = [positions[i:i + len(wanted)] for i in range(len(positions))
               if [part[0] for part in positions[i:i + len(wanted)]] == wanted]
    if len(matches) != 1:
        raise ValueError("comparison operator token could not be located uniquely")
    return matches[0][0][1], matches[0][-1][2]


def function_compares(tree):
    result = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.scopes = []

        def visit_ClassDef(self, node):
            self.scopes.append(("class", node.name))
            for statement in node.body:
                self.visit(statement)
            self.scopes.pop()

        def visit_FunctionDef(self, node):
            self.scopes.append(("function", node.name))
            for statement in node.body:
                self.visit(statement)
            self.scopes.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node):
            pass

        def visit_Compare(self, node):
            if (self.scopes and self.scopes[-1][0] == "function"
                    and len(node.ops) == 1 and type(node.ops[0]) in OPERATORS):
                result.append((".".join(name for _, name in self.scopes), node))
            self.generic_visit(node)

    Visitor().visit(tree)
    return result


def mutate(text, tree, comparison):
    start, end = operator_span(text, comparison)
    updated = OPERATORS[type(comparison.ops[0])]
    modified = text[:start] + updated + text[end:]
    changed = copy.deepcopy(tree)
    matches = [node for node in ast.walk(changed) if isinstance(node, ast.Compare)
               and (node.lineno, node.col_offset) == (comparison.lineno, comparison.col_offset)]
    if len(matches) != 1:
        raise ValueError("comparison AST position was not unique")
    matches[0].ops = [NEW_AST[updated]()]
    parsed = ast.parse(modified, type_comments=True)
    compile(parsed, "<operator-audit>", "exec", dont_inherit=True)
    if ast.dump(changed, include_attributes=False) != ast.dump(parsed, include_attributes=False):
        raise ValueError("mutation changed more than the one comparison operator")
    # Use a unique source-context anchor so the existing isolated-copy runner
    # can apply the frozen edit without changing its API.
    lines = text.splitlines(keepends=True)
    begin_line = text.count("\n", 0, start)
    end_line = text.count("\n", 0, end)
    radius = 2
    while True:
        begin = sum(len(value) for value in lines[:max(0, begin_line - radius)])
        stop = sum(len(value) for value in lines[:min(len(lines), end_line + radius + 1)])
        before = text[begin:stop]
        if text.count(before) == 1:
            break
        radius *= 2
    after = modified[begin:stop + len(updated) - (end - start)]
    if text.replace(before, after, 1) != modified:
        raise ValueError("context replacement was not the operator-only mutation")
    return modified, before, after


def freeze(source, corpus, v1, catalog, sample_commits):
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if commit != PIN:
        raise ValueError("not the pinned source commit")
    v1_raw = (v1 / "manifest.json").read_bytes()
    if digest(v1_raw) != (v1 / "manifest.sha256").read_text().strip():
        raise ValueError("v1 frozen manifest checksum differs")
    previous = json.loads(v1_raw)
    excluded = public_paths(sample_commits) | {case["path"] for case in previous["cases"]}
    candidates = []
    for path in sorted((source / "sqlglot").rglob("*.py")):
        relative = path.relative_to(source).as_posix()
        if relative in excluded:
            continue
        text = path.read_text()
        tree = ast.parse(text, type_comments=True)
        for function, node in function_compares(tree):
            candidates.append((relative, node.lineno, node.col_offset, function, node, text, tree))
    candidates.sort(key=lambda item: item[:4])
    random.Random(SEED).shuffle(candidates)
    selected, paths = [], set()
    for item in candidates:
        if item[0] not in paths:
            selected.append(item)
            paths.add(item[0])
            if len(selected) == 6:
                break
    if len(selected) != 6:
        raise ValueError("not enough distinct source paths")
    cases, patches = [], {}
    for index, (relative, _line, _column, function, node, text, tree) in enumerate(selected, 1):
        modified, before, after = mutate(text, tree, node)
        case_id = f"audit-compare-{index:02d}"
        patch = "".join(difflib.unified_diff(text.splitlines(keepends=True), modified.splitlines(keepends=True), fromfile="a/" + relative, tofile="b/" + relative))
        patches[case_id] = patch
        cases.append({"id": case_id, "category": "operator_audit", "split": "heldout", "path": relative,
                      "function": function, "lineno": node.lineno, "col_offset": node.col_offset,
                      "operator_before": TOKENS[type(node.ops[0])], "operator_after": OPERATORS[type(node.ops[0])],
                      "before": before, "after": after, "original_sha256": digest(text.encode()),
                      "modified_sha256": digest(modified.encode()), "patch_sha256": digest(patch.encode()),
                      "equivalent_ast": False, "operator_only_ast_proof": True})
    manifest = {"version": 1, "source_commit": commit, "catalog_sha256": digest(catalog.read_bytes()),
                "seed": SEED, "sampling": "Seeded shuffle of all eligible single-operator Compare ASTs; first six distinct source paths, no outcomes consulted.",
                "eligible_candidates": len(candidates), "v1_manifest_sha256": digest(v1_raw),
                "excluded_paths": sorted(excluded), "generator_uses_sample_answers": False, "cases": cases}
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    target = corpus / "manifest.json"
    if target.exists() and target.read_text() != encoded:
        raise ValueError("audit corpus is already frozen differently")
    (corpus / "patches").mkdir(parents=True, exist_ok=True)
    (corpus / "input/commits").mkdir(parents=True, exist_ok=True)
    target.write_text(encoded)
    (corpus / "manifest.sha256").write_text(digest(encoded.encode()) + "\n")
    for case_id, patch in patches.items():
        (corpus / "patches" / f"{case_id}.patch").write_text(patch)
        (corpus / "input/commits" / f"{case_id}.patch").write_text(patch)
    shutil.copyfile(catalog, corpus / "tests.json")
    shutil.copyfile(catalog, corpus / "input/tests.json")
    print(json.dumps({"manifest_sha256": digest(encoded.encode()), "cases": len(cases),
                      "locations": [{key: case[key] for key in ("id", "path", "function", "lineno", "operator_before", "operator_after")} for case in cases]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT.parent / "sqlglot")
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--v1", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=ROOT / "sample/tests.json")
    parser.add_argument("--sample-commits", type=Path, default=ROOT / "sample/commits")
    args = parser.parse_args()
    freeze(args.source.resolve(), args.corpus.resolve(), args.v1.resolve(), args.catalog.resolve(), args.sample_commits.resolve())
