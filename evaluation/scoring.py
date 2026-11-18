"""Adjudicate decision, finding, evidence, and ticket quality against frozen labels."""

import json
from collections import Counter
from statistics import median

from .oracle import check_counterexample

STATUS_TYPES = {
    "PASS": "valid",
    "CHANGES_REQUIRED": "defective",
    "INCOMPLETE": "requires_incomplete",
}

_RECORD_COLLECTIONS = {
    "requirements": "rules",
    "approvals": "records",
    "specification": "rules",
    "workflow": "nodes",
    "snippets": "items",
}


def locator(ref):
    if not isinstance(ref, dict):
        return None
    artifact, version, location = (ref.get(key) for key in ("artifact", "version", "location"))
    if not all(isinstance(s, str) and s for s in (artifact, version, location)):
        return None
    prefix = f"/{artifact}/"
    if location.startswith(prefix):
        location = location[len(prefix) - 1:]
    return artifact, version, location


def _pointer_value(section, location, artifact):
    parts = [part.replace("~1", "/").replace("~0", "~") for part in location[1:].split("/")]
    current = section
    for index, part in enumerate(parts):
        if isinstance(current, dict):
            if part not in current:
                missing_branch = (artifact == "workflow" and len(parts) == 3 and index == 2
                                  and parts[0] == "nodes" and current.get("kind") == "decision"
                                  and part in ("true", "false"))
                if missing_branch:
                    return [current, None]
                return []
            current = current[part]
        elif isinstance(current, list) and part.isascii() and part.isdecimal():
            position = int(part)
            if position >= len(current):
                return []
            current = current[position]
        else:
            return []
    return [current]


def _located_values(bundle, key):
    artifact, version, location = key
    section = bundle.get(artifact)
    if artifact not in _RECORD_COLLECTIONS or section is None or section["version"] != version:
        return []
    if location == "/":
        return [section]
    if location.startswith("/"):
        return _pointer_value(section, location, artifact)
    matches = [record for record in section[_RECORD_COLLECTIONS[artifact]]
               if record.get("location") == location]
    return matches if len(matches) == 1 else []


def _excerpt_matches(text, value):
    """Accept a literal artifact excerpt or JSON that reconstructs the located data."""
    if not isinstance(text, str) or not text.strip():
        return False
    quote = text.strip()
    try:
        decoded = json.loads(quote)
    except (ValueError, RecursionError):
        pass
    else:
        if type(decoded) is type(value) and decoded == value:
            return True
    if isinstance(value, str):
        content = value.strip()
        return quote == content or len(quote) >= 5 and quote in content
    if isinstance(value, dict):
        for key in ("text", "source"):
            content = value.get(key)
            if isinstance(content, str) and _excerpt_matches(quote, content):
                return True
    return False


def grounded(bundle, ref):
    key = locator(ref)
    return key is not None and any(_excerpt_matches(ref.get("excerpt"), value)
                                   for value in _located_values(bundle, key))


def _severities(target):
    return ("blocker",) if target["category"] == "defect" else ("warning", "info")


def _assess_match(finding, target, case, bundle):
    found_at = locator(finding)
    correct_location = found_at in {locator(item) for item in target["locations"]}
    source_citations = finding.get("evidence")
    citations = source_citations if isinstance(source_citations, list) else []
    authenticated_citations = [ref for ref in citations if grounded(bundle, ref)]
    grounded_citations = {locator(ref) for ref in authenticated_citations}
    complete = all(
        grounded_citations & {locator(ref) for ref in alternatives}
        for alternatives in target["citation_groups"]
    )
    witness = None
    layer = target["witness_layer"]
    if layer is not None:
        witness = check_counterexample(finding.get("counterexample"), bundle["fields"],
                                       case["routing_oracle"], layer)
    correction = finding.get("correction")
    return {
        "target": target["name"],
        "correct_location": correct_location,
        "supported_evidence": complete,
        "invented_or_unmatched_citations": len(citations) - len(authenticated_citations),
        "counterexample": witness,
        "correction_provided": isinstance(correction, str) and bool(correction.strip()),
    }


def finding_quality(case, bundle, findings):
    targets = case["finding_targets"]
    matches_by_target = {target["name"]: [] for target in targets}
    finding_results = []
    for position, finding in enumerate(findings):
        matches = [
            _assess_match(finding, target, case, bundle)
            for target in targets
            if finding.get("code") in target["codes"]
            and finding.get("rule_id") in target["rule_ids"]
            and finding.get("severity") in _severities(target)
        ]
        for match in matches:
            matches_by_target[match["target"]].append(match)
        finding_results.append({
            "ordinal": position + 1,
            "code": finding.get("code"),
            "rule_id": finding.get("rule_id"),
            "severity": finding.get("severity"),
            "supported": any(item["supported_evidence"] and item["invented_or_unmatched_citations"] == 0
                             for item in matches),
            "matches": matches,
        })

    target_results = []
    for target in targets:
        candidates = matches_by_target[target["name"]]
        witness = target["witness_layer"] is not None
        supported = [item for item in candidates
                     if item["supported_evidence"] and item["invented_or_unmatched_citations"] == 0]
        located_and_supported = [item for item in supported if item["correct_location"]]
        target_results.append({
            "target": target["name"],
            "category": target["category"],
            "required": target["required"],
            "identified": bool(candidates),
            "correct_location": any(item["correct_location"] for item in candidates),
            "supported_evidence": bool(supported),
            "backed_finding": bool(located_and_supported),
            "correction_provided": any(item["correction_provided"] for item in candidates),
            "witness_eligible": witness,
            "counterexample_provided": any(item["counterexample"] not in (None, "absent")
                                          for item in candidates),
            "counterexample_verified": any(item["counterexample"] == "verified" for item in candidates),
            "fully_supported_with_witness": any(item["counterexample"] == "verified"
                                                for item in located_and_supported),
        })
    return {"targets": target_results, "findings": finding_results}


def coverage_quality(bundle, report):
    coverage = report.get("coverage", {})
    if isinstance(coverage, dict):
        rows = coverage.get("rules", [])
        gaps = coverage.get("unreviewed", [])
    else:
        rows = coverage
        gaps = [row for row in rows if row.get("status") == "unreviewed"]
    required = {row["id"] for row in bundle["requirements"]["rules"]}
    reviewed = {row["rule_id"] for row in rows if row.get("status") in {"PASS", "checked"}}
    return {"reviewed_source_rules": len(required & reviewed), "source_rules": len(required),
            "declared_gaps": len(gaps), "all_source_rules_reviewed": required <= reviewed and not gaps}


def model_resources(rows):
    resources = {}
    for cohort in ("no_model", "fake_requested", "real_model"):
        selected = [row for row in rows if row["runtime"]["cohort"] == cohort]
        elapsed = [row["runtime"]["runtime_ms"] for row in selected]
        resources[cohort] = {
            "cases": len(selected),
            "runtime_total_ms": round(sum(elapsed), 3),
            "runtime_median_ms": round(median(elapsed), 3) if elapsed else None,
            "model_calls": sum(row["runtime"]["model_calls"] for row in selected),
            "model_retries": sum(row["runtime"]["model_retries"] for row in selected),
            "verified_fake_traces": sum(row["runtime"]["fake_trace_verified"] is True for row in selected),
            "fake_checks_exercised": sum(row["runtime"]["fake_trace_verified"] is not None
                                         for row in selected),
        }
    return resources


def _rate(numerator, denominator):
    return {"count": numerator, "total": denominator}


def summarize(rows):
    typed = {
        case_type: [row for row in rows if row["expected_status"] == status]
        for status, case_type in STATUS_TYPES.items()
    }
    observed = [row["actual_status"] for row in rows]
    targets = [item for row in rows for item in row["quality"]["targets"] if item["required"]]
    behavior = [item for item in targets if item["witness_eligible"]]
    reported = [item for row in rows for item in row["quality"]["findings"]]
    valid = typed["valid"]
    defective = typed["defective"]
    incomplete = typed["requires_incomplete"]
    return {
        "cases": len(rows),
        "exact_decisions": _rate(sum(row["expected_status"] == row["actual_status"] for row in rows),
                                 len(rows)),
        "observed_status": {status: observed.count(status) for status in (*STATUS_TYPES, "ERROR")},
        "by_expected_case_type": {
            case_type: {
                "cases": len(selected),
                "observed_status": {status: sum(row["actual_status"] == status for row in selected)
                                    for status in (*STATUS_TYPES, "ERROR")},
            }
            for case_type, selected in typed.items()
        },
        "false_passes": _rate(sum(row["actual_status"] == "PASS" for row in defective), len(defective)),
        "false_blocks": _rate(sum(row["actual_status"] == "CHANGES_REQUIRED" for row in valid), len(valid)),
        "incompletes_by_case_type": {
            case_type: _rate(sum(row["actual_status"] == "INCOMPLETE" for row in selected), len(selected))
            for case_type, selected in typed.items()
        },
        "passed_incomplete_cases": _rate(sum(row["actual_status"] == "PASS" for row in incomplete),
                                         len(incomplete)),
        "findings": {
            "required_targets_by_category": dict(Counter(target["category"] for target in targets)),
            "targets_identified": _rate(sum(target["identified"] for target in targets), len(targets)),
            "targets_correctly_located": _rate(sum(target["correct_location"] for target in targets),
                                               len(targets)),
            "targets_with_supported_evidence": _rate(sum(target["supported_evidence"] for target in targets),
                                                       len(targets)),
            "targets_with_located_supported_finding": _rate(sum(target["backed_finding"] for target in targets),
                                                              len(targets)),
            "targets_with_correction": _rate(sum(target["correction_provided"] for target in targets),
                                             len(targets)),
            "unsupported_reported_findings": _rate(sum(not finding["supported"] for finding in reported),
                                                   len(reported)),
            "counterexamples_provided": _rate(sum(target["counterexample_provided"] for target in behavior),
                                               len(behavior)),
            "counterexamples_verified": _rate(sum(target["counterexample_verified"] for target in behavior),
                                               len(behavior)),
            "located_supported_findings_with_verified_counterexample": _rate(
                sum(target["fully_supported_with_witness"] for target in behavior), len(behavior)
            ),
        },
        "coverage": {
            "passes_covering_all_source_rules": _rate(
                sum(row["actual_status"] == "PASS" and row["coverage"]["all_source_rules_reviewed"]
                    for row in rows), observed.count("PASS")
            ),
            "incompletes_disclosing_gaps": _rate(
                sum(row["actual_status"] == "INCOMPLETE" and row["coverage"]["declared_gaps"] > 0
                    for row in rows), observed.count("INCOMPLETE")
            ),
        },
        "resources": model_resources(rows),
    }
