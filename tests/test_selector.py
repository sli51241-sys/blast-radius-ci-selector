"""Synthetic source/coverage cases; no sample patches or answer labels."""

import difflib
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from selector import Selector


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.tests = [{"number": n, "id": f"tests/test_{n}.py::test_case", "file": f"tests/test_{n}.py", "seconds": 0.01} for n in range(4)]
        self.files = {}
        self.imports = {}
        self.runtime_imports = {}
        self.symbols = {}

    def write(self, path, text, *, lines=None, reads=(), **extra):
        target = self.source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        self.files[path] = {"lines": lines or {}, "reads": list(reads), "global": [], **extra}

    def select(self, path, old, new):
        map_path = self.root / "coverage.json"
        map_path.write_text(json.dumps({"tests": [t["id"] for t in self.tests[:4]], "files": self.files, "imports": self.imports, "runtime_imports": self.runtime_imports}))
        if self.symbols:
            with gzip.open(self.root / "symbols.json.gz", "wt") as f:
                json.dump(self.symbols, f)
        patch = "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile="a/" + path, tofile="b/" + path))
        selected, reasons = Selector(self.tests, self.source, map_path).select(patch)
        return {t["number"] for t in selected}, reasons

    def test_changed_function_selects_coverage_and_raw_source_readers(self):
        old = "def alpha():\n    value = 1\n    return value\n\ndef beta():\n    return 2\n"
        self.write("pkg/core.py", old, lines={"2": [0], "3": [0], "6": [1]}, reads=[2])
        selected, _ = self.select("pkg/core.py", old, old.replace("value = 1", "value = 3"))
        self.assertEqual(selected, {0, 2})

    def test_function_end_insertion_boundary_keeps_function_users(self):
        old = "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
        self.write("pkg/core.py", old, lines={"2": [0], "5": [1]}, reads=[2])
        selected, _ = self.select("pkg/core.py", old, old.replace("return 1", "return 3"))
        self.assertEqual(selected, {0, 2})

    def test_definition_change_includes_measured_runtime_import_consumer(self):
        old = "def alpha(value=1):\n    return value\n"
        self.write("pkg/core.py", old, lines={"2": [0]})
        self.write("pkg/lazy.py", "def lazy():\n    from pkg import core\n", lines={"2": [3]})
        self.runtime_imports = {"pkg/lazy.py": {"pkg/core.py": [2]}}
        selected, _ = self.select("pkg/core.py", old, old.replace("value=1", "value=2"))
        self.assertTrue({0, 3}.issubset(selected))

    def test_signature_change_includes_transitive_import_consumers(self):
        old = "def alpha(value=1):\n    return value\n"
        self.write("pkg/core.py", old, lines={"2": [0]})
        self.imports = {"pkg/helper.py": ["pkg/core.py"], "tests/test_3.py": ["pkg/helper.py"]}
        selected, _ = self.select("pkg/core.py", old, old.replace("value=1", "value=2"))
        self.assertTrue({0, 3}.issubset(selected))

    def test_syntax_error_selects_every_test(self):
        old = "def alpha():\n    return 1\n"
        self.write("pkg/core.py", old, lines={"2": [0]})
        selected, _ = self.select("pkg/core.py", old, old.replace("return 1", "return ("))
        self.assertEqual(selected, {0, 1, 2, 3})

    def test_class_field_change_includes_attribute_consumers(self):
        old = "class Settings:\n    FLAG = 1\n"
        self.write("pkg/core.py", old, lines={"2": [0]})
        self.write("pkg/consumer.py", "def use():\n    return Settings.FLAG\n", lines={"2": [1]})
        self.symbols = {"attributes": {"FLAG": {"pkg/consumer.py": [2]}}, "dynamic_attributes": {}}
        selected, _ = self.select("pkg/core.py", old, old.replace("FLAG = 1", "FLAG = 2"))
        self.assertTrue({0, 1}.issubset(selected))

    def test_fixture_change_selects_observed_readers(self):
        old = "SELECT 1;\n"
        self.write("tests/fixtures/query.sql", old, reads=[1])
        selected, _ = self.select("tests/fixtures/query.sql", old, "SELECT 2;\n")
        self.assertEqual(selected, {1})

    def test_ast_equivalent_edit_still_selects_source_readers(self):
        old = "def alpha():\n    return 1\n"
        self.write("pkg/core.py", old, lines={"2": [0]}, reads=[2])
        selected, _ = self.select("pkg/core.py", old, old.replace("return 1", "return 1  # explanatory comment"))
        self.assertEqual(selected, {2})

    def test_unknown_fixture_conservatively_selects_every_test(self):
        self.write("known.txt", "hello\n", reads=[0])
        selected, _ = self.select("unknown.txt", "a\n", "b\n")
        self.assertEqual(selected, {0, 1, 2, 3})

    def test_test_missing_from_baseline_map_is_always_selected(self):
        old = "def alpha():\n    return 1\n"
        self.write("pkg/core.py", old, lines={"2": [0]})
        self.tests.append({"number": 10, "id": "tests/test_new.py::test_new", "file": "tests/test_new.py", "seconds": 1})
        selected, _ = self.select("pkg/core.py", old, old.replace("return 1", "return 2"))
        self.assertEqual(selected, {0, 10})


if __name__ == "__main__":
    unittest.main()
