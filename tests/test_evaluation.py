"""Regression checks for independent evidence and counterexample adjudication."""

import unittest

from evaluation.oracle import check_counterexample, route
from evaluation.scoring import finding_quality, grounded


class EvaluationTest(unittest.TestCase):
    def setUp(self):
        self.rule = {
            "id": "wait",
            "order": 10,
            "location": "policy#wait",
            "text": "IF waiting >= 30 minutes THEN escalation",
        }
        self.fields = {
            "category": {"type": "string", "nullable": True},
            "urgent": {"type": "boolean"},
            "waiting": {"type": "number", "nullable": True, "unit": "minutes"},
        }
        self.node = {
            "id": "different",
            "kind": "decision",
            "rule_id": "wait",
            "when": {"field": "waiting", "op": ">", "value": 30},
            "true": "escalation",
            "false": "normal",
        }
        self.bundle = {
            "fields": self.fields,
            "requirements": {"version": "source-v1", "rules": [self.rule]},
            "workflow": {"version": "graph-v1", "nodes": [self.node]},
        }
        self.source = {"artifact": "requirements", "version": "source-v1", "location": "policy#wait"}
        self.workflow = {"artifact": "workflow", "version": "graph-v1", "location": "/nodes/0/false"}
        self.profile = {"threshold": 30, "operator": ">=", "exception_categories": [],
                        "layers": {"workflow": {"operator": ">"}}}
        self.ticket = {"category": "sales", "urgent": False, "waiting": 30}

    def test_grounding_rejects_invented_locator_and_misattributed_quote(self):
        self.assertTrue(grounded(self.bundle, self.source | {"excerpt": self.rule["text"]}))
        self.assertTrue(grounded(self.bundle, self.workflow | {"excerpt": "normal"}))
        self.assertTrue(grounded(self.bundle, self.workflow | {
            "location": "/workflow/nodes/0/false", "excerpt": '"normal"',
        }))
        self.assertFalse(grounded(self.bundle, self.source | {"excerpt": "Escalation at 20 minutes"}))
        self.assertFalse(grounded(self.bundle, self.source | {
            "location": "policy#invented", "excerpt": self.rule["text"],
        }))
        self.assertFalse(grounded(self.bundle, self.source | {
            "version": "source-v2", "excerpt": self.rule["text"],
        }))

    def test_witness_checks_both_routes_and_precedence(self):
        example = {"ticket": self.ticket, "expected_route": "escalation", "actual_route": "normal"}
        self.assertEqual(check_counterexample(example, self.fields, self.profile, "workflow"),
                         "verified")
        self.assertEqual(check_counterexample(example | {"actual_route": "priority"}, self.fields,
                                              self.profile, "workflow"), "wrong_routes")
        shadowed = example | {"ticket": self.ticket | {"urgent": True}}
        self.assertEqual(check_counterexample(shadowed, self.fields, self.profile, "workflow"),
                         "no_route_difference")
        missing = example | {"ticket": {"urgent": False, "waiting": 30}}
        self.assertEqual(check_counterexample(missing, self.fields, self.profile, "workflow"),
                         "no_route_difference")
        self.assertEqual(route(missing["ticket"], self.profile), "manual")

    def test_right_defect_code_does_not_reward_invented_evidence(self):
        case = {"routing_oracle": self.profile, "finding_targets": [{
            "name": "loses_inclusive_boundary",
            "category": "defect",
            "required": True,
            "codes": ["WORKFLOW_ROUTE_MISMATCH"],
            "rule_ids": ["wait"],
            "locations": [self.workflow],
            "citation_groups": [[self.source], [self.workflow]],
            "witness_layer": "workflow",
        }]}
        finding = self.workflow | {
            "code": "WORKFLOW_ROUTE_MISMATCH",
            "severity": "blocker",
            "rule_id": "wait",
            "correction": "Include exactly 30 minutes.",
            "evidence": [self.source | {"excerpt": "invented"}, self.workflow | {"excerpt": "normal"}],
            "counterexample": {"ticket": self.ticket, "expected_route": "escalation", "actual_route": "normal"},
        }
        quality = finding_quality(case, self.bundle, [finding])
        target = quality["targets"][0]
        self.assertTrue(target["identified"])
        self.assertTrue(target["correct_location"])
        self.assertTrue(target["counterexample_verified"])
        self.assertFalse(target["backed_finding"])
        self.assertFalse(quality["findings"][0]["supported"])

        finding["evidence"][0]["excerpt"] = self.rule["text"]
        quality = finding_quality(case, self.bundle, [finding])
        self.assertTrue(quality["targets"][0]["fully_supported_with_witness"])
