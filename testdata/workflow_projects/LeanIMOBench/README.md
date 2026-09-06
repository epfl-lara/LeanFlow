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

Not done yet — this fixture ships without a `lake-manifest.json` because the
dependency revisions are pinned in `lakefile.toml` and the manifest is whatever
`lake update` resolves them to:

```bash
cd testdata/workflow_projects/LeanIMOBench
lake update          # writes lake-manifest.json, fetches Mathlib
lake build           # builds all 60 statements: the statement-integrity gate
leanflow project init
```

`lake build` with no target builds every problem module. That is the gate you
want once, up front: it proves all 60 statements elaborate under this toolchain
before you spend a single API call. After that, build one problem at a time:

```bash
lake build LeanIMOBench.Basic.PBBasic001
```

Expect the first Mathlib build to be long. It is a different toolchain from
`ProveDemo` (see below), so it does not share that cache.

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

Pinned to **Lean 4.27.0 / Mathlib `a3a10db0`**, because upstream states v2 of the
CSV was fixed "to ensure that problem statements are friendly to automated proof
comparators under Lean and Mathlib 4.27.0". Statements verified at 4.27.0 are not
guaranteed to elaborate — or to mean the same thing — under a different Mathlib,
so the pin is part of the benchmark rather than a default to drift from. It also
matches the `bench-v1-lean4.27.0` pin the T3 suite already uses.

This differs from `ProveDemo` / `DocFormalizationDemo` (4.30.0-rc2) and
`BeckFialaResearch` / `SpencerResearch` (4.33.1). If you retarget it, rerun
`lake build` first and treat any statement that stops elaborating as a blocker,
not a warning.

`lakefile.toml` deliberately sets no `[leanOptions]`: upstream verified these as
plain `import Mathlib` files under default elaboration options, and forcing
`relaxedAutoImplicit` or the Mathlib linter set could change how a statement
elaborates.

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

Prepared, not exercised. No `lake update`, `lake build`, or LeanFlow run has been
performed against this fixture yet — the statements are verified byte-identical
to upstream and structurally checked (60 modules, one theorem and one `sorry`
each, `import Mathlib` only), but they have not been elaborated locally.
`lake build` is the first thing to run.
