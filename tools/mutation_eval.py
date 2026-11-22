#!/usr/bin/env python3
"""Independent development-only mutations and full-catalog ground truth.

Generated patches, labels and reports stay outside the submission repository.
The generator never reads sample answers. Freeze the corpus before measuring.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import difflib
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"

# Chosen from the pinned source before any mutation result was measured.
# One tuning and two held-out locations per category; public sample paths are
# excluded separately. These edits are evaluation inputs, never runtime maps.
SPECS = [
    ("tune-class", "class_data", "tuning", "sqlglot/generators/duckdb.py", "    SUPPORTS_CREATE_TABLE_LIKE = False", "    SUPPORTS_CREATE_TABLE_LIKE = True"),
    ("hold-class-a", "class_data", "heldout", "sqlglot/generators/bigquery.py", "    SUPPORTS_TABLE_ALIAS_COLUMNS = False", "    SUPPORTS_TABLE_ALIAS_COLUMNS = True"),
    ("hold-class-b", "class_data", "heldout", "sqlglot/dialects/exasol.py", '    NULL_ORDERING = "nulls_are_last"', '    NULL_ORDERING = "nulls_are_large"'),
    ("tune-cache", "import_cached_config", "tuning", "sqlglot/dialects/mysql.py", '        "%i": "%M",', '        "%i": "%m",'),
    ("hold-cache-a", "import_cached_config", "heldout", "sqlglot/dialects/postgres.py", '        "HH24": "%H",', '        "HH24": "%I",'),
    ("hold-cache-b", "import_cached_config", "heldout", "sqlglot/dialects/bigquery.py", '        IDENTIFIERS = ["`"]', '        IDENTIFIERS = ["["]'),
    ("tune-predicate", "function_predicate", "tuning", "sqlglot/time.py", "        if subsecond_digit_count > 3:", "        if subsecond_digit_count >= 3:"),
    ("hold-predicate-a", "function_predicate", "heldout", "sqlglot/trie.py", "    if not key:", "    if key:"),
    ("hold-predicate-b", "function_predicate", "heldout", "sqlglot/optimizer/normalize_identifiers.py", "        if isinstance(node, exp.Identifier):", "        if not isinstance(node, exp.Identifier):"),
    ("tune-callback", "callback", "tuning", "sqlglot/parsers/redshift.py", '        "CONVERT_TIMEZONE": lambda args: build_convert_timezone(args, "UTC"),', '        "CONVERT_TIMEZONE": lambda args: build_convert_timezone(args, "GMT"),'),
    ("hold-callback-a", "callback", "heldout", "sqlglot/generators/postgres.py", '        exp.DateAdd: _date_add_sql("+"),', '        exp.DateAdd: _date_add_sql("-"),'),
    ("hold-callback-b", "callback", "heldout", "sqlglot/generators/mysql.py", '        exp.SessionUser: lambda *_: "SESSION_USER()",', '        exp.SessionUser: lambda *_: "CURRENT_USER()",'),
    ("tune-fixture", "fixture", "tuning", "tests/fixtures/optimizer/normalize.sql", "(A OR B) AND (B OR C) AND (E OR F);\n(A OR B) AND (B OR C) AND (E OR F);", "(A AND B) AND (B OR C) AND (E OR F);\n(A OR B) AND (B OR C) AND (E OR F);"),
    ("hold-fixture-a", "fixture", "heldout", "tests/fixtures/optimizer/quote_identifiers.sql", 'SELECT a FROM x;\nSELECT "a" FROM "x";', 'SELECT a FROM x;\nSELECT "wrong" FROM "x";'),
    ("hold-fixture-b", "fixture", "heldout", "tests/fixtures/optimizer/canonicalize.sql", 'SELECT CONCAT("w"."d", "w"."e") AS "c" FROM "w" AS "w";', 'SELECT CONCAT("w"."d", "w"."e") AS "wrong" FROM "w" AS "w";'),
    ("tune-harmless", "harmless", "tuning", "sqlglot/anonymize.py", "", "\n# Independent evaluation: source comment alpha.\n"),
    ("hold-harmless-a", "harmless", "heldout", "sqlglot/jsonpath.py", "", "\n# Independent evaluation: source comment beta.\n"),
    ("hold-harmless-b", "harmless", "heldout", "sqlglot/lineage.py", "", "\n# Independent evaluation: source comment gamma.\n"),
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def public_paths(directory):
    result = set()
    for patch in directory.glob("*.patch"):
        for line in patch.read_text().splitlines():
            if line.startswith(("--- a/", "+++ b/")):
                result.add(line[6:])
    return result


def freeze(source, corpus, catalog, sample_commits):
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if commit != PIN:
        raise ValueError("source is not the pinned checkout")
    excluded = public_paths(sample_commits)
    cases = []
    patches = {}
    for case_id, category, split, relative, before, after in SPECS:
        if relative in excluded:
            raise ValueError(f"public sample path reused: {relative}")
        original = (source / relative).read_text()
        if before and original.count(before) != 1:
            raise ValueError(f"anchor must occur once: {case_id}: {original.count(before)}")
        modified = original.replace(before, after, 1) if before else original + after
        if relative.endswith(".py"):
            compile(modified, relative, "exec", dont_inherit=True)
            equivalent = ast.dump(ast.parse(original), include_attributes=False) == ast.dump(ast.parse(modified), include_attributes=False)
        else:
            equivalent = None
        patch = "".join(difflib.unified_diff(original.splitlines(keepends=True), modified.splitlines(keepends=True), fromfile="a/" + relative, tofile="b/" + relative))
        patches[case_id] = patch
        cases.append({"id": case_id, "category": category, "split": split, "path": relative,
                      "before": before, "after": after, "original_sha256": sha(original.encode()),
                      "modified_sha256": sha(modified.encode()), "patch_sha256": sha(patch.encode()),
                      "equivalent_ast": equivalent})
    manifest = {"version": 1, "source_commit": commit, "catalog_sha256": sha(catalog.read_bytes()),
                "sample_paths_excluded": sorted(excluded), "cases": cases,
                "split_policy": "Frozen before labels: one tuning and two heldout cases per category.",
                "generator_uses_sample_answers": False}
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    target = corpus / "manifest.json"
    if target.exists() and target.read_text() != encoded:
        raise ValueError("frozen manifest differs; use a new corpus directory")
    corpus.mkdir(parents=True, exist_ok=True)
    target.write_text(encoded)
    (corpus / "patches").mkdir(exist_ok=True)
    for case_id, content in patches.items():
        (corpus / "patches" / (case_id + ".patch")).write_text(content)
    shutil.copyfile(catalog, corpus / "tests.json")
    (corpus / "manifest.sha256").write_text(sha(encoded.encode()) + "\n")
    print(json.dumps({"manifest_sha256": sha(encoded.encode()), "cases": len(cases), "splits": dict(Counter(c["split"] for c in cases))}), flush=True)


def load_manifest(corpus):
    raw = (corpus / "manifest.json").read_bytes()
    if sha(raw) != (corpus / "manifest.sha256").read_text().strip():
        raise ValueError("frozen manifest checksum changed")
    return json.loads(raw)


def prepare_copy(source, destination, case):
    if destination.exists():
        raise ValueError(f"isolated source already exists: {destination}")
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".mypy_cache", "*.pyc", ".coverage*"))
    if case:
        path = destination / case["path"]
        original = path.read_text()
        if sha(original.encode()) != case["original_sha256"]:
            raise ValueError("source drifted after corpus freeze")
        changed = original.replace(case["before"], case["after"], 1) if case["before"] else original + case["after"]
        if sha(changed.encode()) != case["modified_sha256"]:
            raise ValueError("mutation hash differs from frozen manifest")
        path.write_text(changed)


class Reports:
    def __init__(self, catalog):
        self.catalog = {test["id"]: test for test in catalog}
        self.outcomes = {}
        self.collection_errors = []
        self.collected = set()

    def pytest_collection_modifyitems(self, config, items):
        selected, deselected = [], []
        for item in items:
            (selected if item.nodeid in self.catalog else deselected).append(item)
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)
        self.collected = {item.nodeid for item in selected}

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append({"nodeid": report.nodeid, "reason": str(report.longrepr)[-12000:]})

    def pytest_runtest_logreport(self, report):
        row = self.outcomes.setdefault(report.nodeid, {"failed": False, "phases": {}})
        row["failed"] |= report.failed
        row["phases"].setdefault(report.when, []).append(report.outcome)
        if report.failed:
            row.setdefault("failure_reasons", []).append(str(report.longrepr)[-12000:])

    def finish(self):
        for error in self.collection_errors:
            path = error["nodeid"].split("::", 1)[0].rstrip("/")
            for nodeid, test in self.catalog.items():
                if test["file"] == path or test["file"].startswith(path + "/"):
                    self.outcomes[nodeid] = {"failed": True, "phases": {"collection": ["failed"]}}
        missing = sorted(set(self.catalog) - set(self.outcomes))
        return {"complete": not missing, "catalog_tests": len(self.catalog), "collected_catalog_tests": len(self.collected),
                "reported_catalog_tests": len(set(self.catalog).intersection(self.outcomes)), "unaccounted": missing,
                "failing_test_numbers": sorted(test["number"] for nodeid, test in self.catalog.items() if self.outcomes.get(nodeid, {}).get("failed")),
                "collection_errors": self.collection_errors, "outcomes": self.outcomes}


def child(source, catalog_path, report_path, test_timeout):
    # Native launchers honor this evaluator-only barrier between cases, so
    # selector CPU measurements can run without interrupting a pytest suite.
    pause = catalog_path.parent / "PAUSE_GROUND_TRUTH"
    paused = report_path.with_suffix(".paused")
    if pause.exists():
        paused.write_text(str(os.getpid()))
        while pause.exists():
            time.sleep(0.2)
        paused.unlink(missing_ok=True)
    import pytest
    os.chdir(source)
    sys.path.insert(0, str(source))
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    recorder = Reports(json.loads(catalog_path.read_text())["tests"])
    start = time.monotonic()
    code = pytest.main(["tests", "-q", "--tb=short", "--disable-warnings", "--continue-on-collection-errors", "-p", "no:cacheprovider", f"--timeout={test_timeout}"], plugins=[recorder])
    result = recorder.finish()
    result.update({"pytest_exit_code": int(code), "wall_seconds": round(time.monotonic() - start, 3),
                   "environment": {"python": sys.version, "platform": platform.platform(), "machine": platform.machine(),
                                   "sqlite": sqlite3.sqlite_version, "pytest": pytest.__version__,
                                   "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
                                   "skip_integration": os.environ["SKIP_INTEGRATION"]}})
    write_json(report_path, result)
    return 0 if result["complete"] and int(code) in (0, 1, 2) else 2


def run(args):
    corpus, source = args.corpus.resolve(), args.source.resolve()
    manifest = load_manifest(corpus)
    if args.case == "baseline":
        cases = [None]
    elif args.case:
        cases = [next(case for case in manifest["cases"] if case["id"] == args.case)]
    else:
        cases = [case for case in manifest["cases"] if args.split == "all" or case["split"] == args.split]
    for case in cases:
        name = case["id"] if case else "baseline"
        run_dir = corpus / "runs" / args.backend / name
        result_path = run_dir / "ground-truth.json"
        if result_path.exists():
            result = json.loads(result_path.read_text())
            if result.get("complete"):
                print(f"{args.backend} {name}: already complete", flush=True)
                continue
            raise ValueError(f"incomplete report exists; preserve it and use a new run directory: {result_path}")
        run_dir.mkdir(parents=True, exist_ok=True)
        copied = run_dir / "source"
        prepare_copy(source, copied, case)
        if args.backend == "native":
            command = [str(args.python.absolute()), "-B", str(Path(__file__).resolve()), "child", "--source", str(copied), "--catalog", str(corpus / "tests.json"), "--report", str(result_path), "--test-timeout", str(args.test_timeout)]
        else:
            command = ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none", "--cpus", "2", "--memory", "4096m", "--memory-swap", "4096m", "--pids-limit", "256", "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65534:65534", "--env", "HOME=/tmp", "--volume", str(copied) + ":/source:ro", "--volume", str(corpus / "tests.json") + ":/catalog.json:ro", "--volume", str(Path(__file__).resolve()) + ":/eval.py:ro", "--volume", str(run_dir) + ":/results:rw", "--workdir", "/source", "--entrypoint", "python", args.image, "-B", "/eval.py", "child", "--source", "/source", "--catalog", "/catalog.json", "--report", "/results/ground-truth.json", "--test-timeout", str(args.test_timeout)]
            run_dir.chmod(0o777)
        print(f"START {args.backend} {name}", flush=True)
        with (run_dir / "pytest.log").open("w") as log:
            proc = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=args.batch_timeout,
                                  env={**os.environ, "SKIP_INTEGRATION": "0", "PYTHONDONTWRITEBYTECODE": "1"})
        if not result_path.exists():
            raise RuntimeError(f"pytest did not produce a report: {name}; see {run_dir / 'pytest.log'}")
        result = json.loads(result_path.read_text())
        print(f"DONE {args.backend} {name}: complete={result['complete']} failures={len(result['failing_test_numbers'])} exit={proc.returncode} seconds={result['wall_seconds']}", flush=True)
        if not result["complete"]:
            raise RuntimeError(f"incomplete catalog ground truth: {name}")
        if case is None and (result["failing_test_numbers"] or result["pytest_exit_code"]):
            raise RuntimeError("baseline is not healthy; mutation labels cannot be trusted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    frozen = subs.add_parser("freeze")
    frozen.add_argument("--source", type=Path, default=ROOT.parent / "sqlglot")
    frozen.add_argument("--corpus", type=Path, required=True)
    frozen.add_argument("--catalog", type=Path, default=ROOT / "sample/tests.json")
    frozen.add_argument("--sample-commits", type=Path, default=ROOT / "sample/commits")
    runner = subs.add_parser("run")
    runner.add_argument("--source", type=Path, default=ROOT.parent / "sqlglot")
    runner.add_argument("--corpus", type=Path, required=True)
    runner.add_argument("--backend", choices=("native", "docker"), default="native")
    runner.add_argument("--python", type=Path, default=ROOT.parent.parent / ".venv/bin/python")
    runner.add_argument("--image", default="blast-radius-eval:phase2")
    runner.add_argument("--split", choices=("tuning", "heldout", "all"), default="heldout")
    runner.add_argument("--case")
    runner.add_argument("--test-timeout", type=int, default=90)
    runner.add_argument("--batch-timeout", type=int, default=1800)
    worker = subs.add_parser("child")
    worker.add_argument("--source", type=Path, required=True)
    worker.add_argument("--catalog", type=Path, required=True)
    worker.add_argument("--report", type=Path, required=True)
    worker.add_argument("--test-timeout", type=int, default=90)
    args = parser.parse_args()
    if args.command == "freeze":
        freeze(args.source.resolve(), args.corpus.resolve(), args.catalog.resolve(), args.sample_commits.resolve())
    elif args.command == "run":
        run(args)
    else:
        raise SystemExit(child(args.source.resolve(), args.catalog.resolve(), args.report.resolve(), args.test_timeout))


if __name__ == "__main__":
    main()
