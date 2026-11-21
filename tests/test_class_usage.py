"""Context export checks for baseline-only class instrumentation."""

from types import SimpleNamespace
import unittest

from tools.class_usage import ClassUsage


class ClassUsageExportTests(unittest.TestCase):
    def export(self, context):
        observer = ClassUsage.__new__(ClassUsage)
        path, name = "sqlglot/example.py", "Settings"
        observer.inventory = {path: [name]}
        observer.class_contexts = {(path, name): {context}}
        observer.attribute_contexts = {(path, name, "FLAG"): {context}}

        def consumers(measured_context, indices):
            if measured_context.startswith("@collect:"):
                file = measured_context.removeprefix("@collect:")
                return [index for node, index in indices.items() if node.split("::", 1)[0] == file]
            node = measured_context.rsplit("|", 1)[0]
            return [indices[node]] if node in indices else []

        observer.plugin = SimpleNamespace(consumers=consumers)
        return observer.export({"tests/test_example.py::test_one": 7})

    def test_collection_usage_is_marked_import_time_and_keeps_consumers(self):
        result = self.export("@collect:tests/test_example.py")
        self.assertEqual(result["class_usage_import_contexts"], ["", "@global", "@collect:*"])
        self.assertEqual(result["class_usage"], {"sqlglot/example.py": {"Settings": [7]}})
        self.assertEqual(result["attribute_usage"], {"sqlglot/example.py": {"Settings": {"FLAG": [7]}}})
        self.assertEqual(result["class_global_usage"], {"sqlglot/example.py": ["Settings"]})
        self.assertEqual(result["attribute_global_usage"], {"sqlglot/example.py": {"Settings": ["FLAG"]}})

    def test_precollection_contexts_are_marked_import_time_without_consumers(self):
        for context in ("", "@global"):
            with self.subTest(context=context):
                result = self.export(context)
                self.assertEqual(result["class_usage"], {"sqlglot/example.py": {"Settings": []}})
                self.assertEqual(result["class_global_usage"], {"sqlglot/example.py": ["Settings"]})
                self.assertEqual(result["attribute_global_usage"], {"sqlglot/example.py": {"Settings": ["FLAG"]}})

    def test_test_and_fixture_contexts_are_not_marked_import_time(self):
        for context in ("tests/test_example.py::test_one|setup", "tests/test_example.py::test_one|call", "@fixture:123"):
            with self.subTest(context=context):
                result = self.export(context)
                self.assertEqual(result["class_global_usage"], {})
                self.assertEqual(result["attribute_global_usage"], {})

    def test_vars_observes_dictionary_attribute_and_preserves_result(self):
        observer = ClassUsage.__new__(ClassUsage)
        observer._getattr, observer._hasattr, observer._vars = getattr, hasattr, vars
        observed = []
        observer._attribute = lambda value, name: observed.append((value, name))
        value = SimpleNamespace(FLAG=1)
        result = observer.call(vars, value)
        self.assertIs(result, vars(value))
        self.assertEqual(observed, [(value, "__dict__")])


if __name__ == "__main__":
    unittest.main()
