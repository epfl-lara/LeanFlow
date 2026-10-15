"""Recover disposable compiler incoming slots without replacing or deleting native toolchains."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent.runtime.harness.processes import start_identity_matches
from core.home import leanflow_home
from leanflow_cli.formalization.evidence import (
    digest,
    durable_json,
    identity,
)
from leanflow_cli.formalization.preparation_fs_inputs import entry_identity
from leanflow_cli.workflows.project_setup.cleanup import process_states
from leanflow_cli.workflows.project_setup.process import SetupCancelled, SetupFailure
from leanflow_cli.workflows.project_setup.toolchain_disposal import (
    disposal_asset_observation,
    dispose_compiler_slot,
)
from leanflow_cli.workflows.project_setup.toolchain_journal_loss import (
    _acknowledged_loss,
    _failure,
    _journal_bytes,
    _loss_observation,
    _parsed_journal,
    _read_journal,
    acknowledge_raw_loss,
    known_owner,
)


def compiler_recovery_observation(path: Path, target: Path) -> dict[str, Any]:
    """Describe exact versions without deleting ambiguous legacy or unbound incoming assets."""
    raw = _journal_bytes(path)
    if target.parent.is_symlink():
        raise _failure("Compiler recovery target parent is aliased.", journal=path, target=target)
    journal = _parsed_journal(raw)
    if journal is None:
        return _loss_observation(path, target, raw)
    checksum = digest(raw)
    if journal.get("target") != str(target) or target.parent.is_symlink():
        raise _failure("Compiler recovery target differs from the selected journal.")
    attempt = journal.get("attempt_id", "")
    versions: dict[str, Any] = {"target": entry_identity(target)}
    slot = target.parent / (".leanflow-install-" + str(attempt))
    if re.fullmatch(r"[0-9a-f]{32}", str(attempt)) and journal.get("slot") == str(slot):
        if slot.is_symlink():
            versions["slot"] = {"symlink": os.readlink(slot)}
        elif slot.exists():
            info = slot.lstat()
            versions["slot"] = {
                "device": info.st_dev,
                "inode": info.st_ino,
                "sha256": entry_identity(slot),
            }
        else:
            versions["slot"] = None
    owner = known_owner(journal)
    table = process_states()
    original_live = bool(
        owner is not None
        and owner["pid"] in table
        and start_identity_matches(table[owner["pid"]][2], owner.get("start", ""))
        and owner["pid"] != os.getpid()
    )
    value = {
        "journal": str(path),
        "target": str(target),
        "journal_sha256": checksum,
        "state": journal.get("state"),
        "versions": versions,
        "retained_disposal_slots": disposal_asset_observation(target, journal),
        "original_owner_live": original_live,
        "uninspected_paths_preserved": {
            key: journal.get(key) for key in ("private", "incoming", "slot")
        },
        "remediation": "Confirm all acquisition processes stopped, then acknowledge this observation; every compiler/private version will be preserved.",
    }
    return {**value, "observation_id": identity(value)}


def acknowledge_compiler_publication(
    path: Path, target: Path, *, observation_id: str, processes_stopped: bool
) -> dict[str, Any]:
    """Release an observation-bound recovery blocker by preserving every observed version."""
    from leanflow_cli.workflows.project_setup.toolchain_install import _installation_owner

    with _installation_owner(time.monotonic() + 30, None):
        return _acknowledge_compiler_publication(
            path, target, observation_id=observation_id, processes_stopped=processes_stopped
        )


def _acknowledge_compiler_publication(
    path: Path, target: Path, *, observation_id: str, processes_stopped: bool
) -> dict[str, Any]:
    """Preserve one unchanged observation while holding compiler installation ownership."""
    observed = compiler_recovery_observation(path, target)
    if processes_stopped is not True or observed["original_owner_live"]:
        raise _failure("Compiler recovery acknowledgment requires stopped acquisition processes.")
    if observed["observation_id"] != observation_id:
        raise _failure(
            "Compiler recovery observation changed; inspect its preserved versions again."
        )
    if observed.get("kind") == "unreadable_compiler_journal":
        return acknowledge_raw_loss(path, target, observed)
    journal, checksum = _read_journal(path)
    if checksum != observed["journal_sha256"]:
        raise _failure("Compiler journal changed after inspection; its versions were preserved.")
    journal["state"] = "PRESERVED"
    journal["disposition"] = {
        "manual_preservation": True,
        "processes_stopped_attested": True,
        "observation": observed,
        "incoming_removed": False,
        "native_target_retained": observed["versions"]["target"] is not None,
    }
    journal["record_id"] = identity(
        {key: value for key, value in journal.items() if key != "record_id"}
    )
    durable_json(path, journal)
    return journal


def recover_compiler_publications(
    target: Path,
    *,
    deadline: float,
    cancel: threading.Event | None = None,
    checkpoint: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Dispose verified orphan incoming assets under the caller's installation lock.

    A complete installed native compiler is always retained. A changed slot, live
    previous owner, or unverifiable shutdown is retained and explicitly blocks.
    """

    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise SetupCancelled("Compiler recovery cancelled")
        if time.monotonic() >= deadline:
            raise _failure("Compiler recovery exceeded the preparation deadline.")

    results = []
    directory = leanflow_home() / "toolchain-installations"
    for path in sorted(directory.glob("*.json")):
        check()

        def failure(message: str) -> SetupFailure:
            return _failure(message, journal=path, target=target)

        try:
            raw = _journal_bytes(path)
        except SetupFailure as error:
            raise failure(str(error)) from error
        journal = _parsed_journal(raw)
        if journal is None:
            if _acknowledged_loss(path, target, raw):
                continue
            raise failure("Compiler publication journal is unreadable.")
        if journal.get("target") != str(target):
            continue
        valid_identity = journal.get("record_id") == identity(
            {k: v for k, v in journal.items() if k != "record_id"}
        )
        if journal.get("state") == "PRESERVED" and valid_identity:
            disposition = journal.get("disposition", {})
            if (
                disposition.get("manual_preservation") is True
                and disposition.get("processes_stopped_attested") is True
            ):
                recorded = disposition.get("observation", {})
                recorded_versions = recorded.get("versions") if isinstance(recorded, dict) else None
                current = compiler_recovery_observation(path, target)
                if (
                    not isinstance(recorded, dict)
                    or recorded.get("retained_disposal_slots", {})
                    != current["retained_disposal_slots"]
                    or (
                        journal.get("slot_disposal")
                        and (
                            not isinstance(recorded_versions, dict)
                            or recorded_versions.get("slot") != current["versions"].get("slot")
                        )
                    )
                ):
                    raise failure(
                        "Preserved compiler disposal versions changed; inspect them again."
                    )
                continue
        if journal.get("state") == "DISPOSED" and valid_identity:
            if journal.get("slot_disposal"):
                slot = target.parent / (".leanflow-install-" + journal.get("attempt_id", ""))
                record = journal["slot_disposal"]
                if (
                    journal.get("slot") != str(slot)
                    or not isinstance(record, dict)
                    or record.get("state") != "DISPOSED"
                ):
                    raise failure("Completed compiler disposal intent is unsupported.")
                try:
                    dispose_compiler_slot(path, journal, slot, check=check)
                except (OSError, ValueError) as error:
                    raise failure(str(error)) from error
            continue
        slot_value = journal.get("slot")
        if not isinstance(slot_value, str) or not slot_value:
            raise failure("Legacy compiler journal has no bound slot; its versions were preserved.")
        slot = Path(slot_value)
        incoming = Path(journal.get("incoming", ""))
        if (
            journal.get("schema_version") != 1
            or not valid_identity
            or journal.get("cleanup_verified") is not True
            or journal.get("state") not in {"REGISTERED", "SLOT_READY", "PREPARED", "INSTALLED"}
            or slot != target.parent / (".leanflow-install-" + journal.get("attempt_id", ""))
            or incoming != slot / "compiler"
            or slot.is_symlink()
            or target.parent.is_symlink()
        ):
            raise failure("Compiler incoming ownership or cleanup evidence is unsupported.")
        if (
            journal.get("state") == "INSTALLED"
            and not slot.exists()
            and not journal.get("slot_disposal")
        ):
            continue
        owner = known_owner(journal)
        table = process_states()
        if (
            owner is not None
            and owner["pid"] in table
            and start_identity_matches(table[owner["pid"]][2], owner.get("start", ""))
            and owner["pid"] != os.getpid()
        ):
            raise failure("The earlier compiler publication controller is still alive.")
        installed = entry_identity(target, check=check)
        if installed is not None and installed != journal.get("after"):
            raise failure("Another native compiler version exists; both versions were preserved.")
        try:
            dispose_compiler_slot(path, journal, slot, check=check, checkpoint=checkpoint)
        except (OSError, ValueError) as error:
            raise failure(str(error)) from error
        journal["state"] = "INSTALLED" if installed is not None else "DISPOSED"
        journal["disposition"] = {
            "incoming_removed": True,
            "expected_sha256": journal["after"],
            "native_target_retained": installed is not None,
        }
        journal["record_id"] = identity({k: v for k, v in journal.items() if k != "record_id"})
        durable_json(path, journal)
        results.append(journal)
    return results


def main() -> None:
    """Expose explicit inspect/acknowledge recovery while preserving ambiguous compiler assets."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "acknowledge"))
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--observation")
    parser.add_argument("--confirm-processes-stopped", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "inspect":
            result = compiler_recovery_observation(args.journal, args.target)
        else:
            result = acknowledge_compiler_publication(
                args.journal,
                args.target,
                observation_id=args.observation or "",
                processes_stopped=args.confirm_processes_stopped,
            )
    except (OSError, ValueError, SetupFailure) as error:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "success": False,
                    "error": str(error),
                    "journal": str(args.journal),
                    "target": str(args.target),
                    "remediation": getattr(
                        error,
                        "remediation",
                        "Inspect the exact journal and target paths before retrying.",
                    ),
                }
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
