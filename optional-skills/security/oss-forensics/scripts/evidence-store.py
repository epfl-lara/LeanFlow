#!/usr/bin/env python3
"""
OSS Forensics Evidence Store Manager.

This legacy script is still covered by the repository test suite, so it remains
available under its historical path even though the broader optional-skills tree
is no longer a main product surface.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys

EVIDENCE_TYPES = [
    "git",
    "gh_api",
    "gh_archive",
    "web_archive",
    "ioc",
    "analysis",
    "manual",
    "vendor_report",
]

VERIFICATION_STATES = ["unverified", "single_source", "multi_source_verified"]

IOC_TYPES = [
    "COMMIT_SHA",
    "FILE_PATH",
    "API_KEY",
    "SECRET",
    "IP_ADDRESS",
    "DOMAIN",
    "PACKAGE_NAME",
    "ACTOR_USERNAME",
    "MALICIOUS_URL",
    "WORKFLOW_FILE",
    "BRANCH_NAME",
    "TAG_NAME",
    "RELEASE_NAME",
    "OTHER",
]


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds") + "Z"


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class EvidenceStore:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.data = {
            "metadata": {
                "version": "2.0",
                "created_at": _now_iso(),
                "last_updated": _now_iso(),
                "investigation": "",
                "target_repo": "",
            },
            "evidence": [],
            "chain_of_custody": [],
        }
        if os.path.exists(filepath):
            try:
                with open(filepath, encoding="utf-8") as handle:
                    self.data = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"Error loading evidence store '{filepath}': {exc}", file=sys.stderr)
                print("Hint: The file might be corrupted. Check for manual edits or syntax errors.", file=sys.stderr)
                raise SystemExit(1) from exc

    def _save(self) -> None:
        self.data["metadata"]["last_updated"] = _now_iso()
        with open(self.filepath, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, ensure_ascii=False)

    def _next_id(self) -> str:
        return f"EV-{len(self.data['evidence']) + 1:04d}"

    def add(
        self,
        source: str,
        content: str,
        evidence_type: str,
        actor: str | None = None,
        url: str | None = None,
        timestamp: str | None = None,
        ioc_type: str | None = None,
        verification: str = "unverified",
        notes: str | None = None,
    ) -> str:
        evidence_id = self._next_id()
        entry = {
            "id": evidence_id,
            "type": evidence_type,
            "source": source,
            "content": content,
            "content_sha256": _sha256(content),
            "actor": actor,
            "url": url,
            "event_timestamp": timestamp,
            "collected_at": _now_iso(),
            "ioc_type": ioc_type,
            "verification": verification,
            "notes": notes,
        }
        self.data["evidence"].append(entry)
        self.data["chain_of_custody"].append(
            {
                "action": "add",
                "evidence_id": evidence_id,
                "timestamp": _now_iso(),
                "source": source,
            }
        )
        self._save()
        return evidence_id

    def list_evidence(self, filter_type: str | None = None, filter_actor: str | None = None):
        results = self.data["evidence"]
        if filter_type:
            results = [entry for entry in results if entry.get("type") == filter_type]
        if filter_actor:
            results = [entry for entry in results if entry.get("actor") == filter_actor]
        return results

    def verify_integrity(self):
        issues = []
        for entry in self.data["evidence"]:
            expected = _sha256(entry["content"])
            stored = entry.get("content_sha256", "")
            if expected != stored:
                issues.append(
                    {
                        "id": entry["id"],
                        "stored_sha256": stored,
                        "computed_sha256": expected,
                    }
                )
        return issues

    def query(self, keyword: str):
        keyword_lower = keyword.lower()
        return [
            entry
            for entry in self.data["evidence"]
            if keyword_lower in (entry.get("content", "") or "").lower()
            or keyword_lower in (entry.get("source", "") or "").lower()
            or keyword_lower in (entry.get("actor", "") or "").lower()
            or keyword_lower in (entry.get("url", "") or "").lower()
        ]

    def export_markdown(self) -> str:
        lines = [
            "# Evidence Registry",
            "",
            f"**Store**: `{self.filepath}`",
            f"**Last Updated**: {self.data['metadata'].get('last_updated', 'N/A')}",
            f"**Total Evidence Items**: {len(self.data['evidence'])}",
            "",
            "| ID | Type | Source | Actor | Verification | Event Timestamp | URL |",
            "|----|------|--------|-------|--------------|-----------------|-----|",
        ]
        for entry in self.data["evidence"]:
            url = entry.get("url") or ""
            url_display = f"[link]({url})" if url else ""
            lines.append(
                f"| {entry['id']} | {entry.get('type', '')} | {entry.get('source', '')} "
                f"| {entry.get('actor') or ''} | {entry.get('verification', '')} "
                f"| {entry.get('event_timestamp') or ''} | {url_display} |"
            )
        lines.append("")
        lines.append("## Chain of Custody")
        lines.append("")
        lines.append("| Evidence ID | Action | Timestamp | Source |")
        lines.append("|-------------|--------|-----------|--------|")
        for entry in self.data["chain_of_custody"]:
            lines.append(
                f"| {entry.get('evidence_id', '')} | {entry.get('action', '')} "
                f"| {entry.get('timestamp', '')} | {entry.get('source', '')} |"
            )
        return "\n".join(lines)

    def summary(self) -> dict:
        by_type: dict[str, int] = {}
        by_verification: dict[str, int] = {}
        actors: set[str] = set()
        for entry in self.data["evidence"]:
            evidence_type = entry.get("type", "unknown")
            by_type[evidence_type] = by_type.get(evidence_type, 0) + 1
            verification = entry.get("verification", "unverified")
            by_verification[verification] = by_verification.get(verification, 0) + 1
            if entry.get("actor"):
                actors.add(entry["actor"])
        return {
            "total": len(self.data["evidence"]),
            "by_type": by_type,
            "by_verification": by_verification,
            "unique_actors": sorted(actors),
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OSS Forensics Evidence Store Manager v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--store", default="evidence.json", help="Path to evidence JSON file (default: evidence.json)")

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    add_parser = subparsers.add_parser("add", help="Add a new evidence entry")
    add_parser.add_argument("--source", required=True, help="Where this evidence came from")
    add_parser.add_argument("--content", required=True, help="The evidence content")
    add_parser.add_argument("--type", required=True, choices=EVIDENCE_TYPES, dest="evidence_type", help="Evidence type")
    add_parser.add_argument("--actor", help="Associated actor")
    add_parser.add_argument("--url", help="URL to original source")
    add_parser.add_argument("--timestamp", help="When the event occurred (ISO 8601)")
    add_parser.add_argument("--ioc-type", choices=IOC_TYPES, help="IOC subtype (for --type ioc)")
    add_parser.add_argument("--verification", choices=VERIFICATION_STATES, default="unverified")
    add_parser.add_argument("--notes", help="Additional investigator notes")
    add_parser.add_argument("--quiet", action="store_true", help="Suppress success message")

    list_parser = subparsers.add_parser("list", help="List all evidence entries")
    list_parser.add_argument("--type", dest="filter_type", choices=EVIDENCE_TYPES, help="Filter by type")
    list_parser.add_argument("--actor", dest="filter_actor", help="Filter by actor")

    subparsers.add_parser("verify", help="Verify SHA-256 integrity of all evidence content")

    query_parser = subparsers.add_parser("query", help="Search evidence by keyword")
    query_parser.add_argument("keyword", help="Keyword to search for")

    subparsers.add_parser("export", help="Export evidence as a Markdown table (stdout)")
    subparsers.add_parser("summary", help="Print investigation statistics")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        raise SystemExit(0)

    store = EvidenceStore(args.store)

    if args.command == "add":
        evidence_id = store.add(
            source=args.source,
            content=args.content,
            evidence_type=args.evidence_type,
            actor=args.actor,
            url=args.url,
            timestamp=args.timestamp,
            ioc_type=args.ioc_type,
            verification=args.verification,
            notes=args.notes,
        )
        if not getattr(args, "quiet", False):
            print(f"Added evidence: {evidence_id}")
        return

    if args.command == "list":
        items = store.list_evidence(
            filter_type=getattr(args, "filter_type", None),
            filter_actor=getattr(args, "filter_actor", None),
        )
        if not items:
            print("No evidence found.")
            return
        for entry in items:
            actor_text = f" | actor: {entry['actor']}" if entry.get("actor") else ""
            url_text = f" | {entry['url']}" if entry.get("url") else ""
            print(f"[{entry['id']}] {entry['type']:12s} | {entry['verification']:20s} | {entry['source']}{actor_text}{url_text}")
        return

    if args.command == "verify":
        issues = store.verify_integrity()
        if not issues:
            print(f"All {len(store.data['evidence'])} evidence entries passed SHA-256 integrity check.")
            return
        print(f"{len(issues)} integrity issue(s) detected:")
        for issue in issues:
            print(f"  [{issue['id']}] stored={issue['stored_sha256'][:16]}... computed={issue['computed_sha256'][:16]}...")
        raise SystemExit(1)

    if args.command == "query":
        results = store.query(args.keyword)
        print(f"Found {len(results)} result(s) for '{args.keyword}':")
        for entry in results:
            print(f"  [{entry['id']}] {entry['type']} | {entry['source']} | {entry['content'][:80]}")
        return

    if args.command == "export":
        print(store.export_markdown())
        return

    if args.command == "summary":
        summary = store.summary()
        print(f"Total evidence items : {summary['total']}")
        print(f"By type              : {json.dumps(summary['by_type'], indent=2)}")
        print(f"By verification      : {json.dumps(summary['by_verification'], indent=2)}")
        print(f"Unique actors        : {summary['unique_actors']}")


if __name__ == "__main__":
    main()
