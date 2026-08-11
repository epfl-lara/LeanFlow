# Claude Code verification prompt

Audit the current LeanFlow working tree and the installed LeanFlow VS Code
extension as a skeptical release verifier. This is a verification task, not an
implementation task: do not edit files, create commits, change branches, start
or stop workflows, or disturb any active Lean process.

The repository is `/Users/lmilikic/Desktop/LeanFlow`. Inspect all current files,
including untracked files; do not assume `git diff` contains the extension.
The intended installed extension is `epfl-lara.leanflow-vscode@0.1.8`, packaged
at `vscode-extension/leanflow-vscode-0.1.8.vsix`.

Verify these claims from current evidence:

1. Extension activation does not leave the LeanFlow sidebar blank or spinning.
2. A VS Code workspace opened at `artifacts/imo-2026` discovers the single
   active nested project `formalization`, while ignoring project copies under
   archive directories. Multiple non-archived nested projects must fail closed.
3. A workflow launched outside the current VS Code window is shown only when
   its durable run identity/process evidence matches. A stopped tracked row
   must not hide the active project status.
4. The Live tab shows phase, target, active file, file and project sorry counts,
   proof state, provider/model/skill, agents, locks, checkpoints, declaration
   queue, blocker, last activity, diagnostics, and goals. Confirm that it
   refreshes on the configured cadence and stops polling after the verified
   process ends. Do not end the process to test this; use tests/code review for
   terminal behavior if a run is active.
5. Opening the dashboard must not spawn a workflow. Compare process identity
   before and after read-only UI inspection.
6. Run/log/status selection is run-scoped; legacy status is adopted only by
   exact PID plus process-token evidence, never PID alone.
7. Prompts, credentials, credential-bearing URLs, and unrestricted environment
   values do not reach ordinary extension storage, UI snapshots, logs, errors,
   or exports.
8. Profile paths, launch targets, skills, webview messages, restored ownership,
   experiment isolation, exact metrics, provenance, and comparison eligibility
   retain their documented fail-closed behavior.

Run the extension checks serially from `vscode-extension`:

```bash
npm run typecheck
npm test
npm run build -- --production
npm audit --omit=dev --audit-level=high
code --list-extensions --show-versions | grep epfl-lara.leanflow-vscode
```

For Python, activate `.venv` first. Run the focused live-status/run command
tests before considering broader gates:

```bash
source .venv/bin/activate
python -m pytest tests/leanflow/test_runs_command.py tests/leanflow/test_native_runner.py -q -n 0
black --check .
ruff check .
mypy
```

Inspect the actual VS Code UI read-only if available. Report exact observed
values and distinguish installed-package evidence, source-code evidence, test
evidence, and inference. List findings by P0/P1/P2/P3 with file and line
references, then give a release verdict. If no actionable issue remains, say so
explicitly and list residual risks. Do not claim a green test proves the live UI;
require direct UI evidence for UI claims.
