"""Integration cases for qualified runtime class users, using synthetic sources.

The fixture's class_usage indices are baseline-map indices, not submitted test
numbers. No event patches, failure labels, or mutation-specific answers appear.
"""

import difflib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from selector import Selector


class ClassPrecisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        (self.source / "pkg").mkdir(parents=True)
        (self.source / "pkg/__init__.py").write_text("")
        # Noncontiguous, shuffled numbers catch accidental use of map indices.
        self.tests = [
            {"number": number, "id": f"tests/test_{i}.py::test_case", "file": f"tests/test_{i}.py", "seconds": 0.01}
            for i, number in enumerate([11, 7, 23, 31])
        ]
        self.path = "pkg/core.py"

    def select(self, old, new, class_usage, *, readers=(), import_safe=True,
               global_attributes=(), import_metadata=True, global_read=False):
        (self.source / self.path).write_text(old, encoding="utf-8")
        # Every module-level line was executed at collection. That alone must
        # not turn a qualified class-data edit into all test users of the file.
        files = {self.path: {
            "lines": {str(n): list(range(4)) for n in range(1, len(old.splitlines()) + 1)},
            "global": list(range(1, len(old.splitlines()) + 1)),
            "reads": list(readers),
            "global_read": global_read,
        }}
        data = {"tests": [test["id"] for test in self.tests], "files": files, "imports": {}}
        if class_usage is not None:
            data["class_usage"] = {self.path: class_usage}
        if import_metadata:
            data["attribute_global_usage"] = {self.path: dict(global_attributes)}
            data["class_usage_import_contexts"] = ["", "@global", "@collect:*"]
        map_path = self.root / "coverage.json"
        map_path.write_text(json.dumps(data))
        patch_text = "".join(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile="a/" + self.path, tofile="b/" + self.path,
        ))
        with patch.object(Selector, "_class_import_safe", return_value=import_safe, create=True):
            selected, reasons = Selector(self.tests, self.source, map_path).select(patch_text)
        return {test["number"] for test in selected}, reasons

    def test_same_attribute_on_unrelated_classes_stays_qualified(self):
        old = "class A:\n    FLAG = 1\nclass B:\n    FLAG = 2\n"
        selected, _ = self.select(old, old.replace("FLAG = 1", "FLAG = 3"), {"A": [0], "B": [1]})
        self.assertEqual(selected, {11})

    def test_inherited_base_change_keeps_mro_inclusive_users(self):
        old = "class Base:\n    FLAG = 1\nclass Derived(Base):\n    pass\nclass Other:\n    FLAG = 9\n"
        # Instrumentation records a Derived receiver against every MRO class.
        selected, _ = self.select(old, old.replace("FLAG = 1", "FLAG = 3"), {"Base": [0, 1], "Derived": [1], "Other": [2]})
        self.assertEqual(selected, {11, 7})

    def test_nested_data_change_includes_outer_derived_property_users(self):
        old = "class Outer:\n    class Inner:\n        FLAG = 1\n    DERIVED = Inner.FLAG + 1\nclass Other:\n    FLAG = 9\n"
        selected, _ = self.select(old, old.replace("FLAG = 1", "FLAG = 3"), {"Outer": [0], "Outer.Inner": [1], "Other": [2]})
        self.assertEqual(selected, {11, 7})

    def test_class_data_change_preserves_raw_source_readers(self):
        old = "class A:\n    FLAG = 1\nclass B:\n    FLAG = 2\n"
        selected, _ = self.select(old, old.replace("FLAG = 1", "FLAG = 3"), {"A": [0], "B": [1]}, readers=[3])
        self.assertEqual(selected, {11, 31})

    def test_missing_class_usage_map_falls_back_conservatively(self):
        old = "class A:\n    FLAG = 1\n"
        selected, _ = self.select(old, old.replace("FLAG = 1", "FLAG = 3"), None)
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_failed_import_probe_keeps_conservative_fallback(self):
        old = "class A:\n    FLAG = 1\nclass B:\n    FLAG = 2\n"
        selected, _ = self.select(old, old.replace("FLAG = 1", "FLAG = 3"), {"A": [0], "B": [1]}, import_safe=False)
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_missing_qualified_class_entry_falls_back_conservatively(self):
        old = "class A:\n    FLAG = 1\nclass B:\n    FLAG = 2\n"
        selected, _ = self.select(old, old.replace("FLAG = 1", "FLAG = 3"), {"B": [1]})
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_inserted_class_field_keeps_conservative_fallback(self):
        old = "class A:\n    FLAG = 1\n"
        selected, _ = self.select(old, old + "    NEW = 2\n", {"A": [0]})
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_deleted_class_field_keeps_conservative_fallback(self):
        old = "class A:\n    FLAG = 1\n"
        selected, _ = self.select(old, "class A:\n    pass\n", {"A": [0]})
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_mixed_class_field_and_unrelated_function_keeps_broad_fallback(self):
        old = "class A:\n    FLAG = 1\n\ndef unrelated():\n    return 2\n"
        new = old.replace("FLAG = 1", "FLAG = 3").replace("return 2", "return 4")
        selected, _ = self.select(old, new, {"A": [0]})
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_mixed_class_field_and_base_change_keeps_broad_fallback(self):
        old = "class A(Base):\n    FLAG = 1\n"
        new = old.replace("Base", "OtherBase").replace("FLAG = 1", "FLAG = 3")
        selected, _ = self.select(old, new, {"A": [0]})
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_other_edit_on_assignment_line_keeps_broad_fallback(self):
        old = "class A:\n    FLAG = 1; effect(2)\n"
        new = old.replace("FLAG = 1", "FLAG = 3").replace("effect(2)", "effect(4)")
        selected, _ = self.select(old, new, {"A": [0]})
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_changed_annotation_keeps_broad_fallback(self):
        old = "class A:\n    FLAG: int = 1\n"
        new = "class A:\n    FLAG: str = '3'\n"
        selected, _ = self.select(old, new, {"A": [0]})
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_multiline_class_value_can_change_line_count(self):
        old = "class A:\n    FLAGS = {\n        'a': 1,\n    }\nclass B:\n    FLAG = 2\n"
        new = old.replace("'a': 1,", "'a': 3,\n        'b': 4,")
        selected, _ = self.select(old, new, {"A": [0], "B": [1]})
        self.assertEqual(selected, {11})

    def test_import_time_field_reader_keeps_broad_fallback(self):
        old = "class A:\n    FLAG = 1\n"
        selected, _ = self.select(old, old.replace("1", "3"), {"A": [0]},
                                  global_attributes={"A": ["FLAG"]})
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_import_time_dictionary_reader_keeps_broad_fallback(self):
        old = "class A:\n    FLAG = 1\n"
        selected, _ = self.select(old, old.replace("1", "3"), {"A": [0]},
                                  global_attributes={"A": ["__dict__"]})
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_missing_import_metadata_keeps_broad_fallback(self):
        old = "class A:\n    FLAG = 1\n"
        selected, _ = self.select(old, old.replace("1", "3"), {"A": [0]}, import_metadata=False)
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_new_rhs_call_keeps_broad_fallback(self):
        old = "class A:\n    FLAG = 1\n"
        selected, _ = self.select(old, old.replace("1", "effect()"), {"A": [0]})
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_augmented_assignment_keeps_broad_fallback(self):
        old = "class A:\n    FLAG = 1\n    FLAG += 2\n"
        selected, _ = self.select(old, old.replace("+= 2", "+= 3"), {"A": [0]})
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_decorated_class_keeps_broad_fallback_for_generated_methods(self):
        old = "@dataclass\nclass A:\n    FLAG: int = 1\n"
        selected, _ = self.select(old, old.replace("1", "3"), {"A": [0]})
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_generated_class_bases_keep_broad_fallback(self):
        for base in ("Enum", "enum.IntEnum", "AutoName", "t.NamedTuple"):
            with self.subTest(base=base):
                old = f"class A({base}):\n    FLAG = 1\n"
                selected, _ = self.select(old, old.replace("1", "3"), {"A": [0]})
                self.assertEqual(selected, {11, 7, 23, 31})

    def test_implicit_dunder_consumers_keep_broad_fallback(self):
        old = "class A:\n    __match_args__ = ('a',)\n"
        selected, _ = self.select(old, old.replace("'a'", "'b'"), {"A": [0]})
        self.assertEqual(selected, {11, 7, 23, 31})

    FACTORY_SOURCE = (
        "def make(flag):\n"
        "    def callback():\n"
        "        return flag\n"
        "    return callback\n"
        "class A:\n"
        "    HANDLERS = {'x': make(True)}\n"
        "class B:\n"
        "    FLAG = 2\n"
    )

    def test_pure_callback_factory_keeps_class_users_and_readers(self):
        old = self.FACTORY_SOURCE
        selected, reasons = self.select(old, old.replace("make(True)", "make(False)"),
                                       {"A": [0], "B": [1]}, readers=[3])
        self.assertEqual(selected, {11, 31})
        self.assertIn("deferred callback factory", reasons[0])

    def test_factory_with_immediate_effect_keeps_broad_fallback(self):
        old = self.FACTORY_SOURCE.replace("    def callback():", "    effect(flag)\n    def callback():")
        selected, _ = self.select(old, old.replace("make(True)", "make(False)"), {"A": [0], "B": [1]})
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_factory_table_consumed_at_collection_keeps_broad_fallback(self):
        old = self.FACTORY_SOURCE
        selected, _ = self.select(old, old.replace("make(True)", "make(False)"),
                                  {"A": [0], "B": [1]}, global_attributes={"A": ["HANDLERS"]})
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_factory_invoked_from_bare_class_name_keeps_broad_fallback(self):
        old = self.FACTORY_SOURCE.replace("class B:", "    CACHED = HANDLERS['x']()\nclass B:")
        selected, _ = self.select(old, old.replace("make(True)", "make(False)"), {"A": [0], "B": [1]})
        self.assertEqual(selected, {11, 7, 23, 31})

    def test_factory_change_with_collection_source_reader_keeps_broad_fallback(self):
        old = self.FACTORY_SOURCE
        selected, _ = self.select(old, old.replace("make(True)", "make(False)"),
                                  {"A": [0], "B": [1]}, global_read=True)
        self.assertEqual(selected, {11, 7, 23, 31})


if __name__ == "__main__":
    unittest.main()
