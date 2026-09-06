# LeanProbe

LeanProbe is a standalone Python package, CLI, and MCP server for fast Lean 4
feedback when a tool repeatedly checks declarations in the same Lean project.
It uses [LeanInteract](https://github.com/augustepoiroux/LeanInteract) as its
execution backend, keeps a Lean REPL warm, reuses elaborated imports and prior
declarations, and checks a named target declaration or replacement chunk.

LeanProbe returns Lean diagnostics, warnings, `sorry` detection, tactic
metadata, goal states, and inline `feedback_lean`. The result is a real Lean
response for the checked chunk and prepared environment. Use `lake env lean
File.lean`, `lake build`, or CI when you need whole-file or whole-project
acceptance.

## MCP Tools

LeanProbe exposes the MCP server name `lean-probe` and the tools
`lean_probe_capabilities`, `lean_probe_prepare`, `lean_probe_check`,
`lean_probe_feedback`, `lean_probe_state`, `lean_probe_step`, and
`lean_probe_close_state`.

For MCP parameter details, result-field semantics, and `feedback_lean` examples,
see [AGENT.md](AGENT.md).

## Why It Is Faster

Many Lean workflows perform several related checks in one file: check a
candidate declaration, inspect diagnostics or proof state, try another
candidate, then move to a nearby declaration. A repeated full-file terminal
check pays import, header, and prior-declaration elaboration cost each time.

LeanProbe separates that cost:

```text
prepare header/imports/prior declarations -> env before target
env before target + checked declaration -> diagnostics/proof states
env before target + next checked declaration -> diagnostics/proof states
```

For sequential same-file checks, "environment" means Lean's elaborated state
after processing some prefix of the file. It is not just the import/header
state. The state grows only when a declaration is accepted:

```text
imports/header -> env0
env0 + declaration t1 -> env1   # env1 contains imports/header and t1
env1 + declaration t2 -> env2   # env2 contains imports/header, t1, and t2
env2 + declaration t3 -> env3
```

If a tool is trying several replacements for `t2`, each attempt should reuse
`env1`; failed attempts do not advance the environment. Once the complete `t2`
is accepted, LeanProbe can use `env2` for later declarations instead of
rechecking imports, `t1`, and `t2` from scratch.

See [Benchmarks](#benchmarks) for headline results and
[BENCHMARKS.md](BENCHMARKS.md) for the benchmark methodology.

## How It Differs From LSP MCP Tools

LeanProbe and LSP-backed Lean MCP servers are complementary. Tools such as
`lean-lsp-mcp` are broad project-navigation and interaction layers over
`lake serve`: they are the better fit for file-position diagnostics, goals,
hover information, references, completions, code actions, widgets, and theorem
search integrations.

LeanProbe is narrower: it screens complete declaration replacements against a
cached LeanInteract environment, exposes proof-state stepping for standalone
snippets, and benchmarks declaration-level agent loops against `lake env lean`.
Use it when an agent is trying many candidate declarations or moving through a
file in source order. Use an LSP MCP beside it when the agent needs editor-like
semantic context around the file.

## Install

LeanProbe is a Python package that talks to Lean through LeanInteract. `pip`
installs LeanProbe's Python dependencies, including `lean-interact`. It does
not install Lean, Lake, or Mathlib; those belong to the Lean toolchain and the
Lake project being checked. `lake` must be available on `PATH` or passed with
`--lake-path`.

Required:

- Python 3.10 or newer.
- Lean 4 and Lake installed through
  [elan](https://github.com/leanprover/elan).
- `git`, used by Lean/Lake dependency workflows.
- A Lean/Lake project to run checks in. For the bundled examples, that project
  must have Mathlib available because the examples start with `import Mathlib`.
- A built Lean project, or `--auto-build` when you want LeanInteract to build it
  before checking.

Install the CLI and Python package:

```bash
python -m pip install lean-probe
```

That command installs the required Python runtime dependencies. If
`python -c "import lean_probe, lean_interact"` fails, run the install command in
the same Python environment that will launch LeanProbe.

Install MCP support when you want to run the MCP server:

```bash
python -m pip install "lean-probe[mcp]"
```

Editable checkout for development:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

Check the Python package and CLI:

```bash
python -c "import lean_probe, lean_interact; print('ok')"
lean-probe --version  # lean-probe 0.2.2
```

Check that Lean/Lake are visible:

```bash
lake --version
lean --version
```

Run LeanProbe by pointing `--cwd` at a Lake project that can import the
dependencies used by the file being checked:

```bash
lean-probe check examples/lean/number_theory_nat.lean nat_mul_pos_bench \
  --cwd /path/to/mathlib-lake-project \
  --pretty
```

If the target project does not already have LeanInteract's REPL support built,
either let LeanInteract build it with `--auto-build` or pass an existing REPL
checkout with `--local-repl-path`.
If `--cwd` is supplied, it must be inside a Lake project; otherwise LeanProbe
returns `error_code="no_project_root"`.

For MCP use, configure the MCP client to run `lean-probe mcp` from this same
Python environment. If the client launches servers outside your activated
shell, use the absolute path to `.venv/bin/lean-probe` in the MCP
configuration.
Set `LEAN_PROBE_LAKE_PATH`, `LEAN_PROBE_LOCAL_REPL_PATH`,
`LEAN_PROBE_AUTO_BUILD`, or `LEAN_PROBE_VERBOSE` to configure the MCP server
without CLI flags.

After an editable development install, run the package tests from the
repository with `python -m pytest -q`.

## CLI

```bash
lean-probe prepare /path/to/File.lean --cwd /path/to/lake-project --theorem-id my_theorem

lean-probe capabilities --cwd /path/to/lake-project --pretty

lean-probe check /path/to/File.lean my_theorem \
  --cwd /path/to/lake-project \
  --replacement-file /tmp/candidate.lean \
  --pretty

lean-probe feedback /path/to/File.lean my_theorem \
  --cwd /path/to/lake-project \
  --pretty
```

Benchmark commands are documented in [BENCHMARKS.md](BENCHMARKS.md).

## Python

```python
from lean_probe import LeanProbe

probe = LeanProbe()
probe.prepare_file("/path/to/File.lean", cwd="/path/to/lake-project", theorem_id="my_theorem")

result = probe.check_target(
    "/path/to/File.lean",
    cwd="/path/to/lake-project",
    theorem_id="my_theorem",
    replacement="""
theorem my_theorem : True := by
  trivial
""",
)
print(result["ok"], result["elapsed_s"])
```

For tactic-by-tactic exploration:

```python
state = probe.proof_state_from_code("theorem ex (n : Nat) : n = n := by sorry")
proof_state = state["sorries"][0]["proof_state"]
step = probe.tactic_step(state["session_id"], proof_state, "rfl")
print(step["proof_status"])
```

## MCP

Run the MCP server over stdio:

```bash
lean-probe mcp
```

Example MCP configuration:

```json
{
  "mcpServers": {
    "lean-probe": {
      "command": "lean-probe",
      "args": ["mcp"]
    }
  }
}
```

Example MCP configuration with LeanProbe environment variables:

```json
{
  "mcpServers": {
    "lean-probe": {
      "command": "lean-probe",
      "args": ["mcp"],
      "env": {
        "LEAN_PROBE_LAKE_PATH": "/opt/homebrew/bin/lake",
        "LEAN_PROBE_AUTO_BUILD": "0"
      }
    }
  }
}
```

For stdio MCP clients such as Codex, keep `LEAN_PROBE_AUTO_BUILD=0` and build
the Lean project from a terminal before using LeanProbe. Some Lean/Lake build
commands print progress to stdout; stdout is reserved for MCP JSON-RPC frames,
so build output can corrupt the transport.

Use `lean_probe_capabilities` when setup is uncertain. Use
`lean_probe_prepare` before repeated checks in the same file, then call
`lean_probe_check` for concrete target declarations or replacements. When
ordinary diagnostics are not enough, call `lean_probe_feedback` and inspect
`messages`, `tactics`, and `feedback_lean`. See [AGENT.md](AGENT.md) for the
full MCP contract.

## Benchmarks

Snapshot refreshed: May 13, 2026, with Lean `4.30.0-rc2`
(`3dc1a088b6d2d8eafe25a7cd7ec7b58d731bd7cc`).

Main results:

| Benchmark shape | Platform | Main result |
| --- | --- | --- |
| Repeated target checks, compact examples | macOS | cached checks averaged 0.008-0.048s by group versus 3.708-3.900s for full-file Lake checks |
| Repeated target checks, compact examples | Linux | cached checks averaged 0.009-0.046s by group versus 2.199-2.301s for full-file Lake checks |
| Repeated target checks, TCS examples | macOS | cached checks averaged 0.031-0.049s by group versus 2.082-2.617s for full-file Lake checks |
| Repeated target checks, TCS examples | Linux | cached checks averaged 0.032-0.054s by group versus 1.495-1.886s for full-file Lake checks |
| Sequential same-file checks | macOS | cached checking completed in 3.789-4.775s per file, a 9.63x-14.24x speedup versus growing-prefix Lake checks |
| Sequential same-file checks | Linux | cached checking completed in 2.301-2.547s per file, a 9.05x-9.82x speedup versus growing-prefix Lake checks |

The practical takeaway is that fresh LeanProbe checks cost roughly the same
order of time as terminal Lean checks, while cached checks are tens of
milliseconds for these examples. Keep the LeanProbe process warm for agent
loops that try many replacements or walk declarations in source order.

For benchmark files, methodology, production interpretation, grouped tables,
per-target rows, and reproduction commands, see [BENCHMARKS.md](BENCHMARKS.md).

## Output Shape

`lean_probe_check` and `lean_probe_feedback` return JSON-compatible dictionaries:

- `success`: false for tool/project/backend failures;
- `ok`: true only when Lean accepts the target without `sorry`;
- `error_code`: stable machine-readable failure code when `success=false`;
- `timed_out`: true when the backend failure was classified as a timeout;
- `messages`: Lean diagnostics with both chunk-local and file-global positions;
- `tactics`: tactic text, ranges, goals, proof states, and used constants;
- `feedback_lean`: target declaration with inline feedback comments;
- `cache`: header/prior-declaration environment reuse metadata;
- `elapsed_s`: wall-clock time for the check.

Current `error_code` values include `no_project_root`, `file_not_found`,
`target_not_found`, `lean_interact_unavailable`, `lean_interact_start_failed`,
`header_failed`, `prior_decl_failed`, `dead_server`, `session_dead`,
`unknown_session`, `timeout`, and `backend_error`.

See [AGENT.md](AGENT.md) for the complete MCP output contract, including
`success` versus `ok`, proof-state stepping, and `feedback_lean`.

Declarations inside `mutual ... end` blocks are included as prior context for
later targets, but the individual declarations inside the mutual block are not
separate LeanProbe targets. If a requested target is found inside such a block,
LeanProbe returns `target_not_found` with a hint that explains the limitation.

## Backend Dependency

[LeanInteract](https://github.com/augustepoiroux/LeanInteract) is LeanProbe's
primary backend dependency. LeanInteract provides the Lean REPL process,
incremental elaboration, command responses, proof states, tactic stepping, and
the low-level interaction API.

LeanProbe builds on that backend with file segmentation, same-file declaration
targeting, warm prior environments, replacement checks, feedback annotation,
CLI commands, MCP tools, and reproducible benchmark harnesses.
