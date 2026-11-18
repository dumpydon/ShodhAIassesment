import json
import unittest

from reviewer.model import (MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, FakeModelClient, ModelIssue,
                            ModelResponse, ModelTimeout, build_request, run_model, validate_response)


def evidence(rule_id, location, artifact="requirements", version="p1"):
    return {"rule_id": rule_id, "artifact": artifact, "version": version, "location": location,
            "excerpt": f"Decision linked to {rule_id}"}


def locator(ref):
    return {key: ref[key] for key in ("artifact", "version", "location")}


def entry(rule_id, assessment="not_assessed", *, refs=(), finding_code=None, question=None):
    return {"rule_id": rule_id, "assessment": assessment,
            "evidence": [locator(ref) for ref in refs], "finding_code": finding_code,
            "question": question}


def answer(*entries):
    return json.dumps({"schema_version": "1.0", "reviews": entries})


def report_with(*sources):
    return {
        "status": "PASS",
        "coverage": {"rules": [{"rule_id": source["rule_id"], "status": "PASS", "source": source}
                               for source in sources]},
        "evidence": list(sources),
        "findings": [],
    }


class ModelValidationTests(unittest.TestCase):
    def setUp(self):
        self.source = evidence("unseen-rule", "policy#n")
        self.rule_id = self.source["rule_id"]
        self.report = report_with(self.source)
        self.request = build_request({}, self.report)

    def test_conflicting_source_locator_cannot_ground_a_second_rule(self):
        second = evidence("second-rule", self.source["location"])
        report = report_with(self.source, second)
        report["evidence"] = [self.source]
        report["findings"] = [{"code": "EVIDENCE_LOCATION_CONFLICT", "severity": "warning",
                               "rule_id": second["rule_id"], "evidence": [self.source, second]}]
        report["status"] = "INCOMPLETE"
        request = build_request({}, report)
        raw = answer(entry(self.rule_id), entry(second["rule_id"], "question", refs=[second],
                                                question="What ticket demonstrates this rule?"))
        with self.assertRaisesRegex(ModelIssue, "invented_or_ambiguous_evidence"):
            validate_response(ModelResponse(raw), request)

    def test_consistent_result_restores_canonical_evidence(self):
        raw = answer(entry(self.rule_id, "consistent", refs=[self.source]))
        [review] = validate_response(ModelResponse(raw), self.request)
        self.assertEqual(review["evidence"], [self.source])

    def test_rejects_missing_and_invented_rule_coverage(self):
        with self.assertRaisesRegex(ModelIssue, "invalid_rule_coverage"):
            validate_response(ModelResponse(answer()), self.request)
        with self.assertRaisesRegex(ModelIssue, "invented_or_duplicate_rule"):
            validate_response(ModelResponse(answer(entry("made-up"))), self.request)

    def test_rejects_invented_version(self):
        wrong_version = {**self.source, "version": "made-up"}
        raw = answer(entry(self.rule_id, "consistent", refs=[wrong_version]))
        with self.assertRaisesRegex(ModelIssue, "invented_or_ambiguous_evidence"):
            validate_response(ModelResponse(raw), self.request)

    def test_rejects_missing_rule_anchor_even_if_other_citation_exists(self):
        unrelated = evidence("unrelated", "policy#other")
        self.report["evidence"].append(unrelated)
        request = build_request({}, self.report)
        with self.assertRaisesRegex(ModelIssue, "missing_rule_anchor"):
            validate_response(ModelResponse(answer(entry(self.rule_id, "consistent", refs=[unrelated]))),
                              request)

    def test_rejects_conflicting_claim_and_confirms_grounded_blocker(self):
        target = evidence(self.rule_id, "/workflow/nodes/0", "workflow", "w1")
        self.report["evidence"].append(target)
        self.report["coverage"]["rules"][0]["status"] = "CHANGES_REQUIRED"
        self.report["findings"].append({"code": "WRONG_ROUTE", "severity": "blocker",
                                        "rule_id": self.rule_id, "message": "The route differs.",
                                        "evidence": [self.source, target]})
        request = build_request({}, self.report)
        with self.assertRaisesRegex(ModelIssue, "conflicting_consistency_claim"):
            validate_response(ModelResponse(answer(entry(self.rule_id, "consistent", refs=[self.source]))),
                              request)
        with self.assertRaisesRegex(ModelIssue, "conflicting_defect_claim"):
            validate_response(ModelResponse(answer(entry(self.rule_id, "defect", refs=[self.source],
                                                         finding_code="WRONG_ROUTE"))), request)
        raw = answer(entry(self.rule_id, "defect", refs=[self.source, target], finding_code="WRONG_ROUTE"))
        self.assertEqual(validate_response(ModelResponse(raw), request)[0]["assessment"], "defect")

    def test_rejects_duplicate_json_keys_and_non_object_payload(self):
        with self.assertRaisesRegex(ModelIssue, "malformed_json"):
            validate_response(ModelResponse('{"schema_version":"0", "schema_version":"1.0", "reviews":[]}'),
                              self.request)
        with self.assertRaisesRegex(ModelIssue, "invalid_response_schema"):
            validate_response(ModelResponse('[]'), self.request)

    def test_truncated_output_retries_once_with_same_request(self):
        raw = answer(entry(self.rule_id, "consistent", refs=[self.source]))
        client = FakeModelClient([ModelResponse(raw, finish_reason="length"), raw])
        result = run_model({}, self.report, client, "required")
        self.assertEqual(result["status"], "complete")
        self.assertEqual((result["calls"], result["retries"]), (2, 1))
        self.assertEqual([item["reason"] for item in result["attempts"]], ["truncated_response", None])
        self.assertEqual(client.calls, [self.request, self.request])
        self.assertTrue(result["client"]["synthetic"])

    def test_timeout_is_bounded_and_reports_failed_coverage(self):
        client = FakeModelClient([ModelTimeout(), ModelTimeout(), "unused"])
        result = run_model({}, self.report, client, "required")
        self.assertEqual((result["status"], result["reason"]), ("failed", "timeout"))
        self.assertEqual((result["calls"], result["retries"]), (2, 1))
        self.assertEqual(result["reviews"], [])

    def test_oversized_model_input_is_reported_without_calls(self):
        bundle = {"requirements": {"text": "x" * MAX_REQUEST_BYTES}}
        client = FakeModelClient([])
        result = run_model(bundle, self.report, client, "required")
        self.assertEqual((result["status"], result["reason"]), ("failed", "request_too_large"))
        self.assertEqual(client.calls, [])

    def test_oversized_response_and_excess_token_usage_are_rejected(self):
        with self.assertRaisesRegex(ModelIssue, "response_too_large"):
            validate_response(ModelResponse("é" * MAX_RESPONSE_BYTES), self.request)
        with self.assertRaisesRegex(ModelIssue, "invalid_output_usage"):
            validate_response(ModelResponse("{}", output_tokens=self.request.max_output_tokens + 1), self.request)

    def test_question_is_visible_as_partial_coverage(self):
        raw = answer(entry(self.rule_id, "question", refs=[self.source], question="What does this cover?"))
        result = run_model({}, self.report, FakeModelClient([raw]), "advisory")
        self.assertEqual((result["status"], result["reason"]), ("partial", "model_coverage_partial"))
        self.assertEqual(result["reviews"][0]["question"], "What does this cover?")
