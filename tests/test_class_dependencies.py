import ast
import unittest

from class_dependencies import class_bindings, changed_bindings, changed_class_bindings


class ClassBindingTests(unittest.TestCase):
    def test_qualified_nested_class_has_enclosing_usage_owner(self):
        tree = ast.parse("class Outer:\n    class Inner:\n        FLAG = 1\n")
        binding, = changed_class_bindings(tree, {3})
        self.assertEqual((binding.qualname, binding.attribute), ("Outer.Inner", "FLAG"))
        self.assertEqual(binding.usage_owners, ("Outer", "Outer.Inner"))

    def test_same_attribute_in_different_classes_stays_distinct(self):
        tree = ast.parse("class A:\n    FLAG = 1\nclass B:\n    FLAG = 2\n")
        self.assertEqual([(b.qualname, b.attribute) for b in changed_class_bindings(tree, {4})], [("B", "FLAG")])

    def test_conditional_class_assignment_and_multiline_span(self):
        tree = ast.parse("class A:\n    if True:\n        FLAGS = {\n            'value': 1,\n        }\n")
        binding, = changed_class_bindings(tree, {4})
        self.assertEqual((binding.qualname, binding.attribute), ("A", "FLAGS"))

    def test_method_local_assignment_is_not_class_data(self):
        tree = ast.parse("class A:\n    def f(self):\n        FLAG = 1\n        self.FLAG = 2\n")
        self.assertEqual(class_bindings(tree), [])

    def test_local_class_uses_python_qualname(self):
        tree = ast.parse("def factory():\n    class Local:\n        FLAG = 1\n")
        binding, = class_bindings(tree)
        self.assertEqual(binding.qualname, "factory.<locals>.Local")

    def test_destructuring_and_augmented_bindings(self):
        tree = ast.parse("class A:\n    X, Y = 1, 2\n    X += 3\n")
        self.assertEqual([b.attribute for b in class_bindings(tree)], ["X", "Y", "X"])

    def test_inserted_and_deleted_bindings_are_paired(self):
        old = ast.parse("class A:\n    OLD = 1\n")
        new = ast.parse("class A:\n    NEW = 2\n")
        pairs = changed_bindings(old, new, {2}, {2})
        self.assertEqual(len(pairs[("A", "OLD")][1]), 0)
        self.assertEqual(len(pairs[("A", "NEW")][0]), 0)


if __name__ == "__main__":
    unittest.main()
