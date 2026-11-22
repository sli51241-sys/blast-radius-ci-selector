#!/usr/bin/env python3
"""Evaluate the sample's exact zero-miss, failing-tests-only CI cost oracle.

EVALUATION ONLY: this tool reads sample answers. It is not imported by the
selector, copied into its Docker image, or used to produce runtime predictions.
All arithmetic uses scorer-compatible integer milliseconds. LPT supplies upper
bounds; load lower bounds and, when needed, exhaustive partition feasibility
search prove the optimum. Selector CPU is outside this oracle.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
WORKER_MS = 2_000
MAX_WORKERS = 8


def duration_ms(seconds: float) -> int:
    return math.floor(seconds * 1_000 + 0.5)


def _lpt(jobs: list[tuple[int, int]], count: int) -> tuple[int, list[list[int]]]:
    # Seed every worker, including zero-duration jobs, to model exactly count
    # nonempty workers rather than accidentally paying for fewer workers.
    workers = [[number] for number, _ in jobs[:count]]
    loads = [duration for _, duration in jobs[:count]]
    for number, duration in jobs[count:]:
        index = min(range(count), key=lambda i: (loads[i], i))
        workers[index].append(number)
        loads[index] += duration
    return max(loads, default=0), workers


def _feasible(jobs: list[tuple[int, int]], count: int, capacity: int):
    """Exact bin feasibility with only symmetry and failed-state pruning."""
    if (jobs and jobs[0][1] > capacity) or sum(d for _, d in jobs) > count * capacity:
        return None, 0
    workers, loads = [[] for _ in range(count)], [0] * count
    failed = set()
    nodes = 0

    def place(index):
        nonlocal nodes
        nodes += 1
        if index == len(jobs):
            return True
        state = (index, tuple(sorted(loads)))
        if state in failed:
            return False
        number, duration = jobs[index]
        seen_loads = set()
        for worker in sorted(range(count), key=lambda i: (-loads[i], i)):
            load = loads[worker]
            if load in seen_loads or load + duration > capacity:
                continue
            seen_loads.add(load)
            loads[worker] += duration
            workers[worker].append(number)
            if place(index + 1):
                return True
            workers[worker].pop()
            loads[worker] = load
            if load == 0:
                break  # Every remaining empty worker is symmetric.
        failed.add(state)
        return False

    if not place(0):
        return None, nodes
    # A feasible packing may use fewer bins. With at least count jobs, split
    # groups to fill empty workers without increasing any load.
    for empty in [i for i, worker in enumerate(workers) if not worker]:
        donor = next(i for i, worker in enumerate(workers) if len(worker) > 1)
        workers[empty].append(workers[donor].pop())
    return workers, nodes


def _exact_makespan(jobs, count, lower, upper):
    checks = nodes = 0
    witness = None
    while lower < upper:
        capacity = (lower + upper) // 2
        packing, explored = _feasible(jobs, count, capacity)
        checks += 1
        nodes += explored
        if packing is None:
            lower = capacity + 1
        else:
            upper, witness = capacity, packing
    # Obtain a witness specifically at the proved minimum capacity.
    witness, explored = _feasible(jobs, count, lower)
    checks += 1
    nodes += explored
    if witness is None:
        raise AssertionError("inconsistent exact partition search")
    return lower, witness, checks, nodes


def exact_oracle(durations: dict[int, int], max_workers: int = MAX_WORKERS) -> dict:
    """Return a proved minimum over all legal nonempty worker counts."""
    if not 1 <= max_workers <= MAX_WORKERS:
        raise ValueError("max_workers must be from one through eight")
    if any(isinstance(d, bool) or not isinstance(d, int) or d < 0 for d in durations.values()):
        raise ValueError("durations must be nonnegative integer milliseconds")
    if not durations:
        return {"failing_count": 0, "failing_total_duration_ms": 0, "longest_failing_test_ms": 0,
                "minimum_cost_ms": 0, "minimum_cost_seconds": 0.0,
                "basic_lower_bound_ms": 0, "workers": [], "worker_loads_ms": [],
                "worker_counts": [], "proof": {"optimal": True, "method": "empty failing set",
                                                  "exact_capacity_checks": 0, "search_nodes": 0}}
    jobs = sorted(durations.items(), key=lambda item: (-item[1], item[0]))
    total, largest = sum(durations.values()), jobs[0][1]
    candidates = []
    for count in range(1, min(max_workers, len(jobs)) + 1):
        lower = max(largest, (total + count - 1) // count)
        upper, packing = _lpt(jobs, count)
        candidates.append({"worker_count": count, "makespan_lower_bound_ms": lower,
                           "total_lower_bound_ms": lower + WORKER_MS * count,
                           "lpt_makespan_ms": upper, "lpt_total_ms": upper + WORKER_MS * count,
                           "packing": packing})
    winner = min(candidates, key=lambda c: (c["lpt_total_ms"], c["worker_count"]))
    best, best_count, best_workers = winner["lpt_total_ms"], winner["worker_count"], winner["packing"]
    checks = nodes = 0
    for candidate in sorted(candidates, key=lambda c: (c["total_lower_bound_ms"], c["worker_count"])):
        count = candidate["worker_count"]
        lower, upper = candidate["makespan_lower_bound_ms"], candidate["lpt_makespan_ms"]
        if lower == upper:
            candidate["proof"] = "load lower bound attained"
            candidate["exact_minimum_makespan_ms"] = lower
            continue
        if (candidate["total_lower_bound_ms"] > best
                or candidate["total_lower_bound_ms"] == best and count >= best_count):
            candidate["proof"] = "lower bound cannot improve global incumbent or worker-count tie"
            continue
        makespan, packing, calls, explored = _exact_makespan(jobs, count, lower, upper)
        checks, nodes = checks + calls, nodes + explored
        candidate["proof"] = "exhaustive capacity feasibility search"
        candidate["exact_minimum_makespan_ms"] = makespan
        cost = makespan + WORKER_MS * count
        if (cost, count) < (best, best_count):
            best, best_count, best_workers = cost, count, packing
    for candidate in candidates:
        candidate.pop("packing")
    basic = min(c["total_lower_bound_ms"] for c in candidates)
    loads = [sum(durations[number] for number in worker) for worker in best_workers]
    assert sorted(number for worker in best_workers for number in worker) == sorted(durations)
    assert max(loads) + WORKER_MS * len(best_workers) == best
    return {"failing_count": len(jobs), "failing_total_duration_ms": total,
            "longest_failing_test_ms": largest, "minimum_cost_ms": best, "minimum_cost_seconds": best / 1000,
            "basic_lower_bound_ms": basic, "workers": best_workers, "worker_loads_ms": loads,
            "worker_counts": candidates,
            "proof": {"optimal": True,
                      "method": "universal load lower bound attained" if best == basic else "all competitive worker counts resolved exactly",
                      "exact_capacity_checks": checks, "search_nodes": nodes}}


def build_report(data: Path, current_output: Path | None) -> dict:
    tests = json.loads((data / "tests.json").read_text())["tests"]
    answers = json.loads((data / "answers.json").read_text())["commits"]
    durations = {test["number"]: duration_ms(test["seconds"]) for test in tests}
    per_commit = {}
    for commit, answer in sorted(answers.items()):
        failing = answer["failing"]
        if len(failing) != len(set(failing)):
            raise ValueError(f"duplicate failing test in sample answer: {commit}")
        per_commit[commit] = exact_oracle({number: durations[number] for number in failing})
    total = sum(case["minimum_cost_ms"] for case in per_commit.values())
    # Independently validate the complete oracle schedule with the official
    # local scorer. This is evaluation-only use of labeled sample answers.
    sys.path.insert(0, str(ROOT))
    from score import evaluate_run

    output = "".join(json.dumps({"commit": commit, "workers": case["workers"]}) + "\n"
                     for commit, case in per_commit.items()).encode()
    scored = evaluate_run(output, tests, answers)
    if not scored["ok"] or scored["missed_commits"] or scored["simulated_milliseconds"] != total:
        raise AssertionError(f"oracle output failed independent scorer validation: {scored}")
    report = {"scope": "exact zero-miss sample oracle, selecting failing tests only; CPU excluded",
              "evaluation_only": True, "commit_count": len(per_commit),
              "maximum_workers": MAX_WORKERS, "startup_cost_per_worker_ms": WORKER_MS,
              "lower_bound_formula": "min_k(max(longest_failing_test_ms, ceil(failing_total_duration_ms / k)) + 2000 * k); empty failing set costs zero",
              "oracle_total_ms": total, "oracle_total_seconds": total / 1000,
              "all_cases_proven_optimal": all(c["proof"]["optimal"] for c in per_commit.values()),
              "cases_attaining_basic_lower_bound": sum(c["minimum_cost_ms"] == c["basic_lower_bound_ms"] for c in per_commit.values()),
              "exact_search_capacity_checks": sum(c["proof"]["exact_capacity_checks"] for c in per_commit.values()),
              "official_scorer_validation": {"valid": True, "missed_commits": [], "simulated_seconds": total / 1000},
              "per_commit": per_commit}
    if current_output:
        current_bytes = current_output.read_bytes()
        current = evaluate_run(current_bytes, tests, answers)
        if not current["ok"]:
            raise ValueError(f"current output is invalid: {current}")
        current_total = current["simulated_milliseconds"]
        report.update({"current_total_ms": current_total, "current_total_seconds": current_total / 1000,
                       "current_missed_commits": current["missed_commits"],
                       "current_minus_oracle_ms": current_total - total,
                       "current_minus_oracle_seconds": (current_total - total) / 1000,
                       "gap_percent_of_current": round(100 * (current_total - total) / current_total, 6) if current_total else 0.0})
        for line in current_bytes.decode().splitlines():
            entry = json.loads(line)
            workers = [w for w in entry["workers"] if w]
            cost = max((sum(durations[number] for number in w) for w in workers), default=0) + WORKER_MS * len(workers)
            case = per_commit[entry["commit"]]
            case["current_execution_cost_ms"] = cost
            case["current_minus_oracle_ms"] = cost - case["minimum_cost_ms"]
        report["zero_failing_commit_count"] = sum(c["failing_count"] == 0 for c in per_commit.values())
        report["current_cost_on_zero_failing_commits_seconds"] = sum(
            c["current_execution_cost_ms"] for c in per_commit.values() if c["failing_count"] == 0
        ) / 1000
    try:
        report["selector_checkpoint"] = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        report["selector_checkpoint"] = None
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "sample")
    parser.add_argument("--current-output", type=Path, default=ROOT / "output.jsonl")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(args.data.resolve(), args.current_output.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Exact sample oracle: {report['oracle_total_seconds']:.3f}s; "
          f"current: {report['current_total_seconds']:.3f}s; "
          f"gap: {report['current_minus_oracle_seconds']:.3f}s; "
          f"{report['cases_attaining_basic_lower_bound']}/{report['commit_count']} lower bounds attained")


if __name__ == "__main__":
    main()
