"""Evidence registration and deterministic report assembly."""

from __future__ import annotations

import json
from typing import Any, Iterable

from .logic import Ticket, encode_ticket


CHECK_DETAILS = {
    "input_contract": "Validate versioned bundle metadata and declared ticket fields.",
    "effective_requirements": "Parse anchored rules and apply scoped approval records.",
    "specification": "Compare ordered specification behavior and source rule coverage.",
    "workflow": "Compare reachable paths, branches, terminations, and routes.",
    "code_syntax": "Parse supplied snippets with the Python syntax parser.",
    "code_inspection": "Validate the side-effect-free Python subset and linked evidence.",
    "code_behavior": "Interpret inspected snippets on reachable tickets.",
    "semantic_domain": "Construct the finite semantic partition of declared ticket inputs.",
}
Ref = dict[str, str]


def locator(ref: Ref) -> tuple[str, str, str]:
    return ref["artifact"], ref["version"], ref["location"]


def excerpt(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(", ", ": "))
    except (TypeError, ValueError, RecursionError, OverflowError):
        return "Artifact content could not be represented as JSON."


class Report:
    def __init__(self):
        self.checks = {
            check: {"id": check, "status": "PASS", "detail": detail}
            for check, detail in CHECK_DETAILS.items()
        }
        self.evidence: list[Ref] = []
        self.findings: list[dict] = []
        self.unreviewed: list[dict] = []
        self._references: dict[tuple[str, str, str], Ref] = {}
        self._finding_keys: set[tuple] = set()

    def ref(
        self, artifact: str, version: str, location: str, raw: Any, *, rule_id: str | None = None
    ) -> Ref:
        ref = {
            "artifact": artifact,
            "version": version,
            "location": location,
            "excerpt": excerpt(raw),
        }
        if rule_id is not None:
            ref["rule_id"] = rule_id
        key = locator(ref)
        existing = self._references.get(key)
        if existing is not None:
            self.issue(
                "input_contract",
                "EVIDENCE_LOCATION_CONFLICT",
                "Two evidence records use the same artifact, version and location.",
                existing,
                "Assign distinct locations to the evidence records.",
                refs=[ref],
            )
            return ref
        self._references[key] = ref
        self.evidence.append(ref)
        return ref

    def issue(
        self,
        check: str,
        code: str,
        message: str,
        ref: Ref,
        correction: str,
        *,
        rule_id: str | None = None,
        refs: Iterable[Ref] = (),
        blocker: bool = False,
        ticket: Ticket | None = None,
        expected_route: str | None = None,
        actual_route: str | None = None,
        details: dict | None = None,
    ) -> None:
        rule_id = rule_id if rule_id is not None else ref.get("rule_id")
        key = code, rule_id, *locator(ref)
        if key in self._finding_keys:
            return
        self._finding_keys.add(key)
        status = "CHANGES_REQUIRED" if blocker else "INCOMPLETE"
        if self.checks[check]["status"] != "CHANGES_REQUIRED":
            self.checks[check]["status"] = status
        evidence = []
        seen = set()
        for item in [ref, *refs]:
            identity = (*locator(item), item["excerpt"])
            if identity not in seen:
                seen.add(identity)
                evidence.append(item)
        finding = {
            "id": f"F{len(self.findings) + 1:03d}",
            "code": code,
            "severity": "blocker" if blocker else "warning",
            "rule_id": rule_id,
            "artifact": ref["artifact"],
            "version": ref["version"],
            "location": ref["location"],
            "message": message,
            "evidence": evidence,
            "correction": correction,
        }
        if ticket is not None:
            finding["counterexample"] = {
                "ticket": encode_ticket(ticket),
                "expected_route": expected_route,
                "actual_route": actual_route,
            }
        if details is not None:
            finding["details"] = details
        self.findings.append(finding)
        if not blocker:
            self.unreviewed.append(
                {
                    "code": code,
                    "check": check,
                    "detail": message,
                    "rule_id": rule_id,
                    "artifact": ref["artifact"],
                    "version": ref["version"],
                    "location": ref["location"],
                }
            )

    def finish(self, rules: list[dict], domain: dict, extra: dict) -> dict:
        blockers = {item["rule_id"] for item in self.findings if item["severity"] == "blocker"}
        incomplete = {item["rule_id"] for item in self.unreviewed}
        for item in rules:
            rule_id = item["rule_id"]
            if rule_id in blockers:
                item["status"] = "CHANGES_REQUIRED"
            elif item["unreviewed_checks"] or rule_id in incomplete or None in incomplete:
                item["status"] = "INCOMPLETE"
            else:
                item["status"] = "PASS"

        if blockers:
            status = "CHANGES_REQUIRED"
        elif self.unreviewed:
            status = "INCOMPLETE"
        else:
            status = "PASS"
        if status == "PASS":
            summary = (
                "The declared scope was reviewed with no blocking discrepancy."
                if domain["exhaustive"]
                else "No blocking discrepancy was found in the cells retained by the ablation."
            )
        elif status == "INCOMPLETE":
            summary = f"{len(self.unreviewed)} review gap(s) prevent a complete decision."
        else:
            count = sum(item["severity"] == "blocker" for item in self.findings)
            summary = f"{count} blocking discrepancy/discrepancies and {len(self.unreviewed)} review gap(s)."

        return {
            "status": status,
            "summary": summary,
            "findings": self.findings,
            "checks": list(self.checks.values()),
            "coverage": {
                "rules": rules,
                "unreviewed": self.unreviewed,
                "domain": domain,
                **extra,
            },
            "evidence": self.evidence,
        }
