"""Verify both freezes, evaluate all checkpoints on CPU, and write JSON results."""

import argparse
import copy
import json
import platform
from pathlib import Path
from time import perf_counter_ns

from .fake_transcripts import transcript
from .freeze import (DATA_MANIFEST, IMPLEMENTATION_MANIFEST, ROOT, file_digests, load_json,
                     sha256_file, verify_checkpoints, verify_dataset, verify_files, write_json)
from .oracle import example_difference
from .scoring import STATUS_TYPES, coverage_quality, finding_quality, summarize

EXPECTED_FAKE_ATTEMPTS = {
    "timeout": ["timeout", "timeout"],
    "truncated": ["truncated_response", "malformed_json"],
    "truncated_then_valid": ["truncated_response", None],
    "coverage_gap": ["invalid_rule_coverage", "invalid_rule_coverage"],
    "conflicting_claim": ["conflicting_defect_claim", "conflicting_defect_claim"],
    "invented_reference": ["invented_or_duplicate_rule", "invented_or_ambiguous_evidence"],
}


def load_cases(root, split):
    cases = load_json(root / "evaluation/labels.json")["cases"]
    settings = load_json(root / "evaluation/settings.json")
    configured = settings["model_by_case"]
    input_ids = {item.stem for item in (root / "fixtures/inputs").glob("*.json")}
    if set(cases) != set(configured) or set(cases) != input_ids:
        raise ValueError("Input, label, and settings case IDs must match one-to-one")
    selected = []
    for case_id, label in cases.items():
        if split not in ("all", label["split"]):
            continue
        bundle = load_json(root / "fixtures/inputs" / f"{case_id}.json")
        for target in label["finding_targets"]:
            layer = target["witness_layer"]
            if layer is not None and example_difference(
                    label["routing_oracle"], layer, bundle["fields"]) is None:
                raise ValueError(f"{case_id} / {target['name']} has no oracle example difference")
        selected.append((case_id, bundle, label, configured[case_id]))
    return selected, settings


def fake_client(bundle, setup):
    from reviewer.model import FakeModelClient, ModelResponse, ModelTimeout

    responses = []
    for item in transcript(bundle, setup["fake_transcript"]):
        if item["kind"] == "text":
            responses.append(item["text"])
        elif item["kind"] == "response":
            responses.append(ModelResponse(item["text"], finish_reason=item["finish_reason"],
                                           response_id="fixture-truncated"))
        else:
            responses.append(ModelTimeout())
    return FakeModelClient(responses, model_name="evaluation-fake")


def registered_report(raw):
    if not isinstance(raw, dict) or raw.get("status") not in STATUS_TYPES:
        raise ValueError("Reviewer did not return a recognized decision")
    findings = raw.get("findings")
    if not isinstance(findings, list) or any(not isinstance(item, dict) for item in findings):
        raise ValueError("Reviewer did not return a findings list")
    return raw


def fake_trace_result(report, client, bundle, setup, max_attempts):
    """Cross-check provenance against the recorded fake transport, independent of labels."""
    calls = client.calls
    model = report.get("model", {})
    attempts = model.get("attempts", [])
    expected = EXPECTED_FAKE_ATTEMPTS[setup["fake_transcript"]]
    isolated = all(
        set(payload) == {"bundle", "deterministic_status", "rules", "evidence"}
        and payload["bundle"] == bundle
        for payload in (json.loads(call.user_payload) for call in calls)
    )
    correct = (
        len(calls) == len(expected) <= max_attempts
        and model.get("calls") == len(calls)
        and model.get("retries") == max(0, len(calls) - 1)
        and model.get("request", {}).get("max_attempts") == max_attempts
        and model.get("client", {}).get("synthetic") is True
        and [attempt.get("reason") for attempt in attempts] == expected
        and isolated
    )
    return correct, isolated if calls else None


def evaluate_case(case, implementation, *, ablation=None, max_attempts):
    case_id, bundle, label, setup = case
    candidate = copy.deepcopy(bundle)
    client = None
    if implementation == "baseline":
        from baseline import review
    else:
        from reviewer.app import review

        if setup["mode"] != "off":
            client = fake_client(candidate, setup)

    started = perf_counter_ns()
    try:
        if implementation == "baseline":
            raw = review(candidate)
        else:
            raw = review(candidate, model_client=client, model_mode=setup["mode"], ablation=ablation)
        report = registered_report(raw)
        status = report["status"]
        error = None
    except Exception as problem:
        status = "ERROR"
        error = {"type": type(problem).__name__, "message": str(problem)}
        report = {}
    duration_ms = (perf_counter_ns() - started) / 1_000_000

    fake_checks = setup["mode"] != "off"
    calls = len(client.calls) if client is not None else 0
    trace_verified, isolated = (fake_trace_result(report, client, bundle, setup, max_attempts)
                                if client is not None else (None, None))
    return {
        "case_id": case_id,
        "case_name": label["name"],
        "split": label["split"],
        "expected_status": label["expected_status"],
        "actual_status": status,
        "error": error,
        "coverage": coverage_quality(bundle, report),
        "quality": finding_quality(label, bundle, report.get("findings", [])),
        "runtime": {
            "runtime_ms": round(duration_ms, 6),
            "cohort": "fake_requested" if fake_checks else "no_model",
            "requested_model_mode": setup["mode"],
            "exercised_model_mode": setup["mode"] if client is not None else "off",
            "model_calls": calls,
            "model_retries": max(0, calls - 1),
            "fake_trace_verified": trace_verified,
            "fake_payload_isolated": isolated,
        },
        "report": report,
    }


def subset_stats(rows):
    return {
        "all": summarize(rows),
        **{
            split: summarize([row for row in rows if row["split"] == split])
            for split in ("development", "held_out")
        },
    }


def changed_decisions(before, after):
    return [
        {
            "case_id": early["case_id"],
            "case_name": early["case_name"],
            "split": early["split"],
            "expected": early["expected_status"],
            "before": early["actual_status"],
            "after": late["actual_status"],
        }
        for early, late in zip(before, after, strict=True)
        if early["actual_status"] != late["actual_status"]
    ]


def paired_outcomes(cases, runs):
    labels = {case_id: label for case_id, _, label, _ in cases}
    paired = {}
    for implementation, rows in runs.items():
        for row in rows:
            pair = labels[row["case_id"]]["pair"]
            if pair is None:
                continue
            grouped = paired.setdefault(pair["group"], {}).setdefault(implementation, {})
            grouped[pair["role"]] = {
                "case_id": row["case_id"],
                "expected_status": row["expected_status"],
                "actual_status": row["actual_status"],
            }
    return paired


def run(root, output, split):
    verify_dataset(root)
    checkpoints = verify_checkpoints(root)
    evaluator_sources = file_digests(root, [
        Path("evaluation/run.py"), Path("evaluation/scoring.py"), Path("evaluation/freeze.py"),
    ])
    cases, settings = load_cases(root, split)
    maximum = settings["model_max_attempts"]
    ablation = settings["ablation"]
    ablation_name = "ablation-" + ablation.replace("_", "-")
    runs = {}
    for method, base, disabled_mechanism in [
        ("baseline", "baseline", None),
        ("final", "final", None),
        (ablation_name, "final", ablation),
    ]:
        runs[method] = [
            evaluate_case(case, base, ablation=disabled_mechanism, max_attempts=maximum)
            for case in cases
        ]

    verify_dataset(root)
    verify_checkpoints(root)
    verify_files(root, evaluator_sources)
    provenance = {
        "dataset_manifest_sha256": sha256_file(root / DATA_MANIFEST),
        "implementation_manifest_sha256": sha256_file(root / IMPLEMENTATION_MANIFEST),
        "baseline_commit": checkpoints["baseline"]["commit"],
        "final_commit": checkpoints["final"]["commit"],
        "evaluator_source_sha256": evaluator_sources,
        "seed": settings["seed"],
        "split": split,
        "python": platform.python_version(),
        "platform": platform.system() + "-" + platform.machine(),
        "real_model_performance_measured": False,
    }
    for name, rows in runs.items():
        write_json(output / f"{name}.json", {"run": name, "provenance": provenance, "results": rows})
    write_json(output / "metrics.json", {
        "provenance": provenance,
        "metrics": {name: subset_stats(rows) for name, rows in runs.items()},
        "baseline_to_final_decision_changes": changed_decisions(runs["baseline"], runs["final"]),
        "ablation": {
            "mechanism": ablation,
            "decision_changes": changed_decisions(runs["final"], runs[ablation_name]),
        },
        "paired_cases": paired_outcomes(cases, runs),
    })
    for name, rows in runs.items():
        exact = summarize(rows)["exact_decisions"]
        print(f"{name}: {exact['count']}/{exact['total']} exact decisions")
    print(f"Reports: {output.resolve()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/reproduced")
    parser.add_argument("--split", choices=("all", "development", "held_out"), default="all")
    args = parser.parse_args()
    run(ROOT, args.output, args.split)


if __name__ == "__main__":
    main()
