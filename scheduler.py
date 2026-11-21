"""Schedule selected tests using the scorer's integer-millisecond costs.

For each worker count, longest-processing-time first (LPT) greedily puts the
next test on the least loaded worker. We compare the eight inexpensive
candidates including their worker startup costs; this is a heuristic, not an
exact solution to the partitioning problem.
"""

import math

WORKER_COST_MS = 2000
MAX_WORKERS = 8


def _durations(tests: list[dict]) -> dict[int, int]:
    """Match score.py's conversion of recorded test seconds to milliseconds."""
    durations = {}
    for test in tests:
        number = test["number"]
        if number in durations:
            raise ValueError(f"duplicate test number: {number}")
        duration = math.floor(test["seconds"] * 1000 + 0.5)
        if duration < 0:
            raise ValueError(f"negative test duration: {number}")
        durations[number] = duration
    return durations


def cost_ms(workers: list[list[int]], tests: list[dict]) -> int:
    """Return simulated execution cost, ignoring empty workers as the scorer does.

    CPU time and missed-failure penalties belong to the caller's evaluation.
    ``tests`` may contain the full catalog or just the selected tests.
    """
    durations = _durations(tests)
    loads = [sum(durations[number] for number in worker) for worker in workers if worker]
    return max(loads, default=0) + WORKER_COST_MS * len(loads)


def schedule(tests: list[dict]) -> list[list[int]]:
    """Return the cheapest deterministic LPT split over one through eight workers.

    Ties prefer fewer workers. Input order does not affect the result, and
    every supplied test is placed once. An empty selection runs no workers.
    """
    durations = _durations(tests)
    if not durations:
        return []

    ordered = sorted(durations, key=lambda number: (-durations[number], number))
    best_workers = []
    best_cost = None
    for count in range(1, min(MAX_WORKERS, len(ordered)) + 1):
        workers = [[] for _ in range(count)]
        loads = [0] * count
        for number in ordered:
            lightest = min(range(count), key=lambda index: (loads[index], index))
            workers[lightest].append(number)
            loads[lightest] += durations[number]

        nonempty = [worker for worker in workers if worker]
        cost = max(loads) + WORKER_COST_MS * len(nonempty)
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_workers = nonempty

    return best_workers
