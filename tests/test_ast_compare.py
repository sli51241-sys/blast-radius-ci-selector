import ast
import copy
import itertools
import unittest

from ast_compare import ast_equal
from class_dependencies import changed_bindings, class_bindings, only_binding_values_changed


def dump_equal(before, after):
    return ast.dump(before, include_attributes=False) == ast.dump(after, include_attributes=False)


def old_binding_values_changed(before, after, bindings):
    """Previous copy/mask/dump proof, retained as a differential oracle."""
    if not bindings or any(not old or not new or len(old) != len(new)
                           for old, new in bindings.values()):
        return False

    def normalized(tree):
        tree = copy.deepcopy(tree)
        values = {id(binding.value) for binding in class_bindings(tree)
                  if (binding.qualname, binding.attribute) in bindings
                  and binding.value is not None}

        class MaskValues(ast.NodeTransformer):
            def visit(self, node):
                if id(node) in values:
                    return ast.Constant(value=None)
                return super().visit(node)

        return ast.dump(MaskValues().visit(tree), include_attributes=False)

    return normalized(before) == normalized(after)


class AstCompareTests(unittest.TestCase):
    def assert_dump_agrees(self, before, after):
        self.assertEqual(ast_equal(before, after), dump_equal(before, after),
                         (ast.dump(before), ast.dump(after)))

    def test_parsed_source_differential(self):
        sources = [
            "", "# comment", "x = 1", "x = True", "x = 1.0", "x = -0.0",
            "x = 'text'", "x = b'text'", "x: int", "x: int = None",
            "x = [1, 2]", "x = (1, 2)", "x = {1, 2}", "x = {'a': 1}",
            "def f(x=1, /, *args, y=None, **kwargs):\n return x",
            "async def f():\n await g()", "f = lambda x=1: x + 1",
            "class C(Base, metaclass=M):\n X = 1\n def f(self): return self.X",
            "match x:\n case {'a': y, **rest}: pass", "type T[X] = list[X]",
            "try:\n f()\nexcept* ValueError:\n pass",
            "x = 1 # type: ignore[attr-defined]", "x = [i for i in values if i]",
        ]
        trees = [ast.parse(source, type_comments=True) for source in sources]
        for before, after in itertools.product(trees, repeat=2):
            self.assert_dump_agrees(before, after)
        for tree in trees:
            self.assert_dump_agrees(tree, copy.deepcopy(tree))

    def test_scalar_types_and_float_representations(self):
        values = [True, 1, 1.0, False, 0, 0.0, -0.0, None, Ellipsis,
                  float('nan'), float('nan'), float('inf'), float('-inf'),
                  complex(0.0, 0.0), complex(-0.0, 0.0), complex(0.0, -0.0),
                  'text', b'text', (True,), (1,), frozenset({1})]
        for before, after in itertools.product(values, repeat=2):
            self.assert_dump_agrees(ast.Constant(value=before), ast.Constant(value=after))
        self.assertFalse(ast_equal(ast.Constant(value=True), ast.Constant(value=1)))
        self.assertFalse(ast_equal(ast.Constant(value=0.0), ast.Constant(value=-0.0)))

    def test_source_attributes_ignored_but_type_ignore_lineno_compared(self):
        before, after = ast.parse('x = 1'), ast.parse('\n\nx = 1')
        self.assertTrue(ast_equal(before, after))
        self.assert_dump_agrees(before, after)
        before = ast.TypeIgnore(lineno=1, tag='[name-defined]')
        after = ast.TypeIgnore(lineno=2, tag='[name-defined]')
        self.assertFalse(ast_equal(before, after))
        self.assert_dump_agrees(before, after)

    def test_default_omitted_fields_match_dump(self):
        missing_value = ast.Constant(value=0)
        del missing_value.value
        pairs = [
            (ast.Return(), ast.Return(value=None)),
            (ast.arguments(), ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
                                            kw_defaults=[], defaults=[])),
            (ast.Module(), ast.Module(body=[], type_ignores=[])),
            (missing_value, ast.Constant(value=None)),
        ]
        for before, after in pairs:
            self.assert_dump_agrees(before, after)
        self.assertFalse(ast_equal(missing_value, ast.Constant(value=None)))

    def test_mask_requires_exact_paired_subtree_and_preserves_neighbors(self):
        before = ast.parse('class C:\n VALUE = (1, 2)\n OTHER = 3')
        after = ast.parse('class C:\n VALUE = factory(x)\n OTHER = 3')
        old_value, new_value = before.body[0].body[0].value, after.body[0].body[0].value
        snapshots = [ast.dump(tree, include_attributes=True) for tree in (before, after)]
        self.assertTrue(ast_equal(before, after, {id(old_value): id(new_value)}))
        self.assertFalse(ast_equal(before, after, {id(old_value): id(copy.deepcopy(new_value))}))
        after.body[0].body[1].value.value = 4
        self.assertFalse(ast_equal(before, after, {id(old_value): id(new_value)}))
        after.body[0].body[1].value.value = 3
        self.assertEqual([ast.dump(tree, include_attributes=True) for tree in (before, after)], snapshots)

    def test_iterative_comparison_accepts_deep_trees(self):
        before, after = ast.Constant(value=1), ast.Constant(value=1)
        for _ in range(2500):
            before = ast.UnaryOp(op=ast.UAdd(), operand=before)
            after = ast.UnaryOp(op=ast.UAdd(), operand=after)
        self.assertTrue(ast_equal(before, after))

    def test_class_mask_matches_previous_proof(self):
        sources = [
            "class C:\n X = 1\n Y = 2\n def f(self): return 3",
            "class C:\n X = 2\n Y = 2\n def f(self): return 3",
            "class C:\n X = 2\n Y = 2\n def f(self): return 4",
            "class C(Base):\n X = 2\n Y = 2\n def f(self): return 3",
            "class C:\n X: int = 1\n X = 2\n if flag: X = 3",
            "class C:\n X: int = 1\n X = 4\n if flag: X = 5",
            "class C:\n X: str = 1\n X = 4\n if flag: X = 5",
            "class C:\n X = Y = 1\n class D:\n  X = 2",
            "class C:\n X = Y = 2\n class D:\n  X = 3",
            "class C:\n X: int\n Y = 2",
            "class C:\n X: int = 1\n Y = 2",
            "def f():\n class C:\n  X = 1\n return C",
            "def f():\n class C:\n  X = 2\n return C",
        ]
        trees = [ast.parse(source) for source in sources]
        for before, after in itertools.product(trees, repeat=2):
            for lines in ({2}, {3}, set(range(1, 10))):
                bindings = changed_bindings(before, after, lines, lines)
                self.assertEqual(only_binding_values_changed(before, after, bindings),
                                 old_binding_values_changed(before, after, bindings),
                                 (ast.dump(before), ast.dump(after), lines))


if __name__ == '__main__':
    unittest.main()
