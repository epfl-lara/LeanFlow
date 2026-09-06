# Lean-IMO-Bench

The 60-problem IMO-LeanProofBench slice used by LEAP
([arXiv:2606.03303](https://arxiv.org/abs/2606.03303)), vendored as a LeanFlow
workflow project so each problem can be proved and scored on its own.

Every problem is a standalone module holding exactly one theorem with exactly
one `sorry`. Nothing is shared between problems except the Lake build cache, so
a run against `PBBasic001` tells you nothing about, and is not perturbed by, a
run against `PBAdvanced012`.

## Inventory

| Split | Problems | Difficulty | Algebra | Combinatorics | Number theory | Geometry |
|---|---|---|---|---|---|---|
| `Basic` | 30 | pre-IMO (8), IMO-easy (14), IMO-medium (8) | 8 | 8 | 8 | 6 |
| `Advanced` | 30 | IMO-easy (10), IMO-medium (10), IMO-hard (10) | 8 | 8 | 6 | 8 |
| **Total** | **60** | | 16 | 16 | 14 | 14 |

`manifest.json` is the frozen inventory: id, split, category, level, competition
source, module path, statement digest, and `leap_solved`.

```bash
python3 scripts/list_problems.py --split Advanced --level IMO-hard
python3 scripts/list_problems.py --category Geometry --unsolved-by-leap
python3 scripts/list_problems.py --split Basic --ids     # for xargs
```

## First-time setup

```bash
cd testdata/workflow_projects/LeanIMOBench
./scripts/setup_project.sh     # reuse a sibling's Mathlib, then build all 60
leanflow project init
```

`setup_project.sh` looks for another project under `testdata/workflow_projects`
already holding a *built* Mathlib at this exact revision and toolchain, and
clones it copy-on-write (see "Shared Mathlib"). If it finds none it falls back
to a normal `lake update`. Then it runs `lake build`, which builds every problem
module: that is the statement-integrity gate, and you want it green before
spending an API call.

After that, build one problem at a time:

```bash
lake build LeanIMOBench.Basic.PBBasic001
```

## Shared Mathlib

A built Mathlib tree is ~7-8 GB, and this repo already carries two of them
(4.30.0-rc2 for the demos, 4.33.1 for the research projects). This fixture adds
**no third copy**: it is pinned to the same toolchain and Mathlib revision as
`BeckFialaResearch` / `SpencerResearch`, and `setup_project.sh` clones their
built package tree with APFS copy-on-write (`cp -c`). The clone shares blocks
with the original, so it is created in seconds and costs essentially no disk:

```
apparent size   8.0G
actual cost     ~47 MB
setup time      27s   (vs. a multi-hour build or a multi-GB download)
```

Copy-on-write is used deliberately in preference to symlinking one shared
packages directory. A symlinked store is literally shared, but then a
`lake update` or a rebuild in any one project mutates the tree every other
project is running against — including a live research campaign. With CoW the
projects stay independent and only diverge on the blocks one of them actually
rewrites.

On a non-APFS filesystem the script falls back to a full copy, and says so.

## Running one problem

```bash
./scripts/run_problem.sh PB-Basic-001
./scripts/run_problem.sh PBAdvanced012 --provider openai-codex
./scripts/run_problem.sh PB-Basic-001 --print      # resolve only, launch nothing
./scripts/run_problem.sh PB-Basic-001 --dry-run    # LeanFlow prints its launch plan
```

The script resolves the problem out of `manifest.json` (by id, theorem name, or
module), exports the flag profile, stamps a run id, writes a per-run
`RUN_CONFIG.json` under `results/<run-id>/` for provenance, and then invokes
`leanflow workflow prove <problem file>`. Because the file has one theorem and
one `sorry`, the file path is an unambiguous target.

A sweep is just a loop — keep it serial unless you have measured that parallel
runs do not contend on the Lean resource gates:

```bash
python3 scripts/list_problems.py --split Basic --ids \
  | xargs -I{} ./scripts/run_problem.sh {}
```

## Budgets

Two tracked profiles under `flag-profiles/`:

- **`lean-imo-bench`** (default) — clean room. Solution research **off**, 1 hour
  and 300 API calls per problem.
- **`lean-imo-bench-research`** — research **on**. Contaminated by design; see
  below. Results under this profile must be labelled separately.

Both are starting points, not measured optima. `--profile <name>` selects one.

## Contamination

**LEAP's formal Lean proofs of these exact 60 theorems are public**, at
`google-deepmind/superhuman/leap/solutions/LEAN-IMO-Bench`. A prover with web
search can retrieve a complete proof of `PBBasic001` by name. This is the single
biggest threat to a meaningful number here, and the fixture is built around it:

- reference solutions are **not** vendored, and `reference-solutions/` is
  gitignored and off the Lake import path;
- the default profile sets `LEANFLOW_DISABLE_SOLUTION_RESEARCH=1`;
- `scripts/fetch_reference_solutions.sh` exists for post-hoc diffing only, is
  interactive, and should never be run before or during a scored run.

The informal problem statement and its short answer are inside each module's
docstring — that is upstream's own format and is part of the benchmark. What is
excluded is the *formal* solution.

## Scoring

`manifest.json` carries LEAP's published baseline so a run can be sliced against
it. LEAP's one-shot formal solve rate:

| Split | LEAP |
|---|---|
| Basic | 25/30 (83.3%) |
| Advanced | 17/30 (56.7%) |
| Overall | 42/60 (70%) |

`leap_solved` per problem was read off LEAP's published solution files, so the
per-problem flags are exact, not inferred. The 18 problems LEAP did not solve —
mostly Advanced geometry and combinatorics — are the interesting headroom:

```bash
python3 scripts/list_problems.py --unsolved-by-leap
```

Any comparison to the 70% is only honest if the run used the clean-room profile,
a single attempt per problem, and no reference solutions on disk.

## Toolchain

Pinned to **Lean 4.33.1 / Mathlib `0df444a3`** -- the same pair as
`BeckFialaResearch` and `SpencerResearch`, so the three share one Mathlib on
disk.

This is *not* the version upstream verified against. Upstream fixed the v2 CSV
"to ensure that problem statements are friendly to automated proof comparators
under Lean and Mathlib 4.27.0", so moving off 4.27.0 carries a real risk that a
statement stops elaborating or quietly changes meaning. That risk was retired by
measurement rather than assumption: every one of the 60 statements was built at
4.33.1 before the pin was changed, and `lake build` is the standing gate that
keeps it honest. See "Verification" below.

If you ever retarget again, rebuild first and treat any statement that stops
elaborating as a blocker, not a warning. A statement that still elaborates but
now means something different is the harder failure and is why the pin is
recorded here rather than left to drift.

`lakefile.toml` deliberately sets no `[leanOptions]`: upstream verified these as
plain `import Mathlib` files under default elaboration options, and forcing
`relaxedAutoImplicit` or the Mathlib linter set could change how a statement
elaborates.

## Verification

`lake build` on 2026-09-06, at the pinned Lean 4.33.1 / Mathlib `0df444a3`:

```
Build completed successfully (8766 jobs).
60/60 statements elaborated      0 errors
60 `declaration uses sorry`      one per problem, matched against manifest.json
```

So the move off upstream's 4.27.0 is checked, not assumed. Three statements --
`PB-Basic-012`, `PB-Advanced-018`, `PB-Advanced-023` -- raise a deprecation
warning for `List.Chain'` (now `List.IsChain`). They elaborate with unchanged
meaning, but they are the three most likely to break on a future Mathlib bump,
so recheck those first if you ever move the pin.

Re-run the gate any time with `lake build`; it is incremental after the first
pass.

## Regenerating

`scripts/build_benchmark.py` is the only supported way to refresh the problems.
It rewrites all 60 modules and `manifest.json` from the pinned upstream CSV and
checks the CSV digest:

```bash
python3 scripts/build_benchmark.py --download
```

Each module is the upstream `Lean Statement` cell **byte for byte** — no added
header, no reformatting — so results stay comparable with published numbers.
That is why problem metadata lives in `manifest.json` and not in the modules; it
also keeps the competition source ("IMO 2019 P1") out of the prover's context,
where it would be a search hint.

## Commit guard

Tracked files here are protected by the repo pre-commit hook, for the same
reason the statements are byte-identical: a prover run that edits a statement
and gets committed silently invalidates every score compared against the
published baseline. Runs write to `.leanflow/`, `.lake/`, and `results/`, all
ignored. To refresh from upstream, regenerate and opt in explicitly:

```bash
python3 scripts/build_benchmark.py --download
ALLOW_LEANIMOBENCH_COMMIT=1 git commit
```

## Provenance and license

- Statements: IMO-LeanProofBench, `imobench/lean_proof_bench_v2.csv` at
  `google-deepmind/superhuman@80b2527a`, sha256 `ffc48aec…`. `v2` is upstream's
  current revision; `v1` is deprecated and differs on four statements
  (PB-Advanced-004/010/027, PB-Basic-028).
- Benchmark materials are **CC-BY-4.0**, © 2026 Google LLC. Formalization credits
  upstream: Mirek Olšák, Ashley Aragorn Khoo, Edward Lockhart, Paul Lezeau,
  Calle Sönne, Moritz Firsching.
- Benchmark: Luong et al., *Towards Robust Mathematical Reasoning* (IMO-Bench).
  System: Kung et al., *LEAP* (arXiv:2606.03303).

## Status

Ready to run. Dependencies resolved, Mathlib shared from `BeckFialaResearch`
via copy-on-write, and all 60 statements built clean at the pinned toolchain
(see "Verification"). No LeanFlow prover job has been run against the fixture --
that is the next step, and it is yours to start:

```bash
./scripts/run_problem.sh PB-Basic-001 --print   # confirm the plan
./scripts/run_problem.sh PB-Basic-001           # launch one problem
```
