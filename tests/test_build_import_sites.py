"""Synthetic collection proof: global reads are observed, test bodies never run."""
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.environ.get("CLASS_USAGE_TOOL"), "set CLASS_USAGE_TOOL to the existing instrumentation")
class CollectionMeasurementTests(unittest.TestCase):
    def test_collection_cached_value_and_repeated_site_are_both_recorded(self):
        proposal = Path(__file__).resolve().parents[1] / "tools"
        with tempfile.TemporaryDirectory(dir=proposal) as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / "sqlglot").mkdir(parents=True)
            (source / "tests").mkdir()
            (source / "sqlglot/__init__.py").write_text("")
            (source / "tests/__init__.py").write_text("")
            (source / "sqlglot/core.py").write_text("class A:\n    FIELD = {'x': 1}\n")
            (source / "tests/test_probe.py").write_text(
                "from sqlglot.core import A\n"
                "CACHED = A.FIELD['x']\n"
                "KEY_EXISTS = 'x' in A.FIELD\n"
                "def test_body_must_never_run():\n"
                "    raise AssertionError('collection tool executed a test')\n"
            )
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "add", "sqlglot", "tests"], check=True)
            subprocess.run(["git", "-C", str(source), "-c", "user.name=Baseline Fixture",
                            "-c", "user.email=baseline@example.invalid", "commit", "-qm", "synthetic baseline"], check=True)
            commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
            output = root / "sites.json.gz"
            result = subprocess.run(
                [sys.executable, "-B", str(proposal / "build_import_sites.py"),
                 "--source", str(source), "--class-usage-tool", os.environ["CLASS_USAGE_TOOL"],
                 "--commit", commit, "--output", str(output)],
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            metadata = json.loads(gzip.decompress(output.read_bytes()))
            self.assertTrue(metadata["complete"])
            self.assertTrue(metadata["collection_complete"])
            self.assertEqual(metadata["collection"]["tests_collected"], 1)
            self.assertEqual(metadata["collection"]["execution_attempts"], 0)
            self.assertEqual(metadata["collection"]["unobserved_modules"], [])
            self.assertEqual(metadata["sites"]["sqlglot/core.py"]["A"]["FIELD"], [
                {"path": "tests/test_probe.py", "line": 2},
                {"path": "tests/test_probe.py", "line": 3},
            ])
            self.assertIn("tests/test_probe.py", metadata["source_sha256"])


if __name__ == "__main__":
    unittest.main()
