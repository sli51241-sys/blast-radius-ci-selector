import ast
import unittest

from tools.freeze_operator_audit import function_compares, mutate


class OperatorAuditTests(unittest.TestCase):
    def change(self, source):
        tree = ast.parse(source, type_comments=True)
        nodes = function_compares(tree)
        self.assertEqual(len(nodes), 1)
        original = ast.dump(tree, include_attributes=True)
        modified, before, after = mutate(source, tree, nodes[0][1])
        self.assertEqual(source.replace(before, after, 1), modified)
        self.assertEqual(ast.dump(tree, include_attributes=True), original)
        return modified

    def test_each_supported_operator_changes_only_the_operator(self):
        for before, after in ((">", ">="), (">=", ">"), ("<", "<="), ("<=", "<"),
                              ("==", "!="), ("!=", "=="), ("is", "=="), ("is not", "!=")):
            with self.subTest(before=before):
                source = f"def f(left, right):\n    return left {before} right\n"
                self.assertEqual(self.change(source), source.replace(f" {before} ", f" {after} "))

    def test_unicode_before_operator_uses_utf8_ast_columns(self):
        self.assertEqual(self.change("def f(é):\n    return é is None\n"),
                         "def f(é):\n    return é == None\n")

    def test_multiline_is_not_preserves_program_scope(self):
        source = "def f(value):\n    return (value is  # comment\n            not None)\n"
        modified = self.change(source)
        self.assertIn("value != None", modified)

    def test_parenthesized_operands_are_unchanged(self):
        source = "def f(left, right):\n    return (left + 1) < (right - 1)\n"
        self.assertEqual(self.change(source), source.replace(" < ", " <= "))

    def test_definitions_headers_class_bodies_and_lambdas_are_not_candidates(self):
        source = """value = 1 == 2
@decorate(3 == 4)
def f(x=(5 == 6)):
    fn = lambda y: y == 0
    class C:
        FLAG = 7 == 8
        def method(self):
            return 9 < 10
    return x > 0
"""
        found = function_compares(ast.parse(source))
        self.assertEqual([(name, type(node.ops[0]).__name__) for name, node in found],
                         [("f.C.method", "Lt"), ("f", "Gt")])

    def test_chained_and_unsupported_operators_are_excluded(self):
        source = "def f(a, b, c):\n    return a < b < c or a in b or a not in b\n"
        self.assertEqual(function_compares(ast.parse(source)), [])


if __name__ == "__main__":
    unittest.main()
