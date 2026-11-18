# Ticket workflow reviewer

A Python CLI reviews a written routing policy against its structured specification, decision graph, and linked Python snippets. The report identifies effective requirements, cites versioned artifacts, and gives a concrete ticket when two supported behaviors differ. The sample policies and evaluation bundles are synthetic.

## Run offline

Use Python 3.14.3 as pinned in [.python-version](.python-version). From the repository root, check `python3 --version` and run the offline command below. The standard library supplies all application and test dependencies.

```sh
python3 -m unittest discover -s tests && python3 -m evaluation.run
```

The evaluator checks frozen inputs and writes reproducible reports. The [evaluation protocol](evaluation/README.md) describes the checkpoints, holdout, oracle, and metrics.

The blind run used reviewer commit `1bd3e22` and scored 24/24, including 7/7 on holdout. Its reports are preserved in commit `4bce2b2`. A separate development case later exposed a replay error when unit conversion produced a numeric witness outside the ordinary literal bounds. Commit `f48b862` emits the existing exact numerator and denominator representation for such witnesses. The regression rerun over the same 24 cases reproduced the decisions, finding scores, and ablation results.

Review a bundle with `python3 -m reviewer fixtures/inputs/case-44bac949132f.json`. The CLI reads standard input when the path is `-`. It emits one JSON object to standard output and uses these exit codes.

| Exit | Decision | Meaning |
| --- | --- | --- |
| `0` | `PASS` | Every required check covered its declared scope, with zero evidenced blockers. |
| `1` | `CHANGES_REQUIRED` | At least one blocker has evidence. See `coverage.unreviewed` for remaining gaps. |
| `2` | `INCOMPLETE` | A gap or failure prevented a complete review, with zero established blockers. |

```sh
python3 -m reviewer BUNDLE.json --model off
python3 -m reviewer BUNDLE.json --model required --fake-transcript RESPONSES.json
python3 baseline.py BUNDLE.json
```

## Inputs and reports

Inputs use schema 1.0. Read the [bundle contract](docs/contracts.md) and [JSON Schema](schemas/bundle.schema.json) for field declarations, source grammar, graph references, approval ordering, evidence anchors, and the supported Python subset. The initial checkpoint is also preserved as `baseline-v1` (commit `208ffb6`).

The report exposes the decision, summary, checks, findings, and coverage. `evidence` is the artifact locator catalog. A finding records a correction and eligible mismatches include a `counterexample` with the ticket, expected route, and actual route. `coverage.rules` records each source rule, its effective reference, and its deterministic review status. `coverage.unreviewed` includes unresolved issues even when the decision is `CHANGES_REQUIRED`.

The deterministic reviewer enumerates a finite partition induced by supported predicates and linked Python expressions. It checks graph reachability by running every representative through the restricted interpreter and comparing route outcomes. The runtime bounds apply to bundle size, expression complexity, and partition cells. A limit or unsupported construct appears as an explicit review gap. Python syntax, static inspection, and bounded interpretation have separate checks.

## Model interface

The CLI defaults to `--model advisory` and uses a synthetic client that demonstrates a cited clarification question. `--model off` runs the deterministic reviewer. `--model required` requires a fully validated model report as well as the core checks. A failed or partial required model check creates a coverage gap. An evidenced blocker still determines `CHANGES_REQUIRED`.

The Python API is `reviewer.app.review(bundle, model_client=None, model_mode="off")`. Implement `reviewer.model.ModelClient` with provider identity, model identity, a synthetic flag, and a `complete(request)` method that returns `ModelResponse`. Transport implementations must enforce the request deadline and output-token budget. The request contains the [versioned prompt](reviewer/model.py), bounded data and evidence, and generation settings. The report keeps request and response hashes, per-attempt outcome, response identity and usage when reported, and the number of calls and retries.

A response must contain one assessment for every source rule. Each assessment uses catalog locators and, where relevant, the source anchor and the location supporting a deterministic blocker. Unknown rules, duplicate keys, invented or disputed locators, and claims that conflict with the core are rejected. A model can raise a grounded question about policy intent or missing evidence. Model questions appear under `model.reviews` and carry advisory authority. Trusted approval metadata determines applicable changes.

Model checks allow at most two calls per run, with a four-second deadline requested per call, a 96 KiB prompt budget, a 24 KiB response limit, and a 3,000-token output budget. Fake transcripts are separate JSON arrays of response strings or `{"error": "timeout"}` and `{"error": "truncated"}` markers. The included offline verification uses fake responses and measures validation and failure handling. It provides no estimate of real-model semantic performance.

A separate development smoke check used the blind reviewer checkpoint `1bd3e22`. It captured a real response through an ephemeral Codex CLI session and replayed it through the validation interface. It produced cited questions and partial model coverage. The exact input, response, runtime, and transport differences are recorded in the [live model smoke report](reports/model-live-smoke.json).

## Assessment material

- [Original assessment](docs/ShodhAI_MLAssessment.pdf)
- [Implementation and verification plan](docs/implementation-plan.md)
- [Engineering note and AI disclosure](docs/engineering-note.pdf)
- [Debugging records and regression commands](docs/debugging.md)
- [Evaluation protocol](evaluation/README.md) and [aggregate metrics](reports/metrics.json)
