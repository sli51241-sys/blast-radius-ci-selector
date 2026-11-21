# Session handoff

Updated November 21, 2026 after the class-data precision check.

## Current state

- Repository: https://github.com/sli51241-sys/blast-radius-ci-selector (`main`).
- Class-data precision checkpoint prepared for a new practice run; the practice score below is the last completed result.
- Final `make sample`: **316.575 simulated seconds**, **0 misses across 30 commits**. This saves **25.579 seconds (7.48%)** against 342.154 seconds.
- Only sample schedule `535z` changed: `TSQL.WEEK_OFFSET` now selects **171** tests instead of **1,617**, reducing its cost from **36.817** to **11.238** seconds. All other 29 schedules are unchanged. This is evaluation evidence; no patch identifiers or sample answers are used by runtime selection.
- All **69 unit tests** passed. The new pinned-baseline map passed **1,621 tests and 25,454 subtests**. The map-generation timeout was raised to 180 seconds after the instrumented `test_max_nodes` exceeded 90 seconds.
- Three local restricted-container runs measured median selector CPU **1.485 seconds before** and **2.303 seconds after** (about **0.817 seconds added**). These are not official server CPU measurements.
- Previous official practice: **388.1 seconds**, scored commit `c7ab054e57d05126b593ccf3c43ad0a1d80ab8c9`, team `dalladalla`. No new practice submission was made. The latest historical checkpoint before these local changes is `edf3606`.

## Active improvement

`maps/classes.json.gz` is now active alongside the unchanged original line coverage and symbols maps. It contains only pinned-baseline class usage and provenance. Class observations include MRO users, nested-class outer owners, and collection-time attribute/dictionary reads. The export now recognizes `@collect:*` contexts and `vars()` dictionary reads.

Narrowing applies only to replacements of existing literal class-data values. An AST comparison proves that the rest of the module is unchanged, fixing the mixed class-field/function edit gap. Imported/cached fields, augmented assignments, dunder fields, modules containing decorated or known generated classes, missing metadata, and failed import probes retain broad selection. The selector verifies that a separate class map matches the original map's pinned source commit and collection-context schema.

## Reproduce

Run `python3 -m unittest discover -s tests` and `make sample`.
The README includes commands to rebuild the original maps and to generate/extract the separate class map with a 180-second instrumentation timeout. `benchmarks/report.json` records the new sample result and CPU comparison.

## Next

Keep the zero-miss checkpoint. The remaining class-data fallbacks still select nearly all tests; table/callback changes need additional dependency evidence before narrowing. RWX allows one submission per hour and only the final submission counts. Submissions close November 22 at 8 a.m. Pacific.
