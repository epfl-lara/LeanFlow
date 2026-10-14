"""Exercise real compiled acceptance through fresh review, publication, and scoped handoff."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import pytest

from agent.runtime.harness import HarnessResult
from core.campaign_limits import CampaignLimits
from leanflow_cli.formalization import environment, freshness, handoff, proposal, publish, review
from leanflow_cli.formalization.evidence import digest, identity
from leanflow_cli.formalization.lean_layout import resolve_layout
from leanflow_cli.formalization.records import (
    create_document,
    current_selection_token,
    load_document,
    load_receipt,
    receipt_events,
)
from leanflow_cli.formalization.service import resolve_artifact, show_document
from leanflow_cli.native.harness_artifacts import source_files
from leanflow_cli.workflows.prover.native_snapshot import create_snapshot
from leanflow_cli.workflows.prover.source import SourceDocument

AXIOMS = ("propext", "Classical.choice", "Quot.sound")


def _review_dossier(request: Any, assignment: dict[str, Any]) -> dict[str, Any]:
    """Read a complete large dossier through the live authenticated bounded review transport."""
    if "dossier" in assignment:
        return dict(assignment["dossier"])
    config = json.loads(Path(request.mcp_config).read_text())
    server = next(iter(config["mcpServers"].values()))["env"]
    assert server["LEANFLOW_BRIDGE_URL"].startswith("http://127.0.0.1:")
    offset, chunks = 0, []
    while True:
        request_id = "fixture-dossier-page-" + str(offset)
        query = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "read_evidence",
                "arguments": {"id": assignment["dossier_ref"], "offset": offset},
            },
        }
        with urlopen(
            Request(
                server["LEANFLOW_BRIDGE_URL"],
                data=json.dumps(query).encode("utf-8"),
                headers={
                    "Authorization": "Bearer " + server["LEANFLOW_BRIDGE_TOKEN"],
                    "Content-Type": "application/json",
                },
            ),
            timeout=5,
        ) as response:
            wire = response.read(16001)
        assert len(wire) <= 16000
        envelope = json.loads(wire)
        assert envelope["jsonrpc"] == "2.0" and envelope["id"] == request_id
        assert not envelope["result"]["isError"]
        page = json.loads(envelope["result"]["content"][0]["text"])
        assert page["id"] == assignment["dossier_ref"]
        assert page["sha256"] == assignment["sha256"]
        assert page["offset"] == offset
        assert page["complete_length"] == assignment["characters"]
        chunks.append(page["text"])
        offset += len(page["text"])
        if page["next_offset"] is None:
            assert offset == assignment["characters"]
            break
        assert page["text"] and page["next_offset"] == offset
    content = "".join(chunks)
    assert len(content.encode("utf-8")) == assignment["utf8_bytes"]
    dossier = json.loads(content)
    assert identity(dossier) == assignment["sha256"]
    assert len(dossier["units"]) == assignment["unit_count"]
    assert len(dossier["regions"]) == assignment["region_count"]
    return dict(dossier)


@pytest.fixture
def formalization_project(tmp_path: Path) -> Path:
    """Use an installed explicit toolchain, with package, library, and root names differing."""
    toolchain = os.environ.get("LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN")
    if not toolchain:
        pytest.skip("Set LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN for pinned compiler integration")
    (tmp_path / "lean-toolchain").write_text(toolchain + "\n")
    (tmp_path / "lakefile.toml").write_text(
        'name = "doubling_paper"\ndefaultTargets = ["Actual"]\n'
        '[[lean_lib]]\nname = "Actual"\nsrcDir = "src"\nroots = ["Main"]\n'
    )
    (tmp_path / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.1.0",
                "packagesDir": ".lake/packages",
                "packages": [],
                "name": "doubling_paper",
                "lakeDir": ".lake",
            }
        )
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src/Main.lean").write_text("theorem unrelated_in_root : True := by sorry\n")
    return tmp_path


@pytest.fixture
def cheap_persistent_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub only the costly compiler-byte scan; real policy/build checks remain enabled."""

    def configuration_identity(root: Path) -> str:
        return identity(
            {
                name: digest((root / name).read_bytes())
                for name in ("lean-toolchain", "lakefile.toml", "lake-manifest.json")
            }
        )

    for module in (proposal, publish, handoff, freshness):
        monkeypatch.setattr(module, "environment_identity", configuration_identity)


@pytest.fixture
def fresh_review_transport(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Simulate external review responses while executing the real evidence protocol gate."""
    calls = []

    def invoke(request: Any) -> HarnessResult:
        calls.append(request)
        assert request.session_id == "" and request.restricted_tools and not request.tools
        request.on_started()
        assignment = json.loads(request.prompt[len(review._CONTRACT) + 1 :])
        dossier = _review_dossier(request, assignment)
        return HarnessResult(
            status="completed",
            session_id=f"independent-{len(calls)}",
            final_response=json.dumps(
                {
                    "revision_id": assignment["revision_id"],
                    "dossier_id": assignment["dossier_id"],
                    "verdicts": [
                        {
                            "unit_id": unit["id"],
                            "source_ids": unit["required_source_ids"],
                            "decision": "PASS",
                            "rationale": "Fixture oracle checks exact quantifiers and named construction dependencies.",
                            "issues": [],
                        }
                        for unit in dossier["units"]
                    ],
                    "coverage": [
                        {
                            "region_id": region["id"],
                            "decision": "PASS",
                            "rationale": "Fixture oracle accounts for this original source region.",
                        }
                        for region in dossier["regions"]
                    ],
                    "claims": [
                        {
                            "source_id": source_id,
                            "status": "asserted",
                            "rationale": "Verified source content asserts this claim.",
                        }
                        for source_id in sorted(
                            {
                                key
                                for unit in dossier["units"]
                                for key in unit["required_source_ids"]
                            }
                        )
                    ],
                    "coherence_decision": "PASS",
                    "feedback": "All fixture source items, shared definitions, and regions are accounted for.",
                }
            ),
        )

    monkeypatch.setattr(review, "run_harness", invoke)
    monkeypatch.setattr(review, "build_runtime_isolation", lambda *args, **kwargs: object())
    return calls


def _write_proposal(
    snapshot: Path,
    record: dict[str, Any],
    units: list[dict[str, Any]],
    *,
    phase: str = "complete",
) -> None:
    """Save exact source-unit mappings and the complete region denominator."""
    from leanflow_cli.formalization.slice_gate import slice_regions

    selected_regions = (
        slice_regions(record)
        if phase == "representative_slice"
        else {row["id"] for row in record["inventory"]["regions"]}
    )
    target = snapshot / ".leanflow-job/formalization-proposal.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_revision_id": record["revision_id"],
                "phase": phase,
                "setup_notes": "Use natural numbers, addition, and a named existence obligation for choice.",
                "units": units,
                "coverage": [
                    {
                        "region_id": region["id"],
                        "disposition": region["disposition"],
                        "item_ids": region.get("item_ids", []),
                        "reason": region["reason"] or "Exact fixture region inspected.",
                    }
                    for region in record["inventory"]["regions"]
                    if region["id"] in selected_regions
                ],
            }
        )
    )


def _unit(
    source_id: str, names: list[str], file: str = "", dependencies: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Create an explicit source-to-declaration unit, including exact library reuse."""
    return {
        "id": "unit-" + source_id,
        "required_source_ids": [source_id],
        "declarations": [
            {"name": name, **({"file": file} if file else {"imports": ["Init"]})} for name in names
        ],
        "representation_note": "Natural-number interpretation; retain all split claims and explicit existence dependence.",
        "depends_on_units": ["unit-" + key for key in dependencies],
    }


def _check_and_publish(
    root: Path, record: dict[str, Any], snapshot: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the real controller and commit its exact reviewed result."""
    baseline = source_files(root)
    metadata = publish.portable_baseline(root, record["document_id"])
    checked = proposal.check_proposal(
        root=root,
        snapshot=snapshot,
        baseline=baseline,
        directory=root / ".leanflow/checks",
        document_id=record["document_id"],
        base_revision_id=record["revision_id"],
        selection_token=current_selection_token(root, record["document_id"], initialize=True),
        allowed_axioms=AXIOMS,
    )
    assert checked["accepted"], json.dumps(checked, indent=2)
    assert (
        source_files(root) == baseline
    ), "Compilation must leave canonical project bytes unchanged"
    reviewed = review.review_revision(
        checked["record"],
        backend="claude",
        model="fixture-reviewer",
        effort="max",
        environment={"LEANFLOW_PROJECT_ROOT": str(root)},
        directory=root / ".leanflow/reviews",
        budget=CampaignLimits(root / ".leanflow/limits.json", {}),
    )
    assert reviewed["accepted"], reviewed
    receipt = publish.publish_checked(
        root, baseline, metadata, checked, reviewed, root / ".leanflow/publication"
    )
    assert receipt["status"] == "published", receipt
    return checked, receipt


@pytest.mark.integration
def test_doubling_chosen_halves_review_publish_and_handoff(
    formalization_project: Path,
    cheap_persistent_identity: None,
    fresh_review_transport: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = formalization_project
    (root / "paper.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\input{definitions}\n"
        "\\input{results}\n\\end{document}\n"
    )
    (root / "definitions.tex").write_text(
        "\\begin{definition}\\label{def:double}For a natural number $n$, define $d(n)=n+n$.\\end{definition}\n"
    )
    (root / "results.tex").write_text(
        "\\begin{theorem}\\label{thm:split}For every natural number $n$, $d(n)=n+n$ and there exists a natural number $k$ such that $d(n)=k+k$.\\end{theorem}\n"
        "\\begin{definition}\\label{def:half}For each natural number $n$, choose a natural number $h(n)$ such that $d(h(n))=n+n$; such a number exists.\\end{definition}\n"
        "\\begin{theorem}\\label{thm:commute}For all natural numbers $n,m$, $n+m=m+n$.\\end{theorem}\n"
    )
    record = create_document(root, root / "paper.tex")
    assert len(record["inventory"]["items"]) == 4
    assert len(record["source_files"]) == 3
    assert not record["inventory"]["diagnostics"]
    layout = resolve_layout(root, record["document_id"])
    assert layout["accepted"], layout
    assert layout["root_module"] == "Main" and layout["library"] == "Actual"
    source_ids = {item["label"]: item["id"] for item in record["inventory"]["items"]}
    snapshot = create_snapshot(root, root / ".leanflow/author")
    defs = layout["owned_prefix"] + "/Defs.lean"
    results = layout["owned_prefix"] + "/Results.lean"
    (snapshot / defs).parent.mkdir(parents=True)
    (snapshot / defs).write_text(
        "namespace Doubling\ndef double (n : Nat) : Nat := n + n\nend Doubling\n"
    )
    (snapshot / results).write_text(
        f"import {layout['module_prefix']}.Defs\nnamespace Doubling\n"
        "theorem double_eq (n : Nat) : double n = n + n := by rfl\n"
        "theorem double_even (n : Nat) : ∃ k : Nat, double n = k + k := by sorry\n"
        "theorem half_exists (n : Nat) : ∃ k : Nat, double k = n + n := by sorry\n"
        "noncomputable def half_of_double (n : Nat) : Nat := Classical.choose (half_exists n)\n"
        "theorem half_spec (n : Nat) : double (half_of_double n) = n + n :=\n"
        "  Classical.choose_spec (half_exists n)\nend Doubling\n"
    )
    (snapshot / layout["aggregator"]).write_text(
        f"import {layout['module_prefix']}.Defs\nimport {layout['module_prefix']}.Results\n"
    )
    _write_proposal(
        snapshot,
        record,
        [
            _unit(source_ids["def:double"], ["Doubling.double"], defs),
            _unit(
                source_ids["thm:split"],
                ["Doubling.double_eq", "Doubling.double_even"],
                results,
                (source_ids["def:double"],),
            ),
            _unit(
                source_ids["def:half"],
                ["Doubling.half_exists", "Doubling.half_of_double", "Doubling.half_spec"],
                results,
                (source_ids["def:double"],),
            ),
            _unit(source_ids["thm:commute"], ["Nat.add_comm"]),
        ],
    )
    checked, receipt = _check_and_publish(root, record, snapshot)
    assert len(fresh_review_transport) == 2
    profiles = checked["record"]["checks"]["profiles"]
    assert profiles["Doubling.half_of_double"]["conditional"]
    assert profiles["Doubling.half_of_double"]["depends_on"] == ["Doubling.half_exists"]
    assert profiles["Doubling.half_spec"]["conditional"]
    assert not profiles["Nat.add_comm"]["pending"]
    assert "unrelated_in_root" not in profiles
    main = (root / "src/Main.lean").read_text()
    assert main.startswith(f"import {layout['module_prefix']}\n")
    assert main.endswith("theorem unrelated_in_root : True := by sorry\n")
    portable = root / "formalization" / record["document_id"]
    validated = handoff.validate_handoff(root, portable / "handoff.json", checked["revision_id"])
    assert validated == receipt["handoff"]
    assert {row["declaration"] for row in validated["obligations"]} == {
        "Doubling.double_even",
        "Doubling.half_exists",
    }
    assert set(handoff.authorized_holes(validated)) == {results}
    displayed = show_document(root, record["document_id"])
    assert displayed["can_prove"] and displayed["counts"]["reviewed"] == 4
    assert not displayed["lean_stale"]
    assert (portable / "Blueprint.md").is_file()
    events = receipt_events(load_document(root, record["document_id"]))
    assert [event["payload"]["status"] for event in events] == ["accepted_staged", "published"]

    # Durable source and Lean views remain meaningful after private session cleanup.
    shutil.rmtree(Path(record["_directory"]))
    restored = show_document(root, record["document_id"])
    assert receipt_events(load_document(root, record["document_id"])) == events
    assert restored["revision_id"] == checked["revision_id"] and restored["can_prove"]
    artifact = next(row for row in restored["artifacts"] if row["path"] == results)
    frozen = resolve_artifact(root, record["document_id"], checked["revision_id"], artifact["id"])
    assert Path(frozen["path"]).read_bytes() == (root / results).read_bytes()

    # Any accepted source or compiler identity change disables launch, including unrelated code.
    for relative in (results, "src/Main.lean"):
        path = root / relative
        before = path.read_bytes()
        path.write_bytes(before + b"-- changed after review\n")
        with pytest.raises(ValueError, match="source changed"):
            handoff.validate_handoff(root, portable / "handoff.json", checked["revision_id"])
        stale = show_document(root, record["document_id"])
        assert stale["lean_stale"] and not stale["can_prove"]
        path.write_bytes(before)
    source_path = root / "results.tex"
    source_before = source_path.read_bytes()
    source_path.write_bytes(source_before + b"An additional unreviewed assertion.\n")
    with pytest.raises(ValueError, match="[Ss]ource changed"):
        handoff.validate_handoff(root, portable / "handoff.json", checked["revision_id"])
    assert show_document(root, record["document_id"])["source_stale"]
    source_path.write_bytes(source_before)
    with monkeypatch.context() as changed_environment:
        changed_environment.setattr(handoff, "environment_identity", lambda root: "changed")
        with pytest.raises(ValueError, match="environment changed"):
            handoff.validate_handoff(root, portable / "handoff.json", checked["revision_id"])

    # Independent proof acceptance updates the editor without changing source review.
    document = SourceDocument(
        results,
        (root / results).read_text(),
        replacements={"0": "exact ⟨n, rfl⟩", "1": "exact ⟨n, rfl⟩"},
    )
    (root / results).write_text(document.render())
    certificate = handoff.proof_preservation_certificate(
        root, validated, {results: document}, root / ".leanflow/proof-checks", AXIOMS
    )
    proved = show_document(root, record["document_id"])
    assert proved["proof_revision_id"] == certificate["proof_revision_id"]
    assert proved["proof_completion"] == "checked" and not proved["can_prove"]
    assert not proved["lean_stale"]
    assert all(item["proof_status"] == "checked" for item in proved["items"])
    current_artifact = next(row for row in proved["artifacts"] if row["path"] == results)
    current_view = resolve_artifact(
        root, record["document_id"], checked["revision_id"], current_artifact["id"]
    )
    assert Path(current_view["path"]).read_text() == document.render()
    assert "sorry" not in Path(current_view["path"]).read_text()
    assert (root / "src/Main.lean").read_text() == main

    # A malformed new submission selects its rejection instead of retaining an old PASS.
    accepted = load_document(root, record["document_id"])
    retry = create_snapshot(root, root / ".leanflow/retry")
    (retry / ".leanflow-job").mkdir(exist_ok=True)
    (retry / ".leanflow-job/formalization-proposal.json").write_text("{malformed")
    rejected = proposal.check_proposal(
        root=root,
        snapshot=retry,
        baseline=source_files(root),
        directory=root / ".leanflow/retry-checks",
        document_id=record["document_id"],
        base_revision_id=accepted["revision_id"],
        selection_token=current_selection_token(root, record["document_id"], initialize=True),
        allowed_axioms=AXIOMS,
    )
    assert not rejected["accepted"]
    selected = load_document(root, record["document_id"])
    assert selected["revision_id"] != accepted["revision_id"]
    selected_receipt = load_receipt(selected)
    assert selected_receipt and selected_receipt["status"] == "check_rejected"
    assert selected_receipt["handoff"] is None
    assert not show_document(root, record["document_id"])["can_prove"]
    with pytest.raises(ValueError, match="selected"):
        handoff.validate_handoff(root, portable / "handoff.json", checked["revision_id"])


@pytest.mark.integration
def test_direct_library_reuse_publishes_without_new_modules_or_obligations(
    formalization_project: Path,
    cheap_persistent_identity: None,
    fresh_review_transport: list[Any],
) -> None:
    root = formalization_project
    (root / "paper.tex").write_text(
        "\\begin{theorem}\\label{commute}For natural numbers $n,m$, $n+m=m+n$.\\end{theorem}\n"
    )
    record = create_document(root, root / "paper.tex")
    snapshot = create_snapshot(root, root / ".leanflow/author")
    _write_proposal(
        snapshot, record, [_unit(record["inventory"]["items"][0]["id"], ["Nat.add_comm"])]
    )
    before = (root / "src/Main.lean").read_bytes()
    checked, receipt = _check_and_publish(root, record, snapshot)
    assert checked["files"] == {}
    assert (root / "src/Main.lean").read_bytes() == before
    assert not receipt["handoff"]["obligations"]
    view = show_document(root, record["document_id"])
    assert not view["can_prove"] and view["handoff"]["obligations"] == []
    path = root / "formalization" / record["document_id"] / "handoff.json"
    assert handoff.validate_handoff(root, path, checked["revision_id"])["obligations"] == []


@pytest.mark.integration
@pytest.mark.parametrize("file_reference", [False, True])
def test_direct_existing_project_reuse_with_custom_root_is_readonly_and_navigable(
    formalization_project: Path,
    cheap_persistent_identity: None,
    fresh_review_transport: list[Any],
    file_reference: bool,
) -> None:
    """Reuse both meanings from a built local module without wrapper declarations."""
    root = formalization_project
    relative = "src/Main/Existing.lean"
    path = root / relative
    path.parent.mkdir(parents=True)
    path.write_text(
        "namespace Existing\n"
        "def double (n : Nat) : Nat := n + n\n"
        "theorem identity_nat (n : Nat) : n = n := rfl\n"
        "theorem unrelated_pending : True := by sorry\n"
        "end Existing\n"
    )
    (root / "paper.tex").write_text(
        "\\begin{definition}For a natural number $n$, define $d(n)=n+n$.\\end{definition}\n"
        "\\begin{theorem}Every natural number $n$ equals itself.\\end{theorem}\n"
    )
    baseline = source_files(root)
    record = create_document(root, root / "paper.tex")
    snapshot = create_snapshot(root, root / ".leanflow/author")
    units = []
    for item, name in zip(
        record["inventory"]["items"], ["Existing.double", "Existing.identity_nat"], strict=True
    ):
        unit = _unit(item["id"], [name], relative if file_reference else "")
        unit["declarations"][0].update(
            declaration_origin="existing_project", imports=["Main.Existing"]
        )
        units.append(unit)
    _write_proposal(snapshot, record, units)
    checked, receipt = _check_and_publish(root, record, snapshot)
    after = source_files(root)
    assert checked["files"] == {} and all(after[name] == data for name, data in baseline.items())
    assert {name for name in after if name.endswith(".lean")} == {
        name for name in baseline if name.endswith(".lean")
    }
    assert receipt["handoff"]["modules"] == {} and receipt["handoff"]["obligations"] == []
    profiles = checked["record"]["checks"]["profiles"]
    assert set(profiles) == {"Existing.double", "Existing.identity_nat"}
    for row in profiles.values():
        assert row["declaration_origin"] == "existing_project"
        assert row["file"] == relative and row["module"] == "Main.Existing"
        assert row["source_sha256"] == digest(baseline[relative])
        assert row["line_start"] in {2, 3}
        assert not row["pending"] and not row["conditional"]
    for unit in checked["record"]["units"]:
        ref = unit["declarations"][0]
        assert ref["file"] == relative and ref["declaration_origin"] == "existing_project"
    assert handoff.authorized_holes(receipt["handoff"]) == {}
    view = show_document(root, record["document_id"])
    artifact = next(row for row in view["artifacts"] if row["path"] == relative)
    resolved = resolve_artifact(root, record["document_id"], checked["revision_id"], artifact["id"])
    assert Path(resolved["path"]).read_bytes() == baseline[relative]
    assert view["handoff"]["obligations"] == [] and not view["can_prove"]


@pytest.mark.integration
def test_real_persistent_compiler_identity(formalization_project: Path) -> None:
    root = formalization_project
    manifest = environment.environment_manifest(root)
    assert manifest["contents"] and "lean-toolchain" in manifest["configuration"]
    assert any("lib/lean" in key or key.endswith("/lean") for key in manifest["contents"])
    assert identity(manifest) == environment.environment_identity(root)


@pytest.mark.integration
def test_direct_existing_root_definition_reuse_preserves_original_bytes(
    formalization_project: Path,
    cheap_persistent_identity: None,
    fresh_review_transport: list[Any],
) -> None:
    """Accept root declaration reuse when no generated-module registration modifies that root."""
    root = formalization_project
    main = root / "src/Main.lean"
    main.write_text(
        "def root_double (n : Nat) : Nat := n + n\n"
        "theorem unrelated_in_root : True := by sorry\n"
    )
    before = main.read_bytes()
    (root / "paper.tex").write_text(
        "\\begin{definition}For a natural number $n$, define $d(n)=n+n$.\\end{definition}\n"
    )
    record = create_document(root, root / "paper.tex")
    snapshot = create_snapshot(root, root / ".leanflow/root-reuse")
    unit = _unit(record["inventory"]["items"][0]["id"], ["root_double"])
    unit["declarations"][0].update(declaration_origin="existing_project", imports=["Main"])
    _write_proposal(snapshot, record, [unit])
    checked, receipt = _check_and_publish(root, record, snapshot)
    assert checked["files"] == {} and receipt["handoff"]["obligations"] == []
    assert main.read_bytes() == before
    row = checked["record"]["checks"]["profiles"]["root_double"]
    assert row["file"] == "src/Main.lean" and row["source_sha256"] == digest(before)


@pytest.mark.integration
def test_new_protocol_generation_after_publication_reviews_slice_before_full(
    formalization_project: Path,
    cheap_persistent_identity: None,
    fresh_review_transport: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compile a fresh generation while retaining older published files and exact proof scope."""
    from leanflow_cli.formalization import preparation, slice_gate
    from leanflow_cli.formalization.lean_layout import generation_layout
    from leanflow_cli.formalization.records import document_directory, save_revision

    root = formalization_project
    source = root / "paper.tex"
    source.write_text("\\begin{theorem}Every natural number $n$ equals itself.\\end{theorem}\n")
    original_source = source.read_bytes()
    old = create_document(root, source)
    layout = resolve_layout(root, old["document_id"])
    snapshot = create_snapshot(root, root / ".leanflow/old-author")
    file = layout["owned_prefix"] + "/Statement.lean"
    (snapshot / file).parent.mkdir(parents=True)
    (snapshot / file).write_text("theorem previous_identity (n : Nat) : n = n := rfl\n")
    (snapshot / layout["aggregator"]).write_text(f"import {layout['module_prefix']}.Statement\n")
    _write_proposal(
        snapshot,
        old,
        [_unit(old["inventory"]["items"][0]["id"], ["previous_identity"], file)],
    )
    _, old_receipt = _check_and_publish(root, old, snapshot)
    assert old_receipt["status"] == "published"
    previous_bytes = {
        name: data for name, data in source_files(root).items() if name != "src/Main.lean"
    }
    record = create_document(root, source)
    record.update(
        workflow_protocol=2,
        preparation_id="next-prepared-generation",
        preparation_plan_id="new-plan",
        slice_source_ids=[record["inventory"]["items"][0]["id"]],
    )
    record = save_revision(document_directory(root, record["document_id"]), record)
    monkeypatch.setattr(
        preparation,
        "assert_preparation_current",
        lambda *args, **kwargs: {"preparation_id": record["preparation_id"]},
    )
    fresh_layout = generation_layout(layout, record)
    assert fresh_layout["owned_prefix"] != layout["owned_prefix"]
    fresh_file = fresh_layout["owned_prefix"] + "/Statement.lean"
    name = fresh_layout["module_prefix"] + ".identity_nat"
    snapshot = create_snapshot(root, root / ".leanflow/new-author")
    (snapshot / fresh_file).parent.mkdir(parents=True)
    (snapshot / fresh_file).write_text(
        f"namespace {fresh_layout['module_prefix']}\n"
        "theorem identity_nat (n : Nat) : n = n := by sorry\n"
        f"end {fresh_layout['module_prefix']}\n"
    )
    (snapshot / fresh_layout["aggregator"]).write_text(
        f"import {fresh_layout['module_prefix']}.Statement\n"
    )
    units = [_unit(record["slice_source_ids"][0], [name], fresh_file)]
    baseline = source_files(root)
    metadata = publish.portable_baseline(root, record["document_id"])
    budget = CampaignLimits(root / ".leanflow/new-budget.json", {})

    def checked_and_reviewed(phase: str) -> tuple[dict[str, Any], dict[str, Any]]:
        _write_proposal(snapshot, record, units, phase=phase)
        checked = proposal.check_proposal(
            root=root,
            snapshot=snapshot,
            baseline=baseline,
            directory=root / ".leanflow/new-checks",
            document_id=record["document_id"],
            base_revision_id=record["revision_id"],
            selection_token=current_selection_token(root, record["document_id"]),
            allowed_axioms=AXIOMS,
        )
        assert checked["accepted"], checked
        reviewed = review.review_revision(
            checked["record"],
            backend="claude",
            model="fixture-reviewer",
            effort="max",
            environment={"LEANFLOW_PROJECT_ROOT": str(root)},
            directory=root / ".leanflow/new-reviews",
            budget=budget,
        )
        assert reviewed["accepted"], reviewed
        return checked, reviewed

    sliced, sliced_review = checked_and_reviewed("representative_slice")
    with pytest.raises(ValueError, match="representative slice"):
        publish.publish_checked(root, baseline, metadata, sliced, sliced_review, root / "slice-pub")
    assert source_files(root) == baseline
    slice_gate.save_reviewed_slice(root, sliced, sliced_review)
    complete, complete_review = checked_and_reviewed("complete")
    receipt = publish.publish_checked(
        root, baseline, metadata, complete, complete_review, root / ".leanflow/new-publish"
    )
    assert receipt["status"] == "published"
    assert [row["declaration"] for row in receipt["handoff"]["obligations"]] == [name]
    assert "previous_identity" not in complete["record"]["checks"]["profiles"]
    assert source.read_bytes() == original_source
    for relative, data in previous_bytes.items():
        if not relative.endswith("Blueprint.md"):
            assert (root / relative).read_bytes() == data
    assert (root / fresh_file).is_file()


@pytest.mark.integration
def test_rejected_translation_corrected_and_published_with_durable_prior_decision(
    formalization_project: Path,
    cheap_persistent_identity: None,
    fresh_review_transport: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real compiling but wrong mapping must be corrected before publication."""
    from leanflow_cli.formalization.records import (
        document_directory,
        rehydrate_document,
        save_receipt,
    )

    root = formalization_project
    (root / "paper.tex").write_text(
        "\\begin{theorem}For natural numbers $n,m$, $n+m=m+n$.\\end{theorem}\n"
    )
    record = create_document(root, root / "paper.tex")
    snapshot = create_snapshot(root, root / ".leanflow/author")
    item_id = record["inventory"]["items"][0]["id"]
    _write_proposal(snapshot, record, [_unit(item_id, ["Nat.add_assoc"])])
    checked = proposal.check_proposal(
        root=root,
        snapshot=snapshot,
        baseline=source_files(root),
        directory=root / ".leanflow/checks",
        document_id=record["document_id"],
        base_revision_id=record["revision_id"],
        selection_token=current_selection_token(root, record["document_id"], initialize=True),
        allowed_axioms=AXIOMS,
    )
    assert checked["accepted"]
    original_transport = review.run_harness

    def reject_wrong_mapping(request: Any) -> HarnessResult:
        result = original_transport(request)
        response = json.loads(result.final_response)
        for verdict in response["verdicts"]:
            verdict.update(
                decision="BLOCK", issues=["Associativity does not translate commutativity"]
            )
        response.update(coherence_decision="BLOCK", feedback="Use the commutativity declaration")
        return HarnessResult(status="completed", final_response=json.dumps(response))

    monkeypatch.setattr(review, "run_harness", reject_wrong_mapping)
    reviewed = review.review_revision(
        checked["record"],
        backend="claude",
        model="fixture-reviewer",
        effort="max",
        environment={"LEANFLOW_PROJECT_ROOT": str(root)},
        directory=root / ".leanflow/reviews",
        budget=CampaignLimits(root / ".leanflow/limits.json", {}),
    )
    assert not reviewed["accepted"]
    rejected_record = checked["record"]
    save_receipt(
        rejected_record,
        {
            "schema_version": 1,
            "document_id": record["document_id"],
            "revision_id": rejected_record["revision_id"],
            "status": "review_rejected",
            "reviews": reviewed["reviews"],
            "checks": rejected_record["checks"],
            "handoff": None,
        },
    )
    assert not (root / "formalization" / record["document_id"]).exists()
    monkeypatch.setattr(review, "run_harness", original_transport)
    _write_proposal(snapshot, record, [_unit(item_id, ["Nat.add_comm"])])
    accepted, published = _check_and_publish(root, record, snapshot)
    assert (
        published["status"] == "published"
        and accepted["revision_id"] != rejected_record["revision_id"]
    )
    assert load_receipt(rejected_record)["status"] == "review_rejected"
    assert "src/Main.lean" in accepted["record"]["context_lean_files"]
    portable = root / "formalization" / record["document_id"]
    negative = [
        json.loads(p.read_text())
        for p in (portable / "review-cache").glob("*.json")
        if not json.loads(p.read_text())["accepted"]
    ]
    assert negative and all(v["status"] == "completed" for v in negative)
    shutil.rmtree(root / ".leanflow/formalizations")
    restored = load_document(root, record["document_id"])
    private = document_directory(root, record["document_id"])
    rehydrate_document(restored, private)
    assert all((private / "review-cache" / (v["cache_key"] + ".json")).is_file() for v in negative)
