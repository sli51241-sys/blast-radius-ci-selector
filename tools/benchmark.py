#!/usr/bin/env python3
"""Compare the starter and selectors with the official sample scorer.

This evaluation tool reads sample labels; the submission program never does.
"""
from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from score import evaluate_run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "sample")
    parser.add_argument("--source", type=Path, default=ROOT.parent / "sqlglot")
    args = parser.parse_args()
    data, source = args.data.resolve(), args.source.resolve()
    tests = json.loads((data / "tests.json").read_text())["tests"]
    answers = json.loads((data / "answers.json").read_text())["commits"]
    output = ROOT / "benchmarks"
    output.mkdir(exist_ok=True)
    modes = {
        "starter": [sys.executable, "-c", "import sys; from pathlib import Path; import starter_selector as s; s.DATA=Path(sys.argv[1]); s.main()", str(data)],
        "scheduled_all": [sys.executable, "selector.py", "--data", str(data), "--source", str(source), "--all-tests"],
        "coverage": [sys.executable, "selector.py", "--data", str(data), "--source", str(source)],
    }
    report = {"dataset": "official 30-commit sample", "cpu_note": "Local selector CPU only; official scores use server CPU and median runs.", "results": {}}
    for name, command in modes.items():
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        start = time.monotonic()
        result = subprocess.run(command, cwd=ROOT, capture_output=True, check=True)
        wall = time.monotonic() - start
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
        (output / f"{name}.jsonl").write_bytes(result.stdout)
        (output / f"{name}.log").write_bytes(result.stderr)
        scored = evaluate_run(result.stdout, tests, answers)
        if not scored["ok"]:
            raise RuntimeError(f"{name}: {scored}")
        entry = {
            "simulated_seconds": scored["simulated_milliseconds"] / 1000,
            "missed_commits": scored["missed_commits"],
            "local_selector_cpu_seconds": round(cpu, 6),
            "local_selector_wall_seconds": round(wall, 6),
        }
        report["results"][name] = entry
        print(f"{name:15s} {entry['simulated_seconds']:10.3f} simulated seconds, {len(entry['missed_commits'])} misses, {cpu:.3f} local CPU seconds")
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
