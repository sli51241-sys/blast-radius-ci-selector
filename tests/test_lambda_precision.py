"""Synthetic integration checks for deferred callbacks stored in class data."""

import difflib
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from selector import Selector


class LambdaPrecisionTests(unittest.TestCase):
    OLD = "class A:\n    HANDLERS = {\n        'key': lambda value: (\n            value + 1\n        ),\n    }\nclass B:\n    FLAG = 2\n"

    def select(self, new, *, executed_at_collection=False, readers=(), covered=True,
               class_metadata=True, import_safe=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pkg").mkdir()
            (root / "pkg/core.py").write_text(self.OLD)
            tests = [{"number": n, "id": f"tests/test_{i}.py::test", "file": f"tests/test_{i}.py"}
                     for i, n in enumerate([11, 7, 23, 31])]
            globals_ = [1, 2, 6, 7, 8] + ([4] if executed_at_collection else [])
            files = {"pkg/core.py": {"global": globals_, "reads": list(readers),
                     "lines": {str(line): [0] if covered and 3 <= line <= 5 else [2]
                               for line in range(1, 9)}}}
            if not covered:
                for line in range(3, 6):
                    files["pkg/core.py"]["lines"][str(line)] = []
            data = {"tests": [test["id"] for test in tests], "files": files}
            if class_metadata:
                data["class_usage"] = {"pkg/core.py": {"A": [0, 1], "B": [2]}}
            map_path = root / "coverage.json"
            map_path.write_text(json.dumps(data))
            (root / "symbols.json.gz").write_bytes(gzip.compress(json.dumps({
                "attributes": {}, "dynamic_attributes": {},
            }).encode()))
            change = "".join(difflib.unified_diff(
                self.OLD.splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile="a/pkg/core.py", tofile="b/pkg/core.py"))
            with patch.object(Selector, "_class_import_safe", return_value=import_safe):
                selected, reasons = Selector(tests, root, map_path).select(change)
            return {test["number"] for test in selected}, reasons

    def test_body_edit_keeps_class_users_and_source_readers(self):
        selected, reasons = self.select(self.OLD.replace("value + 1", "value + 3"), readers=[3])
        self.assertEqual(selected, {11, 7, 31})
        self.assertIn("deferred callback", reasons[0])

    def test_collection_execution_keeps_fallback(self):
        selected, _ = self.select(self.OLD.replace("value + 1", "value + 3"), executed_at_collection=True)
        self.assertEqual(selected, {11, 23})

    def test_table_key_change_alongside_body_keeps_fallback(self):
        selected, _ = self.select(self.OLD.replace("value + 1", "value + 3").replace("'key'", "'other'"))
        self.assertEqual(selected, {11, 23})

    def test_lambda_default_change_keeps_fallback(self):
        selected, _ = self.select(self.OLD.replace("lambda value:", "lambda value=3:"))
        self.assertEqual(selected, {11, 23})

    def test_missing_class_metadata_keeps_fallback(self):
        selected, _ = self.select(self.OLD.replace("value + 1", "value + 3"), class_metadata=False)
        self.assertEqual(selected, {11, 23})

    def test_missing_callback_coverage_keeps_fallback(self):
        selected, _ = self.select(self.OLD.replace("value + 1", "value + 3"), covered=False)
        self.assertEqual(selected, {23})

    def test_import_failure_selects_all(self):
        selected, _ = self.select(self.OLD.replace("value + 1", "value + 3"), import_safe=False)
        self.assertEqual(selected, {11, 7, 23, 31})


if __name__ == "__main__":
    unittest.main()
