"""Conservative import-projection proof for an existing literal class dict.

Callers must still enforce their unchanged-rest-of-AST,
existing-binding, generated-class, coverage, and import-success guards.
This helper does not predict tests or read patches/failure labels.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path


PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def _bare_class_load(tree: ast.AST, attribute: str) -> bool:
    """Class-body names can create cached fields without an attribute read."""
    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.found = False

        def visit_FunctionDef(self, node):
            # Definition headers execute while constructing the class. Method
            # bodies do not. Nested classes inside methods are not import data.
            for part in [*node.decorator_list, node.args, node.returns]:
                if part is not None:
                    self.visit(part)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Load) and node.id == attribute:
                self.found = True

    # Start separately at every class so a local class created by an imported
    # factory is checked too, while ordinary method bodies remain excluded.
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            visitor = Visitor()
            for statement in node.body:
                visitor.visit(statement)
            if visitor.found:
                return True
    return False


def _parents(tree: ast.AST) -> dict:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _membership(expression: ast.AST, parents: dict) -> bool:
    parent = parents.get(expression)
    return (isinstance(parent, ast.Compare) and len(parent.ops) == 1
            and isinstance(parent.ops[0], (ast.In, ast.NotIn))
            and parent.comparators[0] is expression)


def _keys_membership(expression: ast.AST, parents: dict) -> bool:
    if _membership(expression, parents):
        return True
    method = parents.get(expression)
    call = parents.get(method)
    return (isinstance(method, ast.Attribute) and method.value is expression
            and method.attr == "keys" and isinstance(call, ast.Call)
            and call.func is method and not call.args and not call.keywords
            and _membership(call, parents))


def _safe_read(expression: ast.AST, tree: ast.AST, parents: dict) -> bool:
    if _keys_membership(expression, parents):
        return True
    statement = parents.get(expression)
    if not (isinstance(statement, ast.Assign) and statement.value is expression
            and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name)):
        return False
    function = parents.get(statement)
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False  # A module/class alias may escape after import.
    alias = statement.targets[0].id
    stores, loads = [], []
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and node.id == alias:
            if isinstance(node.ctx, ast.Load):
                loads.append(node)
            else:
                stores.append(node)
    if len(stores) != 1 or stores[0] is not statement.targets[0] or not loads:
        return False
    # A same-named argument, global/nonlocal, or nested scope makes this alias
    # proof ambiguous. Decline instead of attempting Python name resolution.
    if any(isinstance(node, ast.arg) and node.arg == alias
           or isinstance(node, (ast.Global, ast.Nonlocal)) and alias in node.names
           or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == alias
           or isinstance(node, ast.ExceptHandler) and node.name == alias
           or isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == alias
           or isinstance(node, ast.MatchMapping) and node.rest == alias
           or isinstance(node, ast.alias) and (node.asname or node.name.split(".")[0]) == alias
           for node in ast.walk(function)):
        return False
    for load in loads:
        cursor = parents.get(load)
        while cursor is not None and cursor is not function:
            if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                return False
            cursor = parents.get(cursor)
        if load.lineno < statement.lineno or not _keys_membership(load, parents):
            return False
    return True


def _site_key_only(tree: ast.Module, line: int, attribute: str) -> bool:
    parents = _parents(tree)
    candidates = []
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) != line:
            continue
        if isinstance(node, ast.Attribute) and node.attr == attribute:
            candidates.append(node)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "vars" and attribute == "__dict__" and len(node.args) == 1 and not node.keywords:
                candidates.append(node)
            elif node.func.id in ("getattr", "hasattr"):
                if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
                    return False  # Cannot identify which runtime read this is.
                if node.args[1].value != attribute:
                    continue
                if node.func.id == "hasattr":
                    # Only presence is observed, and this edit preserves keys.
                    candidates.append((node, "presence"))
                else:
                    candidates.append(node)
    return bool(candidates) and all(
        isinstance(node, tuple) or _safe_read(node, tree, parents) for node in candidates
    )


def dict_values_import_safe(
    *, source: Path, path: str, owner: str, attribute: str,
    old_value: ast.AST, new_value: ast.AST, old_tree: ast.Module,
    metadata: dict | None, changed_paths: set[str], expected_commit: str = PINNED_COMMIT,
) -> bool:
    """Allow unchanged-key literal dict edits only after proving every read site.

    This proof handles a single changed source file. Read sites may
    perform membership directly, call .keys() for membership, or assign one
    function-local alias used solely that way. Value reads and escapes fail.
    Dictionary inspection receives the same strict proof even for other keys.
    """
    if (not metadata or metadata.get("version") != 1 or metadata.get("complete") is not True
            or metadata.get("collection_complete") is not True
            or metadata.get("pinned_commit") != expected_commit or changed_paths != {path}):
        return False
    if not isinstance(old_value, ast.Dict) or not isinstance(new_value, ast.Dict):
        return False
    try:
        old, new = ast.literal_eval(old_value), ast.literal_eval(new_value)
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return False
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    # Preserve key types and insertion order as well as equality. AST equality
    # deliberately rejects some equivalent spelling changes.
    if ([ast.dump(key, include_attributes=False) for key in old_value.keys]
            != [ast.dump(key, include_attributes=False) for key in new_value.keys]):
        return False
    if _bare_class_load(old_tree, attribute):
        return False
    owners = metadata.get("sites", {}).get(path, {})
    if owner not in owners:
        return False
    observed = owners[owner]
    # Require at least one measured field read; this path specifically relaxes
    # the import-read guard, so an empty/missing entry gives no proof.
    if not observed.get(attribute):
        return False
    checks = [(attribute, site) for site in observed[attribute]]
    checks += [("__dict__", site) for site in observed.get("__dict__", [])]
    source = source.resolve()
    parsed = {}
    try:
        original = (source / path).resolve()
        if (not original.is_relative_to(source)
                or hashlib.sha256(original.read_bytes()).hexdigest()
                != metadata.get("source_sha256", {}).get(path)):
            return False
    except OSError:
        return False
    for field, site in checks:
        relative, line = site.get("path"), site.get("line")
        if not isinstance(relative, str) or not isinstance(line, int) or isinstance(line, bool) or line < 1:
            return False
        if relative not in parsed:
            target = (source / relative).resolve()
            if not target.is_relative_to(source):
                return False
            try:
                content = target.read_bytes()
                if hashlib.sha256(content).hexdigest() != metadata.get("source_sha256", {}).get(relative):
                    return False
                parsed[relative] = ast.parse(content)
            except (OSError, SyntaxError, UnicodeError):
                return False
        if not _site_key_only(parsed[relative], line, field):
            return False
    return True
