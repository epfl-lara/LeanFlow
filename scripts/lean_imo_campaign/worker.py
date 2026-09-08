"""Launch one benchmark cell through the frozen LeanFlow prover entry point."""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

from scripts.lean_imo_campaign.artifacts import digest, save
from scripts.lean_imo_campaign.runtime_versions import runtime_directory


def main() -> int:
    """Pin runtime settings before any provider request or controller startup."""
    from leanflow_cli.runtime.runtime_provider import resolve_runtime_provider
    from leanflow_cli.workflows.prover.config import ENV_NAMES
    from leanflow_cli.workflows.prover.entrypoint import main as prove

    directory, cell_id = Path(sys.argv[1]).resolve(), sys.argv[2]
    manifest = json.loads((directory / "campaign.json").read_text())
    cell = next(c for c in manifest["cells"] if c["id"] == cell_id)
    snapshot = runtime_directory(directory, cell)
    identity = json.loads((snapshot / "provenance.json").read_text())
    for relative, expected in identity["runtime_files"].items():
        if digest(snapshot / "runtime" / relative) != expected:
            raise ValueError(f"Frozen runtime changed: {relative}")
    for external, expected in identity.get("external_inputs", {}).items():
        if digest(Path(external)) != expected:
            raise ValueError(
                "Shared LeanFlow configuration changed; pause and review comparability"
            )
    root = Path(cell["project"])
    runtime = resolve_runtime_provider(requested="openai-codex")
    if (
        runtime["provider"] != "openai-codex"
        or runtime["api_mode"] != "codex_responses"
        or runtime["base_url"].rstrip("/") != manifest["provider_base_url"]
    ):
        raise ValueError("Provider identity differs from the frozen comparison")
    env = {
        "LEANFLOW_PROJECT_ROOT": str(root),
        "LEANFLOW_WORKFLOW_RUN_ID": cell["run_id"],
        "LEANFLOW_NATIVE_PROVIDER": runtime["provider"],
        "LEANFLOW_NATIVE_API_MODE": runtime["api_mode"],
        "LEANFLOW_NATIVE_BASE_URL": runtime["base_url"],
        "LEANFLOW_NATIVE_API_KEY": runtime["api_key"],
        "LEANFLOW_NATIVE_MODEL": cell["model"],
        "LEANFLOW_NATIVE_REASONING_EFFORT": cell["effort"],
        "LEANFLOW_NATIVE_ACTIVE_FILE": cell["problem"]["file"],
        "LEANFLOW_NATIVE_ACTIVE_SKILL": "lean-bounded-prover",
        "LEANFLOW_NATIVE_ADDITIONAL_SKILLS": "",
        "LEANFLOW_NATIVE_EXPLICIT_GOAL": "Prove the assigned theorem using local libraries and computations only. Internet research and downloads are disabled. Preserve the original statement.",
        "LEANFLOW_CLEAN_ROOM_TASK_LABELS": "|".join(
            (cell["problem"]["id"], cell["problem"]["theorem"])
        ),
    }
    for key, value in cell["config"].items():
        env[ENV_NAMES[key]] = (
            ("1" if value else "0")
            if isinstance(value, bool)
            else ",".join(value)
            if isinstance(value, (list, tuple))
            else str(value)
        )
    os.environ.update(env)
    if cell.get("resume_run_id"):
        os.environ["LEANFLOW_PROVER_RESUME_RUN_ID"] = cell["resume_run_id"]
    os.chdir(root)

    def interrupt(_signum: int, _frame: object) -> None:
        """Reap separately grouped warm Lean workers before unwinding the controller."""
        from leanflow_cli.workflows.prover.check_process import _close_all

        _close_all()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    save(
        root / ".leanflow" / f"launch-{cell['run_id']}.json",
        {
            "model": cell["model"],
            # The config is what actually routes each role, so report it rather
            # than assuming the planning roles share the prover's model.
            "orchestrator_model": cell["config"].get("orchestrator_model") or cell["model"],
            "effort": cell["effort"],
            "provider": runtime["provider"],
            "api_mode": runtime["api_mode"],
            "base_url": runtime["base_url"],
            "runtime_sha256": identity["runtime_sha256"],
            "config": cell["config"],
            "pid": os.getpid(),
        },
    )
    return prove()


if __name__ == "__main__":
    raise SystemExit(main())
