# Shared dependencies and automatic cleanup

LeanFlow keeps reusable Lean dependencies under
`$LEANFLOW_HOME/cache/dependencies/v1` (normally `~/.leanflow`). A cache entry
contains the exact locked Git dependency closure and its compiled artifacts.
Project setup publishes it after its normal Lean readiness check. Later compatible
projects and native agent workspaces reuse it without downloading or rebuilding
those dependencies.

Entries are sealed read-only. Projects and agents receive private writable
filesystem clones with distinct inodes; editing or deleting one view cannot
change the store or another project. APFS and Linux filesystems supporting reflinks
share physical blocks until a write. Other filesystems use ordinary private copies
with free-space checks. Existing prepared project directories are retained.

Private dependency views retain independent Git metadata as well as compiled
artifacts. Lake uses the recorded package URLs and revisions to recognize existing
installations; dropping this metadata would trigger package replacement. Native
views still exclude private agent settings and controller files. Both fresh and
restored prover workspaces pass a sandboxed LeanProbe check before model launch.

Compatibility includes the exact Lean compiler/toolchain, platform, architecture,
locked dependency revisions and build settings. TOML package names and the root
project's own targets do not prevent reuse. Lean DSL configurations use conservative
whole-file equality because they can run arbitrary build logic. Local path
dependencies, package overrides, edited Git checkouts and custom package directories
keep their private installation. LeanFlow does not enable Lake's experimental
artifact-cache layout or change compiler/sampling defaults.

## Defaults and controls

```yaml
storage:
  cache_target_gib: 20
  min_free_gib: 10
  automatic_cleanup: true
```

```bash
leanflow storage status --json
leanflow storage clean
leanflow storage share /path/to/prepared/project
leanflow config set storage.cache_target_gib 20
leanflow config set storage.min_free_gib 10
```

The **Providers** page in VS Code shows the managed totals, active protections,
agent dependency views, free space and policy. **Clean unused storage** invokes
the same collector as the CLI. Size totals estimate allocated bytes per tree;
shared filesystem blocks can be counted more than once. Actual free space is the
admission authority.

The 20 GiB target is soft: active leases can exceed it. It applies to explicitly
managed dependency entries and runtimes, not every user project or native agent
home. Cleanup runs at setup, installation and workflow admission. It evicts unused
dependency entries in least-recently-used order, removes obsolete managed runtimes
and recovers abandoned staging directories. Unknown ownership or unverifiable
live processes prevent deletion.

Native sessions release their dependency views after their output and spending
are durably saved. A private ownership registry also recovers views after a crash,
only after both their owner and any remaining processes using them have exited.
Recovery removes only `.lake/packages`, retaining source, scratch proofs, the
project's own build directory, results, checkpoints and logs. Resume restores the
saved dependency pins before dispatch. Existing unregistered workspaces are never
adopted for deletion automatically.

Workflows check free space before new work and periodically during quiet native
sessions. They clean first; if the 10 GiB reserve remains unavailable they cancel
owned work and save a `storage_pressure` outcome. Free space or adjust the reserve,
then resume with the existing verified proofs and consumed-call accounting.
These checks are admission guards, not an operating-system disk quota; other
processes can still consume space between checks.

## Installation without interrupting runs

The installer creates versioned Python environments under
`~/.leanflow/runtimes/<source-identity>/venv`. It validates a new environment before
switching the command wrapper and installation metadata. Running processes keep
their previous interpreter; leases and process inspection protect environments
from collection. Healthy managed MCP environments are reused. Explicit existing
`--venv-dir` paths are rejected; `--recreate-venv` creates a fresh sibling rather
than deleting the previous runtime.

The default installation is editable and still reads its source checkout. Keep
that checkout stable while workflows run. Use `--no-editable` for a wheel install
that is independent of subsequent checkout edits.

Caches, environments, private workspaces and workflow state are local ignored
artifacts. Commit source, tests and documentation only.

Campaigns use the exact installed interpreter and shared dependency cache under
active leases. Their baselines retain statements, configuration and provenance;
finished cells release dependency views after saving results. Idle history does
not pin old runtimes indefinitely. If a historical runtime or cache was evicted,
replay requires explicit setup/reinstallation of its recorded inputs before any
model dispatch. Existing private campaign environments remain supported.
