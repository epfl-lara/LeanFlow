"""Acknowledge exact preserved baseline versions without inventing subprocess EOF evidence."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from agent.runtime.harness.processes import _cleanup_states, start_identity_matches
from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.evidence import digest, durable_json, identity, sync_directory
from leanflow_cli.formalization.publication_io import rename_exclusive
from leanflow_cli.runtime.project_operations import project_setup
from leanflow_cli.workflows.project_setup import baseline_transaction as transaction
from leanflow_cli.workflows.project_setup import invocation_evidence, recovery_barrier
from leanflow_cli.workflows.project_setup.scaffold import project_root
from leanflow_cli.workflows.prover import source_mutation

LIMIT = 64 * 1024


def _directory(root: Path) -> Path:
    """Reject aliased recovery infrastructure before interpreting a retained acknowledgment."""
    return recovery_barrier.directory(root)


def _versions(root: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Bind current canonical absence and every retained mutation inode and byte version."""
    result: dict[str, Any] = {}
    for identifier in record["mutations"]:
        mutation = source_mutation.load(root, identifier)
        location = source_mutation.directory(root, identifier)
        paths = {"canonical": root / mutation["path"]}
        paths.update(
            {
                leaf: location / leaf
                for leaf in ("candidate", "displaced", "incoming", *mutation["rollback_slots"])
            }
        )
        for leaf, path in paths.items():
            result[f"{identifier}:{leaf}"] = {
                "path": str(path),
                "version": source_mutation.observe(path),
            }
        result[f"{identifier}:journal"] = {
            "path": str(location / "journal.json"),
            "sha256": digest(read_regular_bytes(location / "journal.json", limit=LIMIT)),
        }
    return result


def _process_observation(
    value: dict[str, Any], table: dict[int, tuple[int, str, str]]
) -> dict[str, Any]:
    """Require recorded controller identity and independently absent groups and descendants."""
    owner = value.get("controller")
    if (
        not isinstance(owner, dict)
        or type(owner.get("pid")) is not int
        or owner["pid"] <= 1
        or not isinstance(owner.get("start"), str)
        or not owner["start"]
    ):
        return {"metadata_available": False, "reason": "Controller identity was not recorded"}
    owner_row = table.get(owner["pid"])
    if (
        owner_row
        and not owner_row[1].startswith("Z")
        and not start_identity_matches(owner_row[2], owner["start"])
    ):
        return {
            "metadata_available": False,
            "reason": "Present controller start identity has an unknown observation frame",
        }
    owner_live = bool(
        owner_row
        and start_identity_matches(owner_row[2], owner["start"])
        and not owner_row[1].startswith("Z")
    )
    pid = value.get("pid")
    if pid is None:
        return {
            "metadata_available": False,
            "reason": "Interrupted launch has no recorded process group; absence cannot be established",
        }
    if type(pid) is not int or pid <= 1:
        return {"metadata_available": False, "reason": "Invocation process group is invalid"}
    observed = value.get("observed_processes", {})
    if (
        not isinstance(observed, dict)
        or len(observed) > 256
        or value.get("observed_processes_truncated") is True
    ):
        return {
            "metadata_available": False,
            "reason": "Invocation descendant identities are unavailable",
        }
    live = []
    for text, start in observed.items():
        if not text.isdigit() or not isinstance(start, str) or not start:
            return {
                "metadata_available": False,
                "reason": "Invocation descendant identity is invalid",
            }
        row = table.get(int(text))
        if row and not row[1].startswith("Z") and not start_identity_matches(row[2], start):
            return {
                "metadata_available": False,
                "reason": "Present descendant start identity has an unknown observation frame",
            }
        if row and start_identity_matches(row[2], start) and not row[1].startswith("Z"):
            live.append(int(text))
    group_live = [
        number
        for number, (group, state, _) in table.items()
        if pid is not None and group == pid and not state.startswith("Z")
    ]
    return {
        "metadata_available": True,
        "controller_live": owner_live,
        "live_recorded_processes": sorted(live),
        "live_group_members": sorted(group_live),
        "processes_absent": not owner_live and not live and not group_live,
    }


def observation(path: str | Path) -> dict[str, Any]:
    """Describe exact recovery inputs without changing journals, sources, or readiness."""
    root = project_root(path)
    table = _cleanup_states(2.0)
    journals = []
    invocations: dict[str, Any] = {}
    selected = transaction.all_records(root)
    roots: dict[Path, set[str]] = {root: set(invocation_evidence.expected(root))}
    for journal, record in selected:
        journals.append(
            {
                "path": str(journal),
                "id": record["id"],
                "state": record["state"],
                "sha256": digest(read_regular_bytes(journal, limit=LIMIT)),
                "versions": _versions(root, record),
            }
        )
        roots[root].update(record["invocations"])
        stage = Path(record["stage"])
        roots.setdefault(stage, set()).update(invocation_evidence.expected(stage))
    for owner_root, identifiers in sorted(roots.items()):
        for found in invocation_evidence.directory(owner_root).glob("*.json"):
            identifiers.add(found.stem)
        if len(identifiers) > invocation_evidence.MAX_INVOCATIONS * transaction.MAX_HISTORY:
            raise ValueError("Recovery invocation set exceeds its bounded audit")
        for identifier in sorted(identifiers):
            value, raw = invocation_evidence.load_record(owner_root, identifier)
            if value.get("id") != identifier or value.get("root") != str(owner_root.resolve()):
                raise ValueError("Recovery cannot acknowledge an unbound invocation ledger")
            if value.get("cleanup_verified") is True:
                continue
            try:
                prior_acknowledged = invocation_evidence.record_closed(
                    owner_root, identifier, value, raw
                )
            except (OSError, ValueError):
                prior_acknowledged = False
            if prior_acknowledged:
                # Exact prior acknowledgments retain their inputs; a subsequently
                # reused unrelated PGID cannot reverse the recorded observation.
                continue
            key = str(owner_root) + ":" + identifier
            invocations[key] = {
                "root": str(owner_root),
                "id": identifier,
                "ledger_sha256": digest(raw),
                "processes": _process_observation(value, table),
                "cleanup_verified": False,
                "physical_eof_verified": False,
                "retained_cwd": value.get("cwd"),
            }
    payload = {
        "schema": 1,
        "root": str(root),
        "journals": journals,
        "uncertain_invocations": invocations,
        "canonical": {
            name: source_mutation.observe(root / name)
            for name in ("lakefile.toml", "lakefile.lean", "lean-toolchain", "lake-manifest.json")
        },
        "remediation": "Inspect every preserved version. Confirm processes stopped and output writers closed externally, then acknowledge this exact observation while keeping current files. Fresh setup is still required.",
    }
    return {**payload, "observation_id": identity(payload)}


def acknowledge(
    path: str | Path,
    *,
    observation_id: str,
    processes_stopped: bool,
    output_closed: bool,
    keep_current: bool,
) -> dict[str, Any]:
    """Preserve all inputs and record external confirmation separately from physical closure."""
    root = project_root(path)
    if processes_stopped is not True or output_closed is not True or keep_current is not True:
        raise ValueError(
            "Recovery requires explicit stopped-process, closed-output, and keep-current confirmations"
        )
    with project_setup(root):
        observed = observation(root)
        if not observation_id or observed["observation_id"] != observation_id:
            raise ValueError("Baseline recovery observation changed; inspect again")
        for item in observed["uncertain_invocations"].values():
            if not item["processes"].get("metadata_available") or not item["processes"].get(
                "processes_absent"
            ):
                raise ValueError(
                    "Recovery requires available metadata and no live controller, group, or recorded descendant"
                )
        directory = _directory(root)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        from leanflow_cli.workflows.project_setup.state import fingerprint, state_directory

        durable_json(
            state_directory(root) / "result.json",
            {
                "status": "failed",
                "code": "setup_recheck_required",
                "phase": "recovery",
                "message": "External recovery acknowledgment requires fresh project setup.",
                "remediation": "Run project setup to verify the retained current configuration.",
                "fingerprint": fingerprint(root),
            },
        )
        durable_json(directory / f"observation-{observation_id}.json", observed)
        barrier = {
            "schema": 1,
            "root": str(root),
            "observation_id": observation_id,
            "canonical": observed["canonical"],
            "fresh_setup_required": True,
            "retain_inputs": True,
            "acknowledgment_complete": False,
        }
        recovery_barrier.write(root, barrier)
        completed = {**barrier, "acknowledgment_complete": True}
        checksum = identity(completed)
        durable_json(directory / f"barrier-{observation_id}.json", completed)
        for journal in observed["journals"]:
            original = Path(journal["path"])
            record = transaction._load(original)
            durable_json(
                directory / f"{record['id']}.json",
                {
                    "schema": 1,
                    "id": record["id"],
                    "observation_id": observation_id,
                    "barrier_sha256": checksum,
                    "versions": journal["versions"],
                    "retain_inputs": True,
                },
            )
            if original.name == "pending.json":
                # Preserve the original intent bytes; the new disposition only
                # retires automatic recovery, never overwrites canonical files.
                raw = read_regular_bytes(original, limit=LIMIT)
                saved = directory / f"intent-{record['id']}-{observation_id}.json"
                if saved.exists():
                    if read_regular_bytes(saved, limit=LIMIT) != raw:
                        raise ValueError("Preserved recovery intent changed")
                else:
                    with saved.open("xb") as handle:
                        handle.write(raw)
                        handle.flush()
                        os.fsync(handle.fileno())
                    sync_directory(directory)
                record["state"] = "acknowledged"
                record["recovery_observation_id"] = observation_id
                transaction._save(root, record)
                rename_exclusive(original, transaction._base(root) / f"{record['id']}.json")
        for item in observed["uncertain_invocations"].values():
            owner = Path(item["root"])
            durable_json(
                invocation_evidence.directory(owner).parent
                / "recovery-acknowledgments"
                / f"{item['id']}.json",
                {
                    "schema": 1,
                    "root": item["root"],
                    "id": item["id"],
                    "ledger_sha256": item["ledger_sha256"],
                    "recovery_root": str(root),
                    "observation_id": observation_id,
                    "barrier_sha256": checksum,
                    "external_processes_stopped": True,
                    "external_output_closed": True,
                    "cleanup_verified": False,
                    "physical_eof_verified": False,
                    "retain_inputs": True,
                },
            )
        recovery_barrier.write(root, completed)
        try:
            issue = transaction.pending_issue(root)
        except (OSError, ValueError, RuntimeError) as error:
            issue = f"Final acknowledgment audit is unavailable: {error}"
        return {
            "status": "acknowledged_external_NOT_VERIFIED",
            "acknowledgment_committed": True,
            "ready": False,
            "recovery_issue": issue,
            "root": str(root),
            "observation_id": observation_id,
            "cleanup_verified": False,
            "physical_eof_verified": False,
            "fresh_setup_required": True,
            "retain_inputs": True,
        }


def acknowledged_issue(root: Path, record: dict[str, Any]) -> str | None:
    """Reblock any acknowledged retained version whose bytes or inode changes later."""
    path = _directory(root) / f"{record['id']}.json"
    if not path.exists() and not path.is_symlink():
        return None
    value = json.loads(read_regular_bytes(path, limit=LIMIT))
    if not isinstance(value, dict) or value.get("id") != record["id"] or value.get("schema") != 1:
        return "Baseline recovery acknowledgment is invalid"
    if not recovery_barrier.acknowledged(
        root, str(value.get("observation_id", "")), str(value.get("barrier_sha256", ""))
    ):
        return "Baseline recovery acknowledgment is incomplete"
    actual = _versions(root, record)
    expected = value.get("versions")
    if not isinstance(expected, dict):
        return "Baseline recovery version observation is unavailable"
    # A later controller-checked setup consumes canonical bindings, while all
    # displaced/candidate/rollback inodes and original mutation intent stay audited.
    if {key: item for key, item in actual.items() if not key.endswith(":canonical")} != {
        key: item for key, item in expected.items() if not key.endswith(":canonical")
    }:
        return "An acknowledged retained baseline version changed; inspect recovery again"
    return None


def has_acknowledgment(root: Path, record: dict[str, Any]) -> bool:
    """Require a bound acknowledgment instead of interpreting a conflicted mutation as clean."""
    path = _directory(root) / f"{record['id']}.json"
    return path.exists() or path.is_symlink()


def canonical_issue(root: Path) -> str | None:
    """Bind current bytes until an owned fresh mutation captures their exact preserved inode."""
    value = recovery_barrier.current(root)
    if value is None:
        return None
    if value["acknowledgment_complete"] is not True:
        return "Baseline recovery acknowledgment is incomplete; inspect the retained versions again"
    if not fresh_setup_required(root):
        return None
    following = [
        record
        for _, record in transaction.all_records(root)
        if record.get("recovery_parent_observation_id") == value["observation_id"]
    ]
    for relative, expected in value["canonical"].items():
        if source_mutation.observe(root / relative) == expected:
            continue
        captured = False
        for record in following:
            for identifier in record["mutations"]:
                mutation = source_mutation.load(root, identifier)
                if mutation["path"] != relative or mutation["before"] != expected:
                    continue
                if expected is not None:
                    captured = (
                        source_mutation.observe(
                            source_mutation.directory(root, identifier) / "displaced"
                        )
                        == expected
                    )
                else:
                    captured = mutation["state"] in {
                        "captured",
                        "install_pending",
                        "installed",
                        "committed",
                        "rolled_back",
                    }
                if captured:
                    break
            if captured:
                break
        if not captured:
            return "Acknowledged canonical baseline versions changed before fresh setup"
    return None


def consume_canonical_binding(root: Path, transaction_id: str) -> None:
    """Release only the current-file binding after controller-verified fresh setup completion."""
    value = recovery_barrier.current(root)
    if value is not None:
        if value["acknowledgment_complete"] is not True:
            raise ValueError("An incomplete recovery acknowledgment cannot become ready")
        durable_json(
            _directory(root) / "consumed.json",
            {
                "schema": 1,
                "observation_id": value["observation_id"],
                "verified_transaction": transaction_id,
            },
        )


def fresh_setup_required(root: Path) -> bool:
    """Keep external acknowledgment from reusing an earlier readiness receipt."""
    value = recovery_barrier.current(root)
    if value is None:
        return False
    if value["acknowledgment_complete"] is not True:
        return True
    consumed = _directory(root) / "consumed.json"
    if not consumed.exists() and not consumed.is_symlink():
        return True
    record = json.loads(read_regular_bytes(consumed, limit=LIMIT))
    if not isinstance(record, dict) or record.get("schema") != 1:
        raise ValueError("Baseline recovery completion evidence is invalid")
    if record.get("observation_id") != value["observation_id"]:
        return True
    identifier = record.get("verified_transaction")
    if not isinstance(identifier, str) or re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
        raise ValueError("Baseline recovery verified completion identity is invalid")
    completed = [item for _, item in transaction.all_records(root) if item["id"] == identifier]
    if (
        len(completed) != 1
        or completed[0]["state"] != "committed"
        or completed[0].get("recovery_parent_observation_id") != value["observation_id"]
    ):
        raise ValueError("Baseline recovery verified completion journal is unavailable")
    return False
