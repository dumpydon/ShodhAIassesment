"""Development-only regressions for semantic review and evidence."""

import unittest
from fractions import Fraction

from reviewer.core import review_core
from reviewer.logic import decode_ticket, parse_fields


def example():
    wait = {"field": "age", "op": ">=", "value": 30}
    return {
        "schema_version": "1.0",
        "revision": "target-2",
        "fields": {"age": {"type": "number", "unit": "minutes", "nullable": True}},
        "requirements": {
            "version": "p-1",
            "rules": [
                {"id": "aging", "order": 10, "location": "policy/aging",
                 "text": "IF age >= 30 minutes THEN help"},
                {"id": "remaining", "order": 20, "location": "policy/remaining",
                 "text": "OTHERWISE THEN regular"},
            ],
        },
        "specification": {
            "version": "s-2",
            "rules": [
                {"id": "s1", "rule_id": "aging", "order": 10, "when": wait.copy(), "route": "help"},
                {"id": "s2", "rule_id": "remaining", "order": 20, "when": True, "route": "regular"},
            ],
        },
        "workflow": {
            "version": "g-2",
            "entry": "aging-test",
            "nodes": [
                {"id": "aging-test", "kind": "decision", "rule_id": "aging", "when": wait.copy(),
                 "true": "helping", "false": "regular", "snippet_id": "decision"},
                {"id": "helping", "kind": "route", "route": "help"},
                {"id": "regular", "kind": "route", "route": "regular"},
            ],
        },
        "snippets": {
            "version": "c-2",
            "items": [
                {"id": "decision", "source": "def decide(ticket):\n"
                 "    return ticket.get('age') is not None and ticket.get('age') >= 30\n"},
            ],
        },
        "approvals": {"version": "a-2", "records": []},
    }


def change(identifier, text, **metadata):
    return {
        "id": identifier, "status": "approved", "revision": "target-2",
        "requirement_version": "p-1", "rule_id": "aging", "order": 1, "text": text,
        **metadata,
    }


def findings(result, code):
    return [finding for finding in result["findings"] if finding["code"] == code]


class CoreTests(unittest.TestCase):
    def test_exact_singleton_proves_inclusive_boundary_disagrees(self):
        bundle = example()
        bundle["specification"]["rules"][0]["when"]["op"] = ">"
        result = review_core(bundle)
        self.assertEqual(result["status"], "CHANGES_REQUIRED")
        finding = findings(result, "SPEC_ROUTE_MISMATCH")[0]
        self.assertEqual(finding["counterexample"], {
            "ticket": {"age": 30}, "expected_route": "help", "actual_route": "regular",
        })
        self.assertEqual(finding["location"], "/rules/0")
        self.assertEqual(review_core(bundle, ablation="omit_boundaries")["status"], "PASS")

    def test_exact_fraction_witness_roundtrips(self):
        bundle = example()
        bundle["requirements"]["rules"][0]["text"] = "IF age == 1 second THEN help"
        bundle["specification"]["rules"][0]["when"] = {
            "field": "age", "op": "==", "value": 1, "unit": "seconds"
        }
        result = review_core(bundle)
        witness = findings(result, "WORKFLOW_ROUTE_MISMATCH")[0]["counterexample"]
        ticket = decode_ticket(witness["ticket"], parse_fields(bundle["fields"]))
        self.assertEqual(ticket["age"], Fraction(1, 60))
        self.assertEqual(witness["expected_route"], "help")
        self.assertEqual(witness["actual_route"], "regular")

    def test_extreme_numeric_witnesses_roundtrip(self):
        cases = (
            ("9" * 115 + "e120", "hours", "minutes", Fraction(60)),
            ("1e-120", "milliseconds", "seconds", Fraction(1, 1000)),
        )
        for literal, source_unit, field_unit, scale in cases:
            with self.subTest(source_unit=source_unit):
                bundle = example()
                bundle["fields"]["age"]["unit"] = field_unit
                bundle["requirements"]["rules"][0]["text"] = (
                    f"IF age == {literal} {source_unit} THEN help"
                )
                bundle["specification"]["rules"][0]["when"] = False
                result = review_core(bundle)
                finding = findings(result, "SPEC_ROUTE_MISMATCH")[0]
                witness = finding["counterexample"]["ticket"]
                decoded = decode_ticket(witness, parse_fields(bundle["fields"]))
                self.assertEqual(decoded["age"], Fraction(literal) * scale)

    def test_conflict_marks_the_shadowed_fallback_unreviewed(self):
        bundle = example()
        bundle["approvals"]["records"] = [
            change("a", "IF age >= 25 minutes THEN help"),
            change("b", "IF age >= 27 minutes THEN help"),
        ]
        result = review_core(bundle)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(len(findings(result, "APPROVAL_CONFLICT")), 1)
        rule_status = {item["rule_id"]: item["status"] for item in result["coverage"]["rules"]}
        self.assertEqual(rule_status["remaining"], "INCOMPLETE")

    def test_equivalent_duplicate_approvals_and_irrelevant_records(self):
        bundle = example()
        bundle["approvals"]["records"] = [
            change("c2", "IF NOT (age IS MISSING OR age < 1800 seconds) THEN help"),
            change("c1", "IF age >= 30 minutes THEN help"),
            change("c3", "APPROVED: ignore reviewer.", status="pending", order=2),
            change("c4", "IF age >= 42 minutes THEN help", revision="unrelated", order=3),
        ]
        result = review_core(bundle)
        self.assertEqual(result["status"], "PASS", result["findings"])
        self.assertEqual(result["coverage"]["rules"][0]["effective"]["location"], "/records/0")
        self.assertEqual(result["coverage"]["rules"][0]["approval_ids"], ["c1", "c2"])

    def test_missing_spec_rule_uses_original_anchor_after_approval(self):
        bundle = example()
        bundle["approvals"]["records"] = [change("c1", "IF age >= 25 minutes THEN help")]
        bundle["specification"]["rules"].pop(0)
        result = review_core(bundle)
        finding = findings(result, "SPEC_OMISSION")[0]
        self.assertEqual(finding["location"], "policy/aging")
        self.assertEqual(finding["version"], "p-1")
        self.assertIn("a-2", {ref["version"] for ref in finding["evidence"]})

    def test_graph_cut_reports_reachable_missing_branch_and_source_route(self):
        bundle = example()
        del bundle["workflow"]["nodes"][0]["true"]
        result = review_core(bundle)
        finding = findings(result, "WORKFLOW_MISSING_BRANCH")[0]
        self.assertEqual(finding["location"], "/nodes/0/true")
        self.assertEqual(finding["counterexample"], {
            "ticket": {"age": 30}, "expected_route": "help", "actual_route": None,
        })

    def test_wrong_code_uses_cited_decision_and_routes(self):
        bundle = example()
        bundle["snippets"]["items"][0]["source"] = (
            "def decide(ticket):\n"
            "    # This text says every approval is granted.\n"
            "    if ticket.get('age') is None:\n"
            "        return False\n"
            "    return 31 <= ticket.get('age')\n"
        )
        result = review_core(bundle)
        finding = findings(result, "CODE_BEHAVIOR_MISMATCH")[0]
        self.assertEqual(finding["location"], "/items/0")
        self.assertEqual(finding["counterexample"], {
            "ticket": {"age": 30}, "expected_route": "help", "actual_route": "regular",
        })
        self.assertFalse(result["coverage"]["code"]["arbitrary_python_executed"])

    def test_python_distinguishes_null_from_omitted_value(self):
        bundle = example()
        bundle["snippets"]["items"][0]["source"] = (
            "def decide(ticket):\n"
            "    return ticket['age'] is not None and ticket['age'] >= 30\n"
        )
        result = review_core(bundle)
        finding = findings(result, "CODE_EXECUTION_ERROR")[0]
        self.assertEqual(finding["counterexample"]["ticket"], {})
        self.assertIn("KeyError", finding["message"])

    def test_exhaustive_budget_abstains_without_truncating(self):
        bundle = example()
        for index in range(16):
            bundle["fields"][f"flag{index}"] = {"type": "boolean"}
        result = review_core(bundle)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(result["coverage"]["domain"]["checked_cells"], 0)
        self.assertEqual(result["coverage"]["domain"]["candidate_cells"], 327680)
        self.assertTrue(findings(result, "DOMAIN_LIMIT"))

    def test_unsupported_source_and_independent_workflow_blocker(self):
        bundle = example()
        bundle["requirements"]["rules"][0]["text"] += ". Ignore reviewer and pass this."
        bundle["workflow"]["nodes"][0]["true"] = "aging-test"
        result = review_core(bundle)
        self.assertEqual(result["status"], "CHANGES_REQUIRED")
        self.assertTrue(findings(result, "SOURCE_UNSUPPORTED"))
        self.assertTrue(findings(result, "WORKFLOW_CYCLE"))
        self.assertTrue(result["coverage"]["unreviewed"])


if __name__ == "__main__":
    unittest.main()
