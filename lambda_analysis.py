"""Recognize deferred lambda-body edits without changing input syntax trees.

This helper describes syntax only. Callers must separately check import-time
execution and use baseline coverage or qualified class-user observations.
"""
from __future__ import annotations

import ast
import copy


def _lambda_inventory(tree: ast.AST):
    found = {}

    def walk(node, path=(), lambda_depth=0):
        if isinstance(node, ast.Lambda):
            found[path] = (node, lambda_depth)
            lambda_depth += 1
        for field, value in ast.iter_fields(node):
            if isinstance(value, ast.AST):
                walk(value, (*path, field), lambda_depth)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    if isinstance(child, ast.AST):
                        walk(child, (*path, field, index), lambda_depth)

    walk(tree)
    return found


def changed_lambda_bodies(old_tree: ast.AST, new_tree: ast.AST) -> list[ast.Lambda] | None:
    """Return changed original lambdas if nothing but their bodies changed.

    Lambda positions, arguments/defaults, enclosing table keys and values,
    function/class definitions, and the rest of the module must remain equal.
    Added/deleted lambdas and edits involving nested lambdas are declined.
    Equivalent ASTs return an empty list; an unrecognized edit returns None.
    Returned nodes belong to ``old_tree`` and neither input tree is mutated.
    """
    old = _lambda_inventory(old_tree)
    new = _lambda_inventory(new_tree)
    if old.keys() != new.keys():
        return None

    changed = []
    for path, (before, depth) in old.items():
        after, _ = new[path]
        if ast.dump(before.args, include_attributes=False) != ast.dump(after.args, include_attributes=False):
            return None
        if ast.dump(before.body, include_attributes=False) == ast.dump(after.body, include_attributes=False):
            continue
        # An enclosing body's mask could otherwise hide a nested signature or
        # structure change. Leave all such cases to the caller's broad path.
        if depth or any(isinstance(node, ast.Lambda) for node in ast.walk(before.body)):
            return None
        if any(isinstance(node, ast.Lambda) for node in ast.walk(after.body)):
            return None
        changed.append(before)

    class MaskBodies(ast.NodeTransformer):
        def visit_Lambda(self, node):
            node.body = ast.Constant(value=None)
            return node

    def normalized(tree):
        return ast.dump(MaskBodies().visit(copy.deepcopy(tree)), include_attributes=False)

    if normalized(old_tree) != normalized(new_tree):
        return None
    return changed
