# Session handoff

Updated November 21, 2026 after three accepted precision improvements.

## Current state

- Repository: https://github.com/sli51241-sys/blast-radius-ci-selector (`main`).
- Optimization stopped after **three accepted improvements**, before the two-hour limit.
- Current `make sample`: **223.421 simulated seconds**, **0 misses across 30 commits**, down from **316.575** at the start of this run.
- All **165 focused checks** passed in the locked Python environment, including the map collector integration check. Three candidate sample runs passed; no sample misses or sample rollbacks occurred.
- Improvement 1 was pushed as `a9d7498b3d39c45cffc1639079c14bbfa9dcb28a`. Improvements 2 and 3 remain local; the stop condition occurred before the next hourly push/practice checkpoint.

## Accepted changes

| Target | Change | Tests before → after | Case seconds before → after | Total sample before → after |
| --- | --- | ---: | ---: | ---: |
| `6ze8` | Isolated deferred lambda bodies, with qualified class users retained | 1,617 → 31 | 36.817 → 2.418 | 316.575 → 282.176 |
| `4d63` | Prove import/collection consumers inspect unchanged dictionary keys | 1,617 → 374 | 36.817 → 12.686 | 282.176 → 258.045 |
| `gbav` | Source-proven pure callback factories with literal argument edits | 1,617 → 32 | 36.817 → 2.193 | 258.045 → 223.421 |

Each loop selected one of the current maximum-cost commits, with ties at 36.817 seconds. Runtime contains no patch IDs or sample-answer lookup tables. Evaluation records are separate from the image.

## Real practice feedback

- Latest tested checkpoint: **340.3 seconds**, receipt **BB-B6DEE032**, commit `a9d7498b3d39c45cffc1639079c14bbfa9dcb28a`.
- All three runs: **333.995 simulated seconds**, **0 missed commits**; CPU **6.28, 6.33, 5.88** seconds.
- This was worse than the prior **338.3** score for `99a1991a0913e9cab61b086ab060574ecdbb892e`: simulated cost was unchanged and median CPU rose from 4.27 to 6.28 seconds. The sample gain did not transfer to this hidden practice batch.
- The team's best remained **338.3 seconds**, **rank 232/330 when checked**. This is the team's best-entry rank, not a separate rank assigned to the worse submission.
- Improvement 2 limits lambda analysis to a touched lambda span to address this overhead. No official practice result exists yet for improvements 2 or 3, so their real effect is unknown.
- No final-entry picker was clicked. The site's automatic best-score final default was left alone.
- The next practice checkpoint in this run would have been no earlier than **22:58:19 UTC / 2:58:19 p.m. Pacific**. Work stopped at the three-improvement limit first.

## Validation and guardrails

The original `maps/coverage.json.gz`, `maps/coverage-v2.json.gz`, and `maps/symbols.json.gz` remain unchanged. `maps/import-sites.json.gz` is derived from the pinned source: **184 library modules**, collection of **1,621 tests across 58 test modules**, zero tests executed, complete instrumentation, and no unresolved sites. Library-import-only metadata was deemed insufficient and expanded before the sample candidate was accepted.

Mixed edits, unknown/default/import-time execution, source readers, generated classes, callback factory side effects/shadowing, dictionary value consumers, and missing metadata keep conservative fallbacks. Reproduction commands are in the README. The offline map builder discloses inert CLI handling and source/instrumenter digests.

## Next

Review the practice regression before making further precision changes. `ktz3` remains at 36.817 seconds and 1,617 selected tests; it changes expression class metadata with real import-time consumers. Do not assume a low sample cost implies a low hidden score. The official final deadline is November 22 at 8 a.m. Pacific.
