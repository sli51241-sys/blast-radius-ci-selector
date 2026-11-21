# How the selector works

The goal is to reduce the cost of checking a change while still including every
test that would fail. Choosing fewer tests helps only when the omitted tests
would pass: missing even one failing test adds a 300-second penalty for that
change.

## 1. Learn from the unchanged project

Before handling changes, the build can run the pinned, unchanged sqlglot test
suite. The coverage tools record which source lines each test executes and which
files it reads. They also record shared fixture consumers and imports.

Think of the resulting map as a notebook:

| Baseline observation | Possible dependency |
| --- | --- |
| Test A executes a SQL generator function | A change to that function may affect A |
| Test B reads a SQL fixture file | A change to that fixture may affect B |
| Several tests use a shared setup fixture | Code executed once during setup may affect them all |

These are observations of the original project, not precomputed answers to
changes. The selector does not load the sample answer file. That file is used
only by the separate scorer to evaluate a proposed schedule.

## 2. Read each change against the original

Every patch independently changes the same pinned project. Patches are not
applied one after another.

The parser records changed file paths, removed and added lines, and the original
position of insertions. An insertion between old lines 10 and 11 has no removed
line, so its boundary and neighboring lines matter when finding the affected
function. Renames, deleted files, binary files, and malformed input are kept
visible rather than disappearing from the change set.

For Python source, the selector also applies the patch in memory and parses the
updated program. It checks the patch's context exactly. A patch that cannot be
understood safely takes a conservative path.

## 3. Choose tests that may depend on the change

For an ordinary function edit, the selector expands the changed location to its
enclosing function. This is broader than selecting only tests that ran the exact
changed line. A changed condition, for example, could make a previously unused
branch run.

Fixture changes use file-read observations, and changed test files include their
own tests. Unknown dependencies require a broader selection. The implementation
includes fallback paths that can select the whole suite when the map does not
provide enough information.

A comment is not automatically harmless. Tests may read raw source or fixture
text, and some comments have meaning to a parser or test helper. Comparing
Python syntax trees provides additional evidence, but it must still account for
source readers and behavior during imports.

The baseline map cannot prove that every possible new behavior has been covered.
Import-time changes, new dependencies, and cached file reads are important
sources of uncertainty. The 300-second miss penalty makes conservative fallbacks
valuable.

## 4. Balance the selected tests across workers

Each worker runs its assigned tests sequentially. The estimated cost is the
slowest worker's total recorded test time, plus two seconds for every worker.

For example, tests taking 8, 7, and 3 seconds cost 20 seconds on one worker:
18 seconds of work plus 2 seconds of overhead. Two workers can receive `[8]`
and `[7, 3]`, costing 10 + 4 = 14 seconds. Three workers cost 8 + 6 = 14
seconds. Extra workers do not always help.

The scheduler compares worker counts up to the allowed maximum of eight,
balances the work, and emits the selected schedule as one JSON line per change.
Diagnostics go to stderr so stdout contains only the required protocol.

## How to evaluate an improvement

Run unit tests for parsing and scheduling, then run the selector on the public
sample and score its output with the supplied scorer. Inspect both the simulated
time and the number of missed changes. The final competition also charges the
selector's CPU time and uses unseen changes, so a sample result is evidence about
that sample rather than a promise about the final.
