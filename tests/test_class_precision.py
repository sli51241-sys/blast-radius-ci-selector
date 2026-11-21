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

    def select(self, old, new, class_usage, *, readers=(), import_safe=True):
        (self.source / self.path).write_text(old, encoding="utf-8")
        # Every module-level line was executed at collection. That alone must
        # not turn a qualified class-data edit into all test users of the file.
        files = {self.path: {
            "lines": {str(n): list(range(4)) for n in range(1, len(old.splitlines()) + 1)},
            "global": list(range(1, len(old.splitlines()) + 1)),
            "reads": list(readers),
        }}
        data = {"tests": [test["id"] for test in self.tests], "files": files, "imports": {}}
        if class_usage is not None:
            data["class_usage"] = {self.path: class_usage}
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


if __name__ == "__main__":
    unittest.main()
