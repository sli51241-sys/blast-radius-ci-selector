"""Find qualified class-data bindings in the unmodified or patched AST.

This module describes source structure only. It neither predicts failures nor
uses patch/test labels. Runtime usage maps can index ``qualname`` using the
corresponding Python class's ``__qualname__``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from ast_compare import ast_equal


@dataclass(frozen=True)
class ClassBinding:
    qualname: str
    attribute: str
    lineno: int
    end_lineno: int
    enclosing_classes: tuple[str, ...]
    value: ast.expr | None
    kind: str

    @property
    def usage_owners(self) -> tuple[str, ...]:
        """Own class plus outer classes that may derive data from a nested one."""
        return (*self.enclosing_classes, self.qualname)


def _target_names(target: ast.expr):
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _target_names(element)
    elif isinstance(target, ast.Starred):
        yield from _target_names(target.value)


def class_bindings(tree: ast.AST) -> list[ClassBinding]:
    """Index class-body bindings, including class-scope conditional assignments.

    Assignments inside methods are excluded. Classes created inside functions
    retain Python's ``<locals>`` qualification. Attribute-target writes such as
    ``Other.FLAG = 1`` are deliberately absent: those need broader analysis.
    """
    found: list[ClassBinding] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.names: list[str] = []
            self.scopes: list[str] = []
            self.classes: list[str] = []

        def generic_visit(self, node):
            # Assignment values, targets and annotations contain expressions,
            # never class/function statement definitions or class assignments.
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case)):
                    self.visit(child)

        def visit_ClassDef(self, node):
            self.names.append(node.name)
            qualname = ".".join(self.names)
            self.scopes.append("class")
            self.classes.append(qualname)
            for statement in node.body:
                self.visit(statement)
            self.classes.pop()
            self.scopes.pop()
            self.names.pop()

        def visit_FunctionDef(self, node):
            self.names.extend((node.name, "<locals>"))
            self.scopes.append("function")
            for statement in node.body:
                self.visit(statement)
            self.scopes.pop()
            del self.names[-2:]

        visit_AsyncFunctionDef = visit_FunctionDef

        def assignment(self, node, targets):
            if self.scopes and self.scopes[-1] == "class":
                for target in targets:
                    for name in _target_names(target):
                        found.append(ClassBinding(
                            self.classes[-1], name, node.lineno, node.end_lineno,
                            tuple(self.classes[:-1]), getattr(node, "value", None),
                            type(node).__name__,
                        ))
            self.generic_visit(node)

        def visit_Assign(self, node):
            self.assignment(node, node.targets)

        def visit_AnnAssign(self, node):
            self.assignment(node, [node.target])

        def visit_AugAssign(self, node):
            self.assignment(node, [node.target])

    Visitor().visit(tree)
    return found


def changed_class_bindings(tree: ast.AST, changed_lines: set[int]) -> list[ClassBinding]:
    """Return class assignments intersecting changed lines in that AST's coordinates."""
    return [binding for binding in class_bindings(tree)
            if any(binding.lineno <= line <= binding.end_lineno for line in changed_lines)]


def changed_bindings(
    old_tree: ast.AST, new_tree: ast.AST, old_lines: set[int], new_lines: set[int]
) -> dict[tuple[str, str], tuple[list[ClassBinding], list[ClassBinding]]]:
    """Pair affected bindings from both trees by qualified class and attribute.

    Lists preserve repeated conditional or augmented assignments. A missing
    side signals an insertion/deletion, which callers should treat cautiously.
    """
    old_all, new_all = class_bindings(old_tree), class_bindings(new_tree)
    old_hits = [binding for binding in old_all
                if any(binding.lineno <= line <= binding.end_lineno for line in old_lines)]
    new_hits = [binding for binding in new_all
                if any(binding.lineno <= line <= binding.end_lineno for line in new_lines)]
    keys = {(b.qualname, b.attribute) for b in [*old_hits, *new_hits]}
    return {
        key: ([b for b in old_all if (b.qualname, b.attribute) == key],
              [b for b in new_all if (b.qualname, b.attribute) == key])
        for key in sorted(keys)
    }


def only_binding_values_changed(old_tree: ast.AST, new_tree: ast.AST, bindings: dict) -> bool:
    """Prove the rest of the program is unchanged before using class-only users.

    Line-span checks alone can overlook a second edit or a changed class header.
    Mask only the paired assignment values, retaining targets, annotations,
    operators, control flow, methods, and all other module statements.
    """
    if not bindings or any(not old or not new or len(old) != len(new)
                           for old, new in bindings.values()):
        return False

    replacements = {id(before.value): id(after.value)
                    for old, new in bindings.values() for before, after in zip(old, new)
                    if before.value is not None and after.value is not None}
    return ast_equal(old_tree, new_tree, replacements)


def literal_binding_values(bindings: dict) -> bool:
    """Restrict narrowing to data that cannot introduce import-time calls."""
    try:
        for old, new in bindings.values():
            for binding in [*old, *new]:
                if (binding.kind == "AugAssign"
                        or binding.attribute.startswith("__") and binding.attribute.endswith("__")):
                    return False
                ast.literal_eval(binding.value)
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return False
    return bool(bindings)


def statement_classes(tree: ast.AST):
    """Yield classes through statement suites without visiting expressions."""
    pending = [tree]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.ClassDef):
            yield node
        pending.extend(child for child in ast.iter_child_nodes(node)
                       if isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case)))


def generated_classes(tree: ast.AST) -> bool:
    """Generated/implicit methods can use defaults without observed receivers."""
    generated_bases = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "AutoName", "NamedTuple"}
    return any(node.decorator_list or any(
        (base.id if isinstance(base, ast.Name) else
         base.attr if isinstance(base, ast.Attribute) else "") in generated_bases
        for base in node.bases)
        for node in statement_classes(tree))
