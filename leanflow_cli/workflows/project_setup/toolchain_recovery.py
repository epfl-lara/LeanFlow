"""Recover disposable compiler incoming slots without replacing or deleting native toolchains."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from core.home import leanflow_home
from leanflow_cli.formalization.evidence import digest, durable_json, identity, sync_directory
from leanflow_cli.formalization.preparation_fs_inputs import entry_identity
from leanflow_cli.workflows.project_setup.cleanup import process_states
from leanflow_cli.workflows.project_setup.process import SetupCancelled, SetupFailure


def _failure(message: str) -> SetupFailure:
    return SetupFailure(
        "setup_toolchain_recovery_required",
        message,
        "Inspect this compiler journal with python -m leanflow_cli.workflows.project_setup.toolchain_recovery inspect; preserve its versions and acknowledge the exact observation after checking stopped processes.",
    )


def _read_journal(path: Path) -> tuple[dict[str, Any], str]:
    """Read a regular controller journal while retaining unsupported legacy payloads for inspection."""
    directory = leanflow_home() / "toolchain-installations"
    if path.parent.resolve() != directory.resolve() or path.is_symlink() or not path.is_file():
        raise _failure("Compiler recovery requires its regular controller journal.")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise _failure("Compiler publication journal has no supported object payload.")
    return value, digest(raw)


def compiler_recovery_observation(path: Path, target: Path) -> dict[str, Any]:
    """Describe exact versions without deleting ambiguous legacy or unbound incoming assets."""
    journal, checksum = _read_journal(path)
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
    owner = journal.get("owner", {})
    table = process_states()
    original_live = bool(
        isinstance(owner, dict)
        and owner.get("pid") in table
        and table[owner["pid"]][2] == owner.get("start")
        and owner["pid"] != os.getpid()
    )
    value = {
        "journal": str(path),
        "target": str(target),
        "journal_sha256": checksum,
        "state": journal.get("state"),
        "versions": versions,
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
    target: Path, *, deadline: float, cancel: threading.Event | None = None
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
        try:
            journal = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise _failure("Compiler publication journal is unreadable.") from error
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
                continue
        if journal.get("state") == "DISPOSED" and valid_identity:
            continue
        slot_value = journal.get("slot")
        if not isinstance(slot_value, str) or not slot_value:
            raise _failure(
                "Legacy compiler journal has no bound slot; its versions were preserved."
            )
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
            raise _failure("Compiler incoming ownership or cleanup evidence is unsupported.")
        if journal.get("state") == "INSTALLED" and not slot.exists():
            continue
        owner = journal.get("owner", {})
        table = process_states()
        if (
            owner.get("pid") in table
            and table[owner["pid"]][2] == owner.get("start")
            and owner["pid"] != os.getpid()
        ):
            raise _failure("The earlier compiler publication controller is still alive.")
        if slot.exists():
            info = slot.lstat()
            if journal.get("slot_inode") is None:
                raise _failure(
                    "Compiler slot was created before its inode was journaled; preserve and acknowledge its exact observation."
                )
            if journal.get("slot_inode") != [info.st_dev, info.st_ino] or any(
                child.name != "compiler" for child in slot.iterdir()
            ):
                raise _failure(
                    "Compiler incoming slot was replaced or contains an independent version."
                )
        actual = entry_identity(incoming, check=check)
        if (
            actual is not None
            and actual != journal.get("after")
            and journal["state"] != "SLOT_READY"
        ):
            raise _failure("Compiler incoming bytes changed; the version was preserved.")
        installed = entry_identity(target, check=check)
        if installed is not None and installed != journal.get("after"):
            raise _failure("Another native compiler version exists; both versions were preserved.")
        if slot.exists():
            shutil.rmtree(slot)
            sync_directory(slot.parent)
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
    if args.action == "inspect":
        result = compiler_recovery_observation(args.journal, args.target)
    else:
        result = acknowledge_compiler_publication(
            args.journal,
            args.target,
            observation_id=args.observation or "",
            processes_stopped=args.confirm_processes_stopped,
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
