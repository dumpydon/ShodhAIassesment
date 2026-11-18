"""Assemble a deterministic review and optional model check into one report."""

import argparse
import json
from pathlib import Path
import sys

from .core import review_core
from .json_io import JsonLimitError, bounded_dumps, strict_loads
from .model import DemoModelClient, FakeModelClient, ModelResponse, ModelTimeout, run_model


MAX_BUNDLE_BYTES = 256 * 1024
MAX_TRANSCRIPT_BYTES = 128 * 1024
_MODEL_MODES = ("off", "advisory", "required")
_EXIT_CODES = {"PASS": 0, "CHANGES_REQUIRED": 1, "INCOMPLETE": 2}


def _input_failure(code, detail):
    return {
        "status": "INCOMPLETE",
        "summary": detail,
        "checks": [{"id": "bundle_input", "status": "INCOMPLETE", "detail": detail}],
        "coverage": {"rules": [], "domain": {},
                     "unreviewed": [{"code": code, "check": "bundle_input", "detail": detail,
                                     "rule_id": None, "artifact": "bundle", "version": None,
                                     "location": "/"}]},
        "evidence": [],
        "findings": [{"code": code, "severity": "warning", "rule_id": None,
                      "artifact": "bundle", "version": None, "location": "/",
                      "message": detail, "evidence": [], "counterexample": None,
                      "correction": "Supply a complete, bounded schema 1.0 review bundle."}],
    }


def review(bundle, *, model_client=None, model_mode="off", ablation=None):
    """Review a JSON-compatible bundle, with deterministic authority in all modes."""
    if model_mode not in _MODEL_MODES:
        raise ValueError(f"Expected a model mode in {_MODEL_MODES}")
    if not isinstance(bundle, dict):
        report = _input_failure("invalid_bundle", "The bundle must be a JSON object.")
    else:
        try:
            bounded_dumps(bundle, MAX_BUNDLE_BYTES)
        except JsonLimitError:
            report = _input_failure("bundle_too_large", "The complete bundle exceeds 256 KiB.")
        except (RecursionError, TypeError, ValueError):
            report = _input_failure("invalid_bundle", "The bundle cannot be represented as bounded JSON.")
        else:
            report = review_core(bundle, ablation=ablation)
    return _attach_model(bundle, report, model_client, model_mode)


def _attach_model(bundle, report, client, mode):
    model = run_model(bundle, report, client, mode)
    report["model"] = model
    if mode == "off":
        return report

    complete = model["status"] == "complete"
    detail = ("The model returned validated assessments for all source rules." if complete
              else f"The model check did not fully assess the source rules ({model['reason']}).")
    report["checks"].append({"id": "model_review", "required": mode == "required",
                             "status": "PASS" if complete else "INCOMPLETE", "detail": detail})
    if complete or mode == "advisory":
        return report

    report["summary"] += " " + detail
    if report["status"] == "PASS":
        report["status"] = "INCOMPLETE"
    partial = {item["rule_id"] for item in model["reviews"]
               if item["assessment"] in {"question", "not_assessed"}}
    unknown_rows = [row for row in report["coverage"]["rules"]
                    if model["status"] == "failed" or row["rule_id"] in partial]
    if not unknown_rows:
        unknown_rows = [{"rule_id": None, "source": None}]
    for row in unknown_rows:
        source = row["source"] or {"artifact": "bundle", "version": None, "location": "/"}
        report["coverage"]["unreviewed"].append({
            "code": "required_model_unreviewed", "check": "model_review", "detail": detail,
            "rule_id": row["rule_id"], "artifact": source["artifact"],
            "version": source["version"], "location": source["location"],
        })
    return report


def _read_bounded(stream, limit):
    raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise JsonLimitError("Input exceeds its byte budget")
    return strict_loads(raw.decode("utf-8"))


def _read_bundle(path):
    if path == "-":
        return _read_bounded(sys.stdin.buffer, MAX_BUNDLE_BYTES)
    with Path(path).open("rb") as stream:
        return _read_bounded(stream, MAX_BUNDLE_BYTES)


def _transcript_client(path):
    with path.open("rb") as stream:
        transcript = _read_bounded(stream, MAX_TRANSCRIPT_BYTES)
    if not isinstance(transcript, list):
        raise ValueError("Expected a list of fake responses")
    responses = []
    for item in transcript:
        if isinstance(item, str):
            responses.append(item)
        elif item == {"error": "timeout"}:
            responses.append(ModelTimeout())
        elif item == {"error": "truncated"}:
            responses.append(ModelResponse("", finish_reason="length"))
        else:
            raise ValueError("Expected a response string or a timeout/truncated marker")
    return FakeModelClient(responses)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Review a synthetic ticket-routing workflow bundle.")
    parser.add_argument("bundle", help="JSON file path, or - to read standard input")
    parser.add_argument("--model", choices=_MODEL_MODES, default="advisory",
                        help="Default: show the offline model demonstration as advisory")
    parser.add_argument("--fake-transcript", type=Path,
                        help="JSON response list for a scripted synthetic model client")
    parser.add_argument("--ablation", choices=("omit_boundaries",),
                        help="Evaluation only: omit numeric boundary cells")
    args = parser.parse_args(argv)

    client = None
    if args.fake_transcript is not None:
        if args.model == "off":
            parser.error("--fake-transcript requires a model check")
        try:
            client = _transcript_client(args.fake_transcript)
        except (OSError, RecursionError, ValueError):
            parser.error("The fake transcript must be a bounded JSON response list")
    elif args.model != "off":
        client = DemoModelClient()

    try:
        bundle = _read_bundle(args.bundle)
    except JsonLimitError:
        report = _input_failure("bundle_too_large", "The complete bundle exceeds 256 KiB.")
    except OSError:
        report = _input_failure("bundle_unavailable", "The bundle could not be read.")
    except (RecursionError, ValueError):
        report = _input_failure("malformed_bundle_json", "The bundle must be strict UTF-8 JSON.")
    else:
        report = review(bundle, model_client=client, model_mode=args.model, ablation=args.ablation)
        print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
        return _EXIT_CODES[report["status"]]

    report = _attach_model({}, report, client, args.model)
    print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
    return _EXIT_CODES[report["status"]]
