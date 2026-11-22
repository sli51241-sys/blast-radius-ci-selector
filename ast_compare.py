"""Compare parsed ASTs without formatting or copying whole trees.

The canonical fields match Python 3.13's ast.dump defaults: source attributes
are ignored, optional None fields and typed empty-list fields are omitted.
Replacement IDs let callers mask paired subtrees after proving their scope.
"""
from __future__ import annotations

import ast


_MISSING = object()
_SIMPLE = (str, bytes, int, bool, type(None), type(Ellipsis))


def _fields(node: ast.AST):
    cls = type(node)
    types = getattr(cls, "_field_types", {})
    result = []
    for name in node._fields:
        value = getattr(node, name, _MISSING)
        if value is _MISSING:
            continue
        if value is None and getattr(cls, name, _MISSING) is None:
            continue
        if isinstance(value, list) and not value:
            if getattr(types.get(name, object), "__origin__", _MISSING) is list:
                continue
        result.append((name, value))
    return result


def ast_equal(before, after, replacements: dict[int, int] | None = None) -> bool:
    """Match dump equality for parsed trees, optionally masking paired AST IDs.

    Primitive types stay distinct (True differs from 1). Float/complex reprs
    preserve dump's treatment of NaN and signed zero. No input is modified.
    """
    pending = [(before, after)]
    while pending:
        left, right = pending.pop()
        if replacements and id(left) in replacements:
            if id(right) != replacements[id(left)]:
                return False
            continue
        if type(left) is not type(right):
            return False
        if isinstance(left, ast.AST):
            old_fields, new_fields = _fields(left), _fields(right)
            if len(old_fields) != len(new_fields):
                return False
            for (old_name, old), (new_name, new) in zip(reversed(old_fields), reversed(new_fields)):
                if old_name != new_name:
                    return False
                pending.append((old, new))
        elif isinstance(left, list):
            if len(left) != len(right):
                return False
            pending.extend(zip(reversed(left), reversed(right)))
        elif isinstance(left, _SIMPLE):
            if left != right:
                return False
        elif repr(left) != repr(right):
            return False
    return True
