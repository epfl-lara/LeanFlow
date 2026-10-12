# Lean-IMO-Bench

This fixture contains 60 IMO-LeanProofBench statements: 30 Basic and 30
Advanced problems. Each standalone module imports Mathlib and contains one
theorem with one `sorry`. `manifest.json` records problem IDs, metadata,
statement digests, source revision, and the pinned upstream baseline.

## Setup

From the LeanFlow repository root:

```bash
cd testdata/workflow_projects/LeanIMOBench
./scripts/setup_project.sh
leanflow project init
```

Setup reuses a built sibling dependency tree only when its Mathlib revision and
toolchain match. It uses copy-on-write where supported and otherwise makes a
full copy. Without a matching tree, it resolves dependencies and requests
Mathlib's prebuilt cache. Cache download failures can require compilation from
source. Setup finishes with `lake build` to check all statements; the expected
60 `sorry` warnings describe the input tasks, not completed proofs.

The fixture pins Lean 4.33.1 and Mathlib
`0df444a360eaa60ab8c11dca51a86af692955474`. Upstream used Lean/Mathlib 4.27.0.
The manifest retains a dated elaboration check; rerun the build after any
toolchain change and review statement fidelity separately.

## Select and run a problem

```bash
python3 scripts/list_problems.py --split Advanced --level IMO-hard
python3 scripts/list_problems.py --unsolved-by-leap
./scripts/run_problem.sh PB-Basic-001 --print
./scripts/run_problem.sh PB-Basic-001
```

Problem IDs, theorem names, and module names are accepted. `--print` resolves the
command without launching; `--dry-run` shows LeanFlow's launch plan. The script
saves run configuration under ignored `results/<run-id>/` before invoking the
prover for the selected file.

The `lean-imo-bench` profile disables solution research and supplies bounded
budgets. `--profile lean-imo-bench-research` enables research; report those runs
separately. Inspect the profiles under `flag-profiles/` and your resolved launch
plan before making model requests. For isolated comparisons, see the
[campaign guide](../../../docs/lean-imo-campaign.md).

## Evaluation and reference solutions

Upstream reference proofs are public. They are excluded from this fixture and
its Lake import path. `scripts/fetch_reference_solutions.sh` is intended for
comparison after evaluation; keep downloaded proofs and previous attempts
outside every scored project.

The pinned manifest records LEAP's baseline as 42/60 overall, 25/30 Basic, and
17/30 Advanced. Compare results only with stated budgets, attempts, toolchains,
and solution-access settings. A successful elaboration check of an input
statement does not establish semantic equivalence across toolchain versions.

Runs write to ignored `.leanflow/`, `.lake/`, and `results/`. Exported historical
measurements under `campaign-results/` describe their recorded configurations
and are not a statement of current prover performance.

## Refreshing the fixture

`scripts/build_benchmark.py` regenerates the modules and manifest from the pinned
CSV and checks its digest. Statements are copied without reformatting. Review
their source and rebuild before changing a pin.

```bash
python3 scripts/build_benchmark.py --download
ALLOW_LEANIMOBENCH_COMMIT=1 git commit
```

The repository pre-commit hook protects intentional fixture changes. It does
not prevent local proving edits; use isolated copies for scored attempts.

## Source and license

Statements come from `google-deepmind/superhuman`, commit
`80b2527a0b4e4bfc6a8b28825fadbdcfdd6048a1`,
`imobench/lean_proof_bench_v2.csv` (SHA-256 recorded in the manifest).
Benchmark materials are CC-BY-4.0, © 2026 Google LLC. Upstream formalization
credits: Mirek Olšák, Ashley Aragorn Khoo, Edward Lockhart, Paul Lezeau,
Calle Sönne, and Moritz Firsching.
