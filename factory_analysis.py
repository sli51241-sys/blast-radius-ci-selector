"""Prove literal argument edits affect only deferred callback construction.

The proof is derived from the unchanged source, not from patch identifiers or
test labels. Callers still need class-usage, import-consumer and import checks.
"""
from __future__ import annotations

import ast
from pathlib import Path


def _equal(before, after, replacements=None):
    if replacements and id(before) in replacements:
        return id(after) == replacements[id(before)]
    if type(before) is not type(after):
        return False
    if isinstance(before, ast.AST):
        return all(_equal(left, right, replacements)
                   for (_, left), (_, right) in zip(ast.iter_fields(before), ast.iter_fields(after)))
    if isinstance(before, list):
        return len(before) == len(after) and all(
            _equal(left, right, replacements) for left, right in zip(before, after))
    return before == after


def _literal(node):
    allowed = (ast.Constant, ast.Tuple, ast.List, ast.Set, ast.Dict,
               ast.UnaryOp, ast.UAdd, ast.USub, ast.Load)
    if any(not isinstance(part, allowed) for part in ast.walk(node)):
        return False
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return False
    return True


def _data_calls(node, path=()):
    """Calls stored directly in literal containers, never passed to another call."""
    if isinstance(node, ast.Call):
        yield path, node
    elif isinstance(node, ast.Dict):
        for index, (key, value) in enumerate(zip(node.keys, node.values)):
            if key is not None:
                yield from _data_calls(value, (*path, "values", index))
    elif isinstance(node, (ast.Tuple, ast.List)):
        for index, value in enumerate(node.elts):
            yield from _data_calls(value, (*path, "elts", index))


def _argument_changes(before, after):
    if not isinstance(before.func, ast.Name) or not _equal(before.func, after.func):
        return None
    if len(before.args) != len(after.args) or len(before.keywords) != len(after.keywords):
        return None
    if any(isinstance(node, ast.Starred) for node in [*before.args, *after.args]):
        return None
    if [kw.arg for kw in before.keywords] != [kw.arg for kw in after.keywords]:
        return None
    if any(kw.arg is None for kw in [*before.keywords, *after.keywords]):
        return None
    old_values = [*before.args, *(kw.value for kw in before.keywords)]
    new_values = [*after.args, *(kw.value for kw in after.keywords)]
    changed = []
    for old, new in zip(old_values, new_values):
        if _equal(old, new):
            continue
        if not _literal(old) or not _literal(new):
            return None
        changed.append((old, new))
    return changed


def _scope_bindings(statements):
    """Conservatively inventory names bound in a module/class namespace."""
    bindings = {}

    def record(name, node):
        bindings.setdefault(name, []).append(node)

    def walk(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            record(node.name, node)
            # Definition headers can bind names through assignment expressions.
            for field, value in ast.iter_fields(node):
                if field not in ("body", "name"):
                    children(value)
            return
        if isinstance(node, ast.Lambda):
            children(node.args.defaults)
            children(node.args.kw_defaults)
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                record(alias.asname or (alias.name.split(".", 1)[0]
                                       if isinstance(node, ast.Import) else alias.name), node)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            record(node.id, node)
        if isinstance(node, ast.ExceptHandler) and node.name:
            record(node.name, node)
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            record(node.name, node)
        if isinstance(node, ast.MatchMapping) and node.rest:
            record(node.rest, node)
        for child in ast.iter_child_nodes(node):
            walk(child)

    def children(value):
        if isinstance(value, ast.AST):
            walk(value)
        elif isinstance(value, list):
            for child in value:
                children(child)

    children(statements)
    return bindings


def _class_scopes(tree, qualname):
    matches = []

    def walk(node, names=(), scopes=()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        if isinstance(node, ast.ClassDef):
            names = (*names, node.name)
            scopes = (*scopes, node)
            if ".".join(names) == qualname:
                matches.append(scopes)
        for child in ast.iter_child_nodes(node):
            walk(child, names, scopes)

    walk(tree)
    return matches[0] if len(matches) == 1 else None


def _class_field_reads_safe(tree, owner, attribute, dictionary):
    """Allow import-time bare-name reads only as unchanged dictionary keys."""
    scopes = _class_scopes(tree, owner)
    if scopes is None:
        return False

    def walk(node, parent=None, field=None):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == attribute:
            return dictionary and isinstance(parent, ast.comprehension) and field == "iter"
        # Bodies are deferred; decorators, defaults and annotations are not.
        ignored = {"body"} if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) else set()
        for name, value in ast.iter_fields(node):
            if name in ignored:
                continue
            if isinstance(value, ast.AST) and not walk(value, node, name):
                return False
            if isinstance(value, list):
                for child in value:
                    if isinstance(child, ast.AST) and not walk(child, node, name):
                        return False
        return True

    return all(walk(statement) for statement in scopes[-1].body)


def _deferred_annotations(tree):
    return any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
               and any(alias.name == "annotations" for alias in node.names)
               for node in tree.body)


def _callback_header_safe(node, future_annotations):
    if getattr(node, "decorator_list", ()) or getattr(node, "type_params", ()):
        return False
    if node.args.defaults or any(value is not None for value in node.args.kw_defaults):
        return False
    annotations = [arg.annotation for arg in [*node.args.posonlyargs, *node.args.args,
                                             *node.args.kwonlyargs]]
    annotations.extend(getattr(arg, "annotation", None)
                       for arg in (node.args.vararg, node.args.kwarg))
    annotations.append(getattr(node, "returns", None))
    return future_annotations or not any(value is not None for value in annotations)


def _pure_factory(function, module):
    if not isinstance(function, ast.FunctionDef) or function.decorator_list or getattr(function, "type_params", ()):
        return False
    body = list(function.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body.pop(0)
    deferred = _deferred_annotations(module)
    if len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Lambda):
        return _callback_header_safe(body[0].value, deferred)
    if (len(body) == 2 and isinstance(body[0], ast.FunctionDef)
            and isinstance(body[1], ast.Return) and isinstance(body[1].value, ast.Name)
            and body[1].value.id == body[0].name):
        return _callback_header_safe(body[0], deferred)
    return False


def _resolve_factory(name, tree, owner, source, active_overrides):
    scopes = _class_scopes(tree, owner)
    if scopes is None or any({name, "*"}.intersection(_scope_bindings(scope.body)) for scope in scopes):
        return False
    module_bindings = _scope_bindings(tree.body)
    if "*" in module_bindings:
        return False
    bindings = module_bindings.get(name, [])
    if len(bindings) != 1:
        return False
    bound = bindings[0]
    if isinstance(bound, ast.FunctionDef):
        return _pure_factory(bound, tree)
    if not isinstance(bound, ast.ImportFrom) or bound.level or not bound.module:
        return False
    aliases = [alias for alias in bound.names if (alias.asname or alias.name) == name]
    if len(aliases) != 1 or aliases[0].name == "*":
        return False
    relative = bound.module.replace(".", "/")
    for suffix in (".py", "/__init__.py"):
        path = (source / (relative + suffix)).resolve()
        if not path.is_relative_to(source.resolve()) or not path.is_file():
            continue
        key = path.relative_to(source.resolve()).as_posix()
        if key in active_overrides:
            return False
        try:
            module = ast.parse(path.read_bytes(), filename=key, type_comments=True)
        except (OSError, SyntaxError, UnicodeError):
            return False
        target_bindings = _scope_bindings(module.body)
        if "*" in target_bindings:
            return False
        target = target_bindings.get(aliases[0].name, [])
        return len(target) == 1 and _pure_factory(target[0], module)
    return False


def changed_factory_arguments(old_tree, new_tree, bindings, source, active_overrides=None) -> bool:
    """Accept only literal argument changes to source-proven callback factories.

    Scope checks decline aliases shadowed in module/class namespaces. Calls must
    be direct class-field values or values inside literal dictionaries/lists/
    tuples. Calls passed to another call, changes to table keys, and changes to
    factory modules in the active patch are declined. Bare field reads during
    class construction must be keys-only comprehensions over a literal dict.
    No full-tree copy occurs; bindings without changed literal call arguments
    return before scope scans.
    """
    replacements = {}
    candidates = []
    for (owner, attribute), (old_bindings, new_bindings) in bindings.items():
        if attribute.startswith("__") and attribute.endswith("__"):
            return False
        if not old_bindings or len(old_bindings) != len(new_bindings):
            return False
        for before, after in zip(old_bindings, new_bindings):
            if (getattr(before, "kind", None) not in ("Assign", "AnnAssign")
                    or getattr(after, "kind", None) not in ("Assign", "AnnAssign")):
                return False
            old_calls = dict(_data_calls(before.value)) if before.value is not None else {}
            new_calls = dict(_data_calls(after.value)) if after.value is not None else {}
            for path, old_call in old_calls.items():
                new_call = new_calls.get(path)
                if new_call is None:
                    continue
                changes = _argument_changes(old_call, new_call)
                if changes:
                    replacements.update({id(old): id(new) for old, new in changes})
                    candidates.append((old_call.func.id, owner, attribute,
                                       isinstance(before.value, ast.Dict)))
    if not candidates:
        return False
    if not _equal(old_tree, new_tree, replacements):
        return False
    source = Path(source)
    active_overrides = active_overrides or {}
    return all(_class_field_reads_safe(old_tree, owner, attribute, dictionary)
               and _resolve_factory(name, old_tree, owner, source, active_overrides)
               for name, owner, attribute, dictionary in set(candidates))
