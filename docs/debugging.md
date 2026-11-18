# Debugging regressions

Both failures were observed during development on independently constructed test inputs. The commands below select development tests. Neither case used deliberate fault injection.

## Approval conflict left a dependent fallback marked complete

Two top-level approvals for the same rule and change order specified different thresholds. The bundle status was already `INCOMPLETE`, but the later fallback still carried `PASS` in per-rule coverage. That mattered because the model report accepts a consistency claim only for a rule that the deterministic reviewer finished checking.

```sh
python3 -m unittest discover -s tests -p test_core.py -v
```

Observed failure excerpt

```text
test_conflict_marks_the_shadowed_fallback_unreviewed (test_core.CoreTests.test_conflict_marks_the_shadowed_fallback_unreviewed) ... FAIL
AssertionError: 'PASS' != 'INCOMPLETE'
- PASS
+ INCOMPLETE

Ran 10 tests in 0.004s

FAILED (failures=1)
```

The policy traversal had recorded `effective_requirements` in the fallback's `unreviewed_checks`. Report assembly derived the final rule status only from findings with that rule's ID and global gaps, so it dropped the propagated uncertainty. The focused fix uses those per-rule gaps during report assembly. A blocker for the rule keeps its priority. The regression asserts that the conflict and dependent fallback both remain visible.

The same command returned `Ran 10 tests in 0.004s` and `OK`.

## A disputed locator could ground a different rule

Two source records claimed the same artifact, version and location with different excerpts. The deterministic reviewer reported the collision and registered the first excerpt. The model validator matched the locator alone and allowed a question about the second rule to cite that first excerpt.

```sh
python3 -m unittest discover -s tests -p test_model.py -k test_conflicting_source_locator_cannot_ground_a_second_rule -v
```

Observed failure excerpt

```text
test_conflicting_source_locator_cannot_ground_a_second_rule (test_model.ModelValidationTests.test_conflicting_source_locator_cannot_ground_a_second_rule) ... FAIL
AssertionError: ModelIssue not raised

Ran 1 test in 0.001s

FAILED (failures=1)
```

The validator now excludes disputed locations from its evidence catalog and exposes a source anchor only when it matches the canonical reference in full. The regression requires a citation using the conflicting locator to raise `ModelIssue`.

The focused rerun returned `Ran 1 test in 0.000s` and `OK`.

## Maintenance after the blind checkpoint

An independent development probe converted a supported source literal to a 237-digit base-unit integer. The counterexample contained that exact integer, but replay rejected it under the scalar literal cap. A second probe converted `1e-120` milliseconds to `1e-123` seconds and reached the scalar exponent cap. The focused test reproduced both failures.

```sh
python3 -m unittest discover -s tests -p test_core.py -k test_extreme_numeric_witnesses_roundtrip -v
```

Observed failure excerpt

```text
reviewer.logic.ContractError: Numeric literal is invalid or too large.
reviewer.logic.ContractError: Numeric exponent is too large.

Ran 1 test in 0.002s

FAILED (errors=2)
```

The encoder now previews the scalar using the replay decoder's numeric validation. It uses the existing numerator/denominator form if the scalar exceeds bounds or loses precision. The regression asserts exact decoded values for both conversions. The focused rerun returned `Ran 1 test in 0.001s` and `OK`.
