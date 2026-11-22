"""Prove an isolated ordinary function body edit before removing def-line hits."""
import ast

from ast_compare import ast_equal


def _generator(function):
    pending = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return True
        pending.extend(ast.iter_child_nodes(node))
    return False


def runtime_body_lines(before, after, global_lines, changed_lines=None):
    """Return old body lines only when a single unchanged function owns the edit.

    Decorated definitions, inline bodies, and functions executed during
    import/collection decline the proof. Their existing dependencies survive.
    """
    old = [node for node in ast.walk(before) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    new = [node for node in ast.walk(after) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(old) != len(new):
        return None
    for left, right in zip(old, new):
        if changed_lines is not None and not all(left.lineno < line <= left.end_lineno for line in changed_lines):
            continue
        if left.decorator_list or right.decorator_list or not left.body or not right.body:
            continue
        if left.body[0].lineno <= left.lineno or right.body[0].lineno <= right.lineno:
            continue
        if ast_equal(left.body, right.body):
            continue
        if not ast_equal(before, after, {id(left.body): id(right.body)}):
            continue
        # Docstrings and generator flags are observable without executing a
        # function body. Preserve their existing definition consumers.
        if ast.get_docstring(left, clean=False) != ast.get_docstring(right, clean=False):
            continue
        if _generator(left) != _generator(right):
            continue
        lines = set(range(left.body[0].lineno, left.end_lineno + 1))
        if lines.intersection(global_lines):
            return None
        return lines
    return None
