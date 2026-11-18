"""Initial checkpoint: compare simple written rules with a linear decision chain."""

import argparse
import json
import re
from pathlib import Path


_COMPARISON = re.compile(
    r'([A-Za-z_]\w*)\s*(<=|>=|==|!=|<|>)\s*'
    r'("(?:[^"\\]|\\.)*"|true|false|-?\d+(?:\.\d+)?)'
    r'(?:\s+([A-Za-z]+))?'
)
_PRESENCE = re.compile(r"([A-Za-z_]\w*) IS (MISSING|PRESENT)")


def parse_rule(text):
    condition, delimiter, route = text.rpartition(" THEN ")
    if not delimiter or not re.fullmatch(r"[A-Za-z_][\w.-]*", route):
        raise ValueError("Expected a single condition and a route")
    if condition == "OTHERWISE":
        return True, route
    if not condition.startswith("IF "):
        raise ValueError("Expected IF or OTHERWISE")
    condition = condition[3:]
    if presence := _PRESENCE.fullmatch(condition):
        return {"field": presence[1], "op": presence[2].lower()}, route
    if comparison := _COMPARISON.fullmatch(condition):
        result = {"field": comparison[1], "op": comparison[2],
                  "value": json.loads(comparison[3])}
        if comparison[4]:
            result["unit"] = comparison[4]
        return result, route
    raise ValueError("The checkpoint supports a single atomic predicate per rule")


def review(bundle):
    findings = []
    coverage = []

    def evidence(artifact, location, record=None):
        return {"artifact": artifact, "version": bundle[artifact]["version"],
                "location": (record or {}).get("location", location),
                "excerpt": json.dumps(record, sort_keys=True) if record else ""}

    def finding(code, rule_id, source, message, correction, severity="blocker", other=None):
        primary = other or source
        findings.append({"code": code, "severity": severity, "rule_id": rule_id,
                         "artifact": primary["artifact"], "version": primary["version"],
                         "location": primary["location"], "message": message,
                         "evidence": [source, other] if other else [source],
                         "correction": correction, "counterexample": None})

    try:
        if bundle["schema_version"] != "1.0":
            raise ValueError("Expected schema 1.0")
        source_rules = sorted(enumerate(bundle["requirements"]["rules"]),
                              key=lambda item: item[1]["order"])
        spec_rules = sorted(enumerate(bundle["specification"]["rules"]),
                            key=lambda item: item[1]["order"])
        graph = bundle["workflow"]
        index = {node["id"]: (position, node) for position, node in enumerate(graph["nodes"])}
        spec_index = {rule["rule_id"]: (position, rule) for position, rule in spec_rules}
        if (not source_rules or len(index) != len(graph["nodes"]) or
                len(spec_index) != len(spec_rules) or
                len({rule["id"] for _, rule in source_rules}) != len(source_rules)):
            raise ValueError("Expected nonempty source and unique rule and node references")
        expected_order = [rule["id"] for _, rule in source_rules]
        actual_order = [rule["rule_id"] for _, rule in spec_rules]
        relevant_changes = {}
        for position, record in enumerate(bundle["approvals"]["records"]):
            if (record["status"] == "approved" and record["revision"] == bundle["revision"]
                    and record["requirement_version"] == bundle["requirements"]["version"]):
                relevant_changes.setdefault(record["rule_id"], []).append((position, record))

        next_node = graph["entry"]
        for position, rule in source_rules:
            rule_id = rule["id"]
            source = evidence("requirements", f"/requirements/rules/{position}", rule)
            start_findings = len(findings)
            spec = spec_index.get(rule_id)
            if spec is None:
                finding("missing_requirement", rule_id, source,
                        "An anchored requirement has no specification rule.",
                        "Add a specification rule for this source requirement.")

            try:
                changes = relevant_changes.get(rule_id, [])
                if len(changes) > 1:
                    raise ValueError("The checkpoint cannot order multiple applicable approvals")
                if changes:
                    change_position, change = changes[0]
                    source = evidence("approvals", f"/approvals/records/{change_position}", change)
                    condition, route = parse_rule(change["text"])
                else:
                    condition, route = parse_rule(rule["text"])
            except ValueError as error:
                finding("unreviewed_requirement", rule_id, source, str(error),
                        "Review this requirement with explicit approval and predicate semantics.",
                        severity="warning")
                coverage.append({"rule_id": rule_id, "status": "unreviewed"})
                next_node = None
                continue

            if spec is not None:
                spec_position, spec_rule = spec
                spec_ref = evidence("specification", f"/specification/rules/{spec_position}", spec_rule)
                if spec_rule["when"] != condition or spec_rule["route"] != route:
                    finding("spec_mismatch", rule_id, source,
                            "The specification differs from the written predicate or route.",
                            "Align the specification predicate and route to the effective requirement.",
                            other=spec_ref)
                common_order = [item for item in expected_order if item in spec_index]
                common_actual_order = [item for item in actual_order if item in expected_order]
                if common_order.index(rule_id) != common_actual_order.index(rule_id):
                    finding("spec_precedence", rule_id, source,
                            "The specification evaluates this rule in a different order.",
                            "Restore the relative order of applicable rules.", other=spec_ref)

            if next_node is not None:
                node_pair = index.get(next_node)
                node = node_pair[1] if node_pair else {}
                graph_ref = evidence("workflow", f"/workflow/nodes/{node_pair[0]}" if node_pair
                                     else "/workflow/entry", node)
                if condition is True:
                    actual_route = node.get("route") if node.get("kind") == "route" else None
                    next_node = None
                else:
                    target_pair = index.get(node.get("true"))
                    target = target_pair[1] if target_pair else {}
                    actual_route = target.get("route") if target.get("kind") == "route" else None
                    if node.get("kind") != "decision" or node.get("when") != condition:
                        finding("workflow_predicate", rule_id, source,
                                "The ordered workflow predicate differs from the effective requirement.",
                                "Place the effective decision at this point of the workflow.",
                                other=graph_ref)
                    next_node = node.get("false")
                if actual_route != route:
                    finding("workflow_destination", rule_id, source,
                            "The workflow does not reach the expected route on this branch.",
                            "Link this branch to the route named by the source requirement.",
                            other=graph_ref)
                if condition is not True and next_node not in index:
                    finding("missing_branch", rule_id, source,
                            "The workflow decision has no existing false-branch node.",
                            "Link a valid false branch for tickets that miss the predicate.",
                            other=graph_ref)
                    next_node = None

            coverage.append({"rule_id": rule_id,
                             "status": "checked" if len(findings) == start_findings else "changed"})
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        findings.append({"code": "input_unreviewed", "severity": "warning", "rule_id": None,
                         "artifact": "bundle", "version": None, "location": "/",
                         "message": f"Cannot review baseline input ({type(error).__name__}).",
                         "evidence": [], "correction": "Supply a complete schema 1.0 review bundle.",
                         "counterexample": None})

    status = ("CHANGES_REQUIRED" if any(item["severity"] == "blocker" for item in findings)
              else "INCOMPLETE" if findings else "PASS")
    return {"status": status, "summary": "Initial checkpoint for atomic predicates and linear workflows.",
            "findings": findings, "coverage": coverage,
            "checks": [{"name": "baseline_comparison", "status": status}],
            "model": {"mode": "off", "calls": 0, "retries": 0}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    with args.bundle.open(encoding="utf-8") as stream:
        print(json.dumps(review(json.load(stream)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
