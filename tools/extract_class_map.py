#!/usr/bin/env python3
"""Extract baseline class observations without replacing the line coverage map.

Run build_coverage.py --class-usage first. Only baseline metadata and observed
users are retained; this tool never reads patches or labeled sample answers.
"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    with gzip.open(options.input, "rt", encoding="utf-8") as handle:
        measured = json.load(handle)
    if measured.get("exit_code") != 0 or not measured.get("class_usage"):
        parser.error("input must be a successful baseline run with class usage")
    contexts = ["", "@global", "@collect:*"]
    if measured.get("class_usage_import_contexts") != contexts:
        parser.error("rebuild input with collection-time class dependency recording")
    data = {key: measured[key] for key in (
        "commit", "tests", "class_usage", "class_global_usage", "attribute_global_usage",
        "exit_code", "environment", "instrumentation",
    )}
    data["version"] = 1
    data["import_contexts"] = contexts
    data["baseline_passed_tests"] = sum(
        outcome.get("call") == "passed" for outcome in measured["outcomes"].values()
    )
    options.output.parent.mkdir(parents=True, exist_ok=True)
    with options.output.open("wb") as stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as archive:
            archive.write(json.dumps(data, separators=(",", ":")).encode())
    print(f"Extracted {len(data['tests'])} baseline tests into {options.output}")


if __name__ == "__main__":
    main()
