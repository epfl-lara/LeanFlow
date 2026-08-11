"""Authenticate immutable requested-root registries without live filesystem reads."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.workflows import plan_state

CAMPAIGN_ROOTS_FIELD = "negation_promotion_requested_roots"
CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD = "negation_promotion_root_registration_open"

_REGISTRY_FIELDS = frozenset({"version", "campaign_id", "roots", "registry_sha256"})
_ROOT_FIELDS = frozenset(
    {
        "campaign_id",
        "theorem",
        "operation_path",
        "node_id",
        "graph_node_name",
        "graph_node_file",
        "declaration_signature_sha256",
        "initial_source_revision_sha256",
        "root_identity_sha256",
    }
)
_ROOT_IDENTITY_FIELDS = (
    "campaign_id",
    "theorem",
    "operation_path",
    "node_id",
    "graph_node_name",
    "graph_node_file",
    "declaration_signature_sha256",
    "initial_source_revision_sha256",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CampaignRootRegistryAudit:
    """Report strict authentication of one sealed requested-root registry."""

    ok: bool
    reason: str
    campaign_id: str = ""
    roots: tuple[Mapping[str, Any], ...] = ()


def _sha256_json(payload: object) -> str:
    """Hash one JSON-compatible identity with canonical separators."""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _nonempty_string(value: object) -> str:
    """Return one exact non-empty string, rejecting coercible impostors."""
    return value.strip() if isinstance(value, str) else ""


def _valid_sha256(value: object) -> bool:
    """Return whether a value is one lowercase SHA-256 digest."""
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _lexical_absolute_file(value: object) -> str:
    """Return a normalized absolute path without resolving live filesystem state."""
    raw = _nonempty_string(value)
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        return ""
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        return ""
    return str(path)


def campaign_root_identity_payload(root: Mapping[str, Any]) -> dict[str, str]:
    """Build the exact immutable identity sealed by one root record."""
    return {field: str(root[field]) for field in _ROOT_IDENTITY_FIELDS}


def campaign_root_registry_sha256(roots: Sequence[Mapping[str, Any]]) -> str:
    """Hash an ordered, already-authenticated root registry."""
    payload = [
        {
            **campaign_root_identity_payload(root),
            "root_identity_sha256": str(root["root_identity_sha256"]),
        }
        for root in roots
    ]
    return _sha256_json(payload)


def audit_campaign_root_registry(campaign: object) -> CampaignRootRegistryAudit:
    """Authenticate every raw requested-root element without filtering evidence."""
    if not isinstance(campaign, Mapping):
        return CampaignRootRegistryAudit(False, "campaign root authority is not a mapping")
    if campaign.get(CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD) is not False:
        return CampaignRootRegistryAudit(
            False, "requested-root registration has no sealed fresh-campaign origin"
        )
    provider_turn_nonce = campaign.get("provider_turn_nonce")
    if type(provider_turn_nonce) is not int or provider_turn_nonce < 0:
        return CampaignRootRegistryAudit(
            False, "requested-root registration has invalid fresh-campaign provider provenance"
        )
    campaign_id = _nonempty_string(campaign.get("campaign_id"))
    if not campaign_id:
        return CampaignRootRegistryAudit(False, "campaign identity is missing")
    registry = campaign.get(CAMPAIGN_ROOTS_FIELD)
    if not isinstance(registry, Mapping):
        return CampaignRootRegistryAudit(False, "requested campaign root registry is missing")
    if set(registry) != _REGISTRY_FIELDS:
        return CampaignRootRegistryAudit(
            False, "requested campaign root registry has missing or unknown fields"
        )
    if type(registry.get("version")) is not int or registry.get("version") != 1:
        return CampaignRootRegistryAudit(
            False, "requested campaign root registry has unknown version"
        )
    if _nonempty_string(registry.get("campaign_id")) != campaign_id:
        return CampaignRootRegistryAudit(False, "requested-root registry campaign identity changed")
    raw_roots = registry.get("roots")
    if not isinstance(raw_roots, list):
        return CampaignRootRegistryAudit(
            False, "requested campaign root registry roots are not a list"
        )
    roots: list[Mapping[str, Any]] = []
    semantic_identities: set[tuple[str, str, str]] = set()
    for index, raw_root in enumerate(raw_roots):
        if not isinstance(raw_root, Mapping):
            return CampaignRootRegistryAudit(
                False, f"requested campaign root {index} is not a mapping"
            )
        if set(raw_root) != _ROOT_FIELDS:
            return CampaignRootRegistryAudit(
                False, f"requested campaign root {index} has missing or unknown fields"
            )
        if not all(isinstance(raw_root[field], str) for field in _ROOT_FIELDS):
            return CampaignRootRegistryAudit(
                False, f"requested campaign root {index} has non-string identity fields"
            )
        if any(not _nonempty_string(raw_root[field]) for field in _ROOT_FIELDS):
            return CampaignRootRegistryAudit(
                False, f"requested campaign root {index} has empty identity fields"
            )
        if raw_root["campaign_id"] != campaign_id:
            return CampaignRootRegistryAudit(
                False, f"requested campaign root {index} changed campaign identity"
            )
        if raw_root["theorem"] != raw_root["graph_node_name"]:
            return CampaignRootRegistryAudit(
                False, f"requested campaign root {index} changed theorem identity"
            )
        if not _lexical_absolute_file(raw_root["operation_path"]):
            return CampaignRootRegistryAudit(
                False, f"requested campaign root {index} has non-canonical source identity"
            )
        if raw_root["node_id"] != plan_state.node_id_for(
            raw_root["graph_node_name"], raw_root["graph_node_file"]
        ):
            return CampaignRootRegistryAudit(
                False, f"requested campaign root {index} has non-deterministic graph identity"
            )
        for field in (
            "declaration_signature_sha256",
            "initial_source_revision_sha256",
            "root_identity_sha256",
        ):
            if not _valid_sha256(raw_root[field]):
                return CampaignRootRegistryAudit(
                    False, f"requested campaign root {index} has invalid {field}"
                )
        if raw_root["root_identity_sha256"] != _sha256_json(
            campaign_root_identity_payload(raw_root)
        ):
            return CampaignRootRegistryAudit(
                False, f"requested campaign root {index} has forged identity seal"
            )
        semantic_identity = (
            raw_root["theorem"],
            raw_root["operation_path"],
            raw_root["node_id"],
        )
        if semantic_identity in semantic_identities:
            return CampaignRootRegistryAudit(
                False, "requested campaign root registry is semantically ambiguous"
            )
        semantic_identities.add(semantic_identity)
        roots.append(raw_root)
    if roots != sorted(roots, key=lambda root: (root["operation_path"], root["theorem"])):
        return CampaignRootRegistryAudit(
            False, "requested campaign root registry order is ambiguous"
        )
    registry_sha256 = registry.get("registry_sha256")
    if not _valid_sha256(registry_sha256) or registry_sha256 != campaign_root_registry_sha256(
        roots
    ):
        return CampaignRootRegistryAudit(
            False, "requested campaign root registry is unauthenticated"
        )
    return CampaignRootRegistryAudit(
        True,
        "requested campaign roots are registered",
        campaign_id=campaign_id,
        roots=tuple(roots),
    )
