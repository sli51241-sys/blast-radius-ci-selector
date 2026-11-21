import ast
import unittest

from lambda_analysis import changed_lambda_bodies


class LambdaBodyTests(unittest.TestCase):
    def parse(self, source):
        return ast.parse(source, type_comments=True)

    def match(self, before, after):
        return changed_lambda_bodies(self.parse(before), self.parse(after))

    def test_callback_body_change_preserves_surrounding_nonliteral_table(self):
        old = "class Config:\n    HANDLERS = {**Base.HANDLERS, 'x': lambda args: build(args[0]), 'y': factory()}\n"
        hits = self.match(old, old.replace("args[0]", "args[1]"))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].lineno, 2)

    def test_returns_only_actually_changed_original_nodes(self):
        old = self.parse("first = lambda x: x + 1\nsecond = lambda y: y + 2\n")
        new = self.parse("first = lambda x: x + 1\nsecond = lambda y: y + 3\n")
        original = [n for n in ast.walk(old) if isinstance(n, ast.Lambda)]
        hits = changed_lambda_bodies(old, new)
        self.assertEqual(hits, [original[1]])
        self.assertIs(hits[0], original[1])

    def test_multiple_independent_bodies_can_change(self):
        hits = self.match("a = lambda x: x + 1\nb = lambda x: x + 2\n",
                          "a = lambda x: x + 3\nb = lambda x: x + 4\n")
        self.assertEqual([node.lineno for node in hits], [1, 2])

    def test_default_change_is_declined(self):
        self.assertIsNone(self.match("fn = lambda x=1: x + 1", "fn = lambda x=2: x + 1"))

    def test_signature_changes_are_declined(self):
        for updated in ("lambda y: y + 1", "lambda x, y=0: x + 1", "lambda *, x: x + 1"):
            with self.subTest(updated=updated):
                self.assertIsNone(self.match("fn = lambda x: x + 1", "fn = " + updated))

    def test_class_header_change_is_declined(self):
        old = "class C(Base):\n    FN = lambda x: x + 1\n"
        self.assertIsNone(self.match(old, old.replace("Base", "Other").replace("x + 1", "x + 2")))

    def test_class_field_mixed_with_body_change_is_declined(self):
        old = "class C:\n    FLAG = 1\n    FN = lambda x: x + 1\n"
        self.assertIsNone(self.match(old, old.replace("FLAG = 1", "FLAG = 2").replace("x + 1", "x + 2")))

    def test_unrelated_function_change_is_declined(self):
        old = "fn = lambda x: x + 1\ndef other():\n    return 1\n"
        self.assertIsNone(self.match(old, old.replace("x + 1", "x + 2").replace("return 1", "return 2")))

    def test_table_key_change_is_declined(self):
        self.assertIsNone(self.match("table = {'old': lambda x: x + 1}",
                                     "table = {'new': lambda x: x + 2}"))

    def test_other_table_value_change_is_declined(self):
        self.assertIsNone(self.match("table = {'fn': lambda x: x + 1, 'limit': 10}",
                                     "table = {'fn': lambda x: x + 2, 'limit': 20}"))

    def test_add_and_remove_are_declined(self):
        self.assertIsNone(self.match("value = 1", "value = lambda x: x"))
        self.assertIsNone(self.match("value = lambda x: x", "value = 1"))

    def test_added_nested_lambda_is_declined(self):
        self.assertIsNone(self.match("fn = lambda x: x", "fn = lambda x: (lambda y: y)(x)"))

    def test_changed_nested_lambda_body_is_declined(self):
        self.assertIsNone(self.match("fn = lambda x: (lambda y: y + 1)(x)",
                                     "fn = lambda x: (lambda y: y + 2)(x)"))

    def test_changed_outer_body_with_nested_lambda_is_declined(self):
        self.assertIsNone(self.match("fn = lambda x: (lambda y: y)(x) + 1",
                                     "fn = lambda x: (lambda y: y)(x) + 2"))

    def test_changed_nested_default_is_declined(self):
        self.assertIsNone(self.match("fn = lambda x: (lambda y=1: y)(x)",
                                     "fn = lambda x: (lambda y=2: y)(x)"))

    def test_lambda_inside_default_is_not_masked(self):
        self.assertIsNone(self.match("fn = lambda callback=(lambda x: x + 1): callback()",
                                     "fn = lambda callback=(lambda x: x + 2): callback()"))

    def test_equivalent_program_returns_no_changed_lambdas(self):
        self.assertEqual(self.match("fn = lambda x: x + 1", "# comment\nfn = lambda x: x + 1"), [])
        self.assertEqual(self.match("value = 1", "value = 1"), [])

    def test_nonlambda_change_is_declined_without_any_lambda(self):
        self.assertIsNone(self.match("value = 1", "value = 2"))

    def test_input_trees_are_not_mutated(self):
        before = self.parse("fn = lambda x: x + 1")
        after = self.parse("fn = lambda x: x + 2")
        snapshots = [ast.dump(tree, include_attributes=True) for tree in (before, after)]
        changed_lambda_bodies(before, after)
        self.assertEqual([ast.dump(tree, include_attributes=True) for tree in (before, after)], snapshots)


if __name__ == "__main__":
    unittest.main()
