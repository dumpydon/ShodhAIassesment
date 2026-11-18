# Implementation and verification plan

This plan describes the work and acceptance checks for the [Shodh AI workflow reviewer assessment](ShodhAI_MLAssessment.pdf). The [engineering note](engineering-note.pdf) records measured results and limitations.

## 1. Freeze a small working reviewer

1. Define a versioned bundle with anchored policy text, declared ticket fields, ordered specification rules, workflow decisions and routes, linked Python snippets, and structured approval metadata. Use IDs to locate evidence across artifacts and preserve the source anchor for missing implementations.
2. Parse simple atomic policies, compare direct specification records, follow a linear decision chain, and preserve unsupported inputs in coverage. Run independent smoke inputs that exercise a pass, an evidenced branch difference, and an unsupported expression.
3. Tag the checkpoint before implementing exact semantic partitioning, the Python interpreter, or the model adapter. Keep the command runnable so the same evaluation inputs can be scored against both versions.

## 2. Build the deterministic core

1. Parse the declared source and structured languages into a shared typed expression model. Resolve approval records by revision and source version, and record every effective rule's source and approval evidence. Require an explicit result when approved records conflict.
2. Partition the declared ticket domain at all relevant thresholds and categorical comparisons. Include missing, null, boundary, and fallback values permitted by the contract. Verify ordered rules and follow reachable graph paths on each representative. Save evidence and a concrete ticket when outcomes differ.
3. Parse Python with `ast`, inspect the permitted constructs, and interpret the resulting restricted expressions and statements. Track syntax, inspection, and behavior in separate checks. Disclose unsupported or unavailable evidence and bounded-domain failures in coverage.

## 3. Add the model boundary

1. Define the prompt and client protocol so local scripted responses and a future transport use the same validation path. Treat artifact text and code comments as data. Keep revision approval and verdict authority in the deterministic layer.
2. Validate the response schema, complete rule enumeration, recorded artifact versions and locations, and agreement with established deterministic findings. Expose grounded clarification questions as advisory items. Record bounded request configuration, attempt outcomes, and provenance.
3. Exercise malformed and truncated JSON, invented evidence, conflicts, timeouts, oversize inputs, and an injected instruction. Confirm that a failed required check yields `INCOMPLETE` unless a deterministic blocker establishes `CHANGES_REQUIRED`.

## 4. Evaluate frozen inputs and preserve regressions

1. Generate synthetic development and holdout bundles with a separate routing oracle and labels stored outside reviewer input. Include at least three valid, six defective, and three incomplete cases. Record exact anchored evidence expectations. Keep at least four holdout cases sealed during implementation and tuning.
2. Freeze the cases, labels, generation configuration, and final reviewer before the scored holdout run. Execute the same bundle population against the baseline checkpoint, the final implementation, and the final implementation with numeric boundary representatives omitted.
3. Count false passes, false blocks, abstentions by case type, and evidence quality. Replay witnesses through the independent oracle. Collect elapsed time, model calls, and retries. Record scripted model results separately from any real-model smoke check.
4. Preserve two observed failing outputs, diagnoses, focused changes, and regression commands. Identify deliberate faults if used. Rerun the full CPU-only tests and evaluation from the pinned interpreter and link the reproducible artifacts with the engineering note.
