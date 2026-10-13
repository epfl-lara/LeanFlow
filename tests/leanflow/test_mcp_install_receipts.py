"""Reject stale or unverified immutable MCP installation receipts."""

from __future__ import annotations

import json

import pytest

from leanflow_cli.cli import mcp_install_receipts as receipts


@pytest.fixture
def sealed_environment(tmp_path):
    venv = tmp_path / "venv"
    command = venv / "bin/server"
    command.parent.mkdir(parents=True)
    command.write_text("console content")
    patch = venv / "lib/python3.12/site-packages/backend/module.py"
    patch.parent.mkdir(parents=True)
    patch.write_text("patched module content")
    metadata = patch.parent.parent / "backend-1.0.dist-info/METADATA"
    metadata.parent.mkdir()
    metadata.write_text("Name: backend\nVersion: 1.0\n")
    fingerprint = receipts.mcp_install_fingerprint({"package": "backend==1.0"}, ["patch code"])
    kwargs = {
        "expected_fingerprint": fingerprint,
        "packages": ("backend",),
        "arguments": ("--test",),
    }
    receipts.write_managed_mcp_receipt(
        venv,
        **kwargs,
        installed_files=(command, patch),
        validation={"console_script": True, "fastmcp": True, "initialized": True},
    )
    return venv, command, patch, metadata, kwargs


def test_unchanged_receipt_preserves_validated_environment(sealed_environment):
    venv, command, patch, _metadata, kwargs = sealed_environment
    assert receipts.managed_mcp_receipt_status(venv, **kwargs, installed_files=(command, patch))[
        "current"
    ]
    document = (venv / receipts.RECEIPT_NAME).read_text()
    assert "--test" not in document
    with pytest.raises(FileExistsError, match="immutable"):
        receipts.write_managed_mcp_receipt(
            venv,
            **kwargs,
            installed_files=(command, patch),
            validation={"console_script": True, "fastmcp": True, "initialized": True},
        )
    assert (venv / receipts.RECEIPT_NAME).read_text() == document


@pytest.mark.parametrize(
    "change",
    ["patch", "provenance", "ambiguous-metadata", "new-file", "escape", "invalid-json", "symlink"],
)
def test_receipt_rejects_changed_or_ambiguous_content(sealed_environment, tmp_path, change):
    venv, command, patch, metadata, kwargs = sealed_environment
    receipt = venv / receipts.RECEIPT_NAME
    installed_files = (command, patch)
    if change == "patch":
        patch.write_text("altered installed patch")
    elif change == "provenance":
        metadata.with_name("direct_url.json").write_text('{"url":"different-origin"}')
    elif change == "ambiguous-metadata":
        duplicate = metadata.parent.parent / "backend-2.0.dist-info/METADATA"
        duplicate.parent.mkdir()
        duplicate.write_text("Name: backend\nVersion: 2.0\n")
    elif change == "new-file":
        new_file = patch.with_name("extra_patch.py")
        new_file.write_text("new package patch file")
        installed_files = (command, patch, new_file)
    elif change == "escape":
        outside = tmp_path / "outside"
        outside.write_text("private")
        payload = json.loads(receipt.read_text())
        payload["files"]["../outside"] = "invalid"
        receipt.write_text(json.dumps(payload))
    elif change == "invalid-json":
        receipt.write_text("[]")
    else:
        outside = tmp_path / "outside-receipt"
        receipt.replace(outside)
        receipt.symlink_to(outside)
    assert not receipts.managed_mcp_receipt_status(venv, **kwargs, installed_files=installed_files)[
        "current"
    ]


def test_receipt_cannot_attest_incomplete_initialization(tmp_path):
    with pytest.raises(ValueError, match="required checks"):
        receipts.write_managed_mcp_receipt(
            tmp_path,
            expected_fingerprint="spec",
            packages=(),
            installed_files=(),
            arguments=(),
            validation={"console_script": True, "fastmcp": True, "initialized": False},
        )
    assert not (tmp_path / receipts.RECEIPT_NAME).exists()
