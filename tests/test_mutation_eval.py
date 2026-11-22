from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import mutation_eval as evaluation


class GroundTruthReportTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [
            {"id": "tests/test_a.py::test_one", "file": "tests/test_a.py", "number": 7},
            {"id": "tests/test_a.py::test_two", "file": "tests/test_a.py", "number": 19},
            {"id": "tests/test_b.py::test_other", "file": "tests/test_b.py", "number": 31},
        ]

    def log(self, recorder, node, outcome, when="call"):
        recorder.pytest_runtest_logreport(SimpleNamespace(
            nodeid=node, failed=outcome == "failed", when=when,
            outcome=outcome, longrepr="synthetic failure"))

    def test_failed_subtest_survives_parent_pass(self):
        reports = evaluation.Reports(self.catalog)
        for test in self.catalog:
            self.log(reports, test["id"], "passed")
        self.log(reports, self.catalog[0]["id"], "failed")
        self.log(reports, self.catalog[0]["id"], "passed")
        result = reports.finish()
        self.assertTrue(result["complete"])
        self.assertEqual(result["failing_test_numbers"], [7])

    def test_collection_failure_expands_to_whole_test_file(self):
        reports = evaluation.Reports(self.catalog)
        reports.pytest_collectreport(SimpleNamespace(
            failed=True, nodeid="tests/test_a.py", longrepr="ImportError"))
        self.log(reports, self.catalog[2]["id"], "passed")
        result = reports.finish()
        self.assertTrue(result["complete"])
        self.assertEqual(result["failing_test_numbers"], [7, 19])

    def test_missing_tests_make_ground_truth_incomplete(self):
        reports = evaluation.Reports(self.catalog)
        self.log(reports, self.catalog[0]["id"], "passed")
        self.assertFalse(reports.finish()["complete"])
        self.assertEqual(len(reports.finish()["unaccounted"]), 2)

    def test_setup_error_and_timeout_are_failures(self):
        reports = evaluation.Reports(self.catalog)
        self.log(reports, self.catalog[0]["id"], "failed", "setup")
        self.log(reports, self.catalog[1]["id"], "failed", "call")
        self.log(reports, self.catalog[2]["id"], "skipped", "setup")
        result = reports.finish()
        self.assertTrue(result["complete"])
        self.assertEqual(result["failing_test_numbers"], [7, 19])

    def test_only_non_catalog_items_are_deselected(self):
        reports = evaluation.Reports(self.catalog)
        items = [SimpleNamespace(nodeid=self.catalog[0]["id"]), SimpleNamespace(nodeid="tests/test_extra.py::test_new")]
        removed = []
        config = SimpleNamespace(hook=SimpleNamespace(pytest_deselected=lambda items: removed.extend(items)))
        reports.pytest_collection_modifyitems(config, items)
        self.assertEqual([item.nodeid for item in items], [self.catalog[0]["id"]])
        self.assertEqual(len(removed), 1)


class FrozenCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.samples = self.root / "sample-patches"
        self.samples.mkdir()
        self.catalog = self.root / "tests.json"
        self.catalog.write_text('{"tests": []}')
        (self.source / "fresh.py").write_text("value = 1\n")
        self.spec = [("holdout", "class_data", "heldout", "fresh.py", "value = 1", "value = 2")]

    def freeze(self):
        with patch.object(evaluation, "SPECS", self.spec), patch.object(evaluation.subprocess, "check_output", return_value=evaluation.PIN):
            evaluation.freeze(self.source, self.root / "corpus", self.catalog, self.samples)

    def test_freezing_leaves_original_source_unchanged(self):
        self.freeze()
        self.assertEqual((self.source / "fresh.py").read_text(), "value = 1\n")
        self.assertEqual(len(evaluation.load_manifest(self.root / "corpus")["cases"]), 1)

    def test_public_sample_source_paths_are_excluded(self):
        (self.samples / "known.patch").write_text("--- a/fresh.py\n+++ b/fresh.py\n")
        with self.assertRaisesRegex(ValueError, "public sample path reused"):
            self.freeze()

    def test_frozen_manifest_cannot_be_adapted_after_measurement(self):
        self.freeze()
        self.spec[0] = ("holdout", "class_data", "heldout", "fresh.py", "value = 1", "value = 3")
        with self.assertRaisesRegex(ValueError, "frozen manifest differs"):
            self.freeze()


if __name__ == "__main__":
    unittest.main()
