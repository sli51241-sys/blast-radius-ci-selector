# Blast Radius CI Selector

A Python entry for the RWX track of the Builders Buildathon, November 2026.
Given an independent change to a pinned SQLGlot checkout, it selects relevant
regression tests and schedules them across up to eight workers.

## Approach

1. **Measure the unchanged repository.** Record per-test executed source lines,
   fixture reads, shared fixture setup, and local import relationships.
2. **Analyze each diff.** Apply it in memory, parse Python syntax, and identify
   affected functions, class data, source readers, and fixture consumers.
3. **Select conservatively.** Include tests covering affected code; broaden the
   selection for import-time changes, dynamic attributes, or unfamiliar inputs.
4. **Schedule cheaply.** Try one through eight worker counts with deterministic
   longest-test-first balancing. Include the two-second cost of each worker.

The runtime uses only Python's standard library and has no network dependency.
It never reads sample answers or uses patch-to-answer lookup tables.

## Run the official sample in Docker

Requires Docker, Python 3.9+, and make:

```sh
make sample
```

The Makefile builds Linux/AMD64 and applies the scorer's restrictions: no
network, two CPUs, 4 GB memory, read-only filesystem, unprivileged user, and
64 MB temporary storage. Logs go to stderr; stdout contains only the required
JSON lines.

## Local development

With a Python 3.13 environment and the pinned source at `../sqlglot`:

```sh
CLASS_USAGE_TOOL="$PWD/tools/class_usage.py" python -m unittest discover -s tests -v
python tools/benchmark.py --source ../sqlglot
python selector.py --data sample --source ../sqlglot > output.jsonl
python score.py sample sample/answers.json output.jsonl
```

Recreate the unchanged source checkout if needed:

```sh
git clone https://github.com/tobymao/sqlglot.git ../sqlglot
git -C ../sqlglot checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
```

## Measurements

The reproducible report is [`benchmarks/report.json`](benchmarks/report.json).
These are **simulated CI seconds on the supplied 30-commit sample**, not wall
clock times or hidden leaderboard scores. Lower is better.

| Version | Simulated seconds | Missed commits |
| --- | ---: | ---: |
| Official starter: all tests, three workers | 1210.470 | 0 |
| All tests, worker-count selection | 1104.510 | 0 |
| Coverage and dependency selection | 342.154 | 0 |
| Literal class-data precision | 316.575 | 0 |
| Deferred class callbacks | 282.176 | 0 |
| Key-only import projections | 258.045 | 0 |
| Pure callback factories | 223.421 | 0 |

The server additionally charges selector CPU and takes a median of repeated
runs. Sample results do not establish correctness or ranking on unseen changes.

## Rebuild the baseline maps

Install the supplied locked test environment plus `coverage==7.17.4`:

```sh
python -m pip install -r requirements-tools.txt
python tools/build_coverage.py --source ../sqlglot --output maps/coverage.json.gz
python tools/build_symbols.py --source ../sqlglot --output maps/symbols.json.gz
python tools/build_coverage.py --source ../sqlglot --class-usage --test-timeout 180 --output /tmp/classes-baseline.json.gz
python tools/extract_class_map.py --input /tmp/classes-baseline.json.gz --output maps/classes.json.gz
python -B tools/build_import_sites.py --source ../sqlglot --class-usage-tool tools/class_usage.py --output maps/import-sites.json.gz
```

The measured baseline passed 1,621 tests and 25,454 subtests with
`SKIP_INTEGRATION=0`. During offline instrumentation, process-pool tasks run
synchronously with pickle round trips so coverage includes worker execution
and serialization. This affects map generation only. Shared pytest fixture
setup is attributed to all its consumers. The map records its environment,
source commit, instrumentation, and baseline outcomes.

The separate class map records qualified class receivers, their base classes,
and fields read during collection. It preserves the original line map. Class
narrowing applies only to existing literal data values when the rest of the AST
is unchanged and collection did not read the field or its class dictionary.
Generated classes, augmented assignments, and special Python fields retain the
broad fallback. An in-memory import check must also pass.

Deferred callback edits have separate structural proofs. Lambda-body edits keep
the signature and surrounding AST unchanged, require no collection execution,
and include both callback coverage and qualified class users. Literal argument
edits to callback factories require source that only constructs and returns a
callback, unchanged call structure, and no import-time value consumers.

A separate pinned-source map records qualified attribute read sites during all
library imports and collection of all baseline tests, without executing tests.
Existing literal dictionaries can narrow when their keys are unchanged and every
observed import/collection consumer is proven to inspect only those keys. Value
reads, unknown flows, stale maps, and mixed source edits retain broad selection.
The map builder records its inert CLI handling and source/instrumenter digests.

## Read the code

- [`selector.py`](selector.py): coverage lookup, dependency guards, and entrypoint.
- [`scheduler.py`](scheduler.py): worker count and load balancing.
- [`patch_analysis.py`](patch_analysis.py): strict unified diffs and in-memory application.
- [`tools/coverage_plugin.py`](tools/coverage_plugin.py): baseline instrumentation.
- [`docs/approach.md`](docs/approach.md): a plain-English walkthrough.

## Limits

Dynamic dependencies can escape baseline coverage. Module and class changes
may select more tests than necessary. LPT scheduling is a heuristic rather
than an exact solution. Cross-platform behavior and novel final changes remain
risks; a zero-miss public sample is not a proof about the final.

## Submission and provenance

Submit this public repository URL and its full commit hash on the RWX track.
The root Dockerfile downloads only SQLGlot at the fixed commit above. No test
labels, sample patches, or scoring scripts are copied into the runtime image.

Reused material: the official Blast Radius kit supplied `starter_selector.py`,
`score.py`, `SCORING.md`, `requirements.lock`, the sample dataset, the Makefile,
and the base image pin. Original starter instructions are preserved in
[`docs/STARTER_README.md`](docs/STARTER_README.md). SQLGlot is an MIT-licensed
upstream dependency fetched during the build. This entry is MIT licensed.
