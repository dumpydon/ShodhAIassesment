"""Build scripted model responses from inputs and scenario settings only."""

import json

TRUNCATED = {"kind": "response", "text": '{"schema_version":"1.0","reviews":[',
             "finish_reason": "length"}
MALFORMED = {"kind": "text", "text": '{"schema_version": "1.0", "reviews": ['}
TIMEOUT = {"kind": "timeout"}


def source_review(bundle, rule):
    return {
        "rule_id": rule["id"],
        "assessment": "consistent",
        "evidence": [{"artifact": "requirements", "version": bundle["requirements"]["version"],
                      "location": rule["location"]}],
        "finding_code": None,
        "question": None,
    }


def model_json(reviews):
    return {"kind": "text", "text": json.dumps({"schema_version": "1.0", "reviews": reviews})}


def transcript(bundle, scenario):
    """The fake has no access to labels, oracle, or reviewer output."""
    if scenario == "timeout":
        return [TIMEOUT, TIMEOUT]
    if scenario == "truncated":
        return [TRUNCATED, MALFORMED]

    reviews = [source_review(bundle, rule) for rule in bundle["requirements"]["rules"]]
    if scenario == "truncated_then_valid":
        return [TRUNCATED, model_json(reviews)]
    if scenario == "coverage_gap":
        return [model_json(reviews[1:])] * 2
    if scenario == "invented_reference":
        invented_anchor = model_json([
            reviews[0] | {"evidence": [reviews[0]["evidence"][0] | {"location": "policy#fiction"}]},
            *reviews[1:],
        ])
        invented_rule = model_json([reviews[0] | {"rule_id": "fictional-policy-rule"}, *reviews[1:]])
        return [invented_rule, invented_anchor]
    if scenario == "conflicting_claim":
        conflicting = reviews[0] | {"assessment": "defect", "finding_code": "SPEC_ROUTE_MISMATCH"}
        return [model_json([conflicting, *reviews[1:]])] * 2
    raise ValueError(f"Unknown fake model scenario {scenario!r}")
