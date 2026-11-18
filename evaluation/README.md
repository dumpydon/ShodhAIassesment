# Controlled evaluation

The suite has 24 synthetic reviews. The development split has 17 cases and the held-out split has seven. Manual labels expect six passes, ten changes required, and eight incomplete reviews. The dataset manifest was committed before any scoring. Held-out inputs, the generator, and case-level labels stayed outside the implementation workspace until the final reviewer checkpoint was recorded.

## Reproduce

```sh
python3 -m unittest discover -s tests && python3 -m evaluation.run
```

The evaluator checks the dataset and both implementation snapshots against their SHA-256 manifests before and after scoring. It writes raw reports and measured metrics to `reports/reproduced/` by default. Use `--output <directory>` for another destination or `--split development` for the development results alone. Runtime measurements vary across runs. The baseline commit and the final commit are recorded in `frozen_implementations.json`.

The fixed seed and per-case fake-client settings are in `settings.json`. To regenerate data for inspection, use `python3 -m evaluation.generate --output <empty-directory>`. The expected fixture, label, and setting hashes are in `frozen_dataset.json`.

## Scoring

- Decisions use three manual labels. Counts include false passes among defective cases, false blocks among valid cases, incompletes by expected type, and passes on cases that require an incomplete result. A run-time exception appears as `ERROR`.
- Labels list expected defect and review-gap findings with their allowed rule references, precise locations, and citation groups. Exact locations include artifact and version. A citation must quote the located input or render the same item as JSON. Each citation group needs one independently grounded reference. The unsupported count includes unmatched reported findings, missing support, and invented or misquoted references.
- The independent routing oracle replays both sides of behavioral counterexamples in the fixture's native units. It applies hand-labeled routing parameters and mutations directly and imports no reviewer code. It checks the ticket against declared fields, verifies both claimed routes, and confirms a difference. Findings about an absent branch or unsupported evidence have their own targets.
- The baseline, final, and ablated reviewer see the same inputs and requested settings. The ablation removes singleton numeric boundary cells. The frozen initial checkpoint has no model transport, so its fake-requested cases record zero calls and explicitly show that the model check was unexercised.
- The resource totals measure elapsed reviewer calls and use the recorded transport for fake calls and retries. Fake-client cases verify retry reasons and that the model payload contains the bundle and deterministic context. No external model was called and semantic model performance was not measured.

## Files

| Path | Content |
| --- | --- |
| `fixtures/inputs/` | Synthetic reviewer inputs |
| `evaluation/labels.json` | Manual case labels, accepted anchors, and independent routing parameters |
| `evaluation/frozen_dataset.json` | SHA-256 freeze for inputs, settings, labels, generator, oracle, and fake transcripts |
| `evaluation/frozen_implementations.json` | Git commits and SHA-256 source hashes for the baseline and final reviewer |
| `reports/baseline.json` | Initial working checkpoint responses and case-level scores |
| `reports/final.json` | Final responses and case-level scores |
| `reports/ablation-omit-boundaries.json` | Ablated responses and case-level scores |
| `reports/metrics.json` | Counts, denominators, split metrics, resource use, and changed outcomes |
