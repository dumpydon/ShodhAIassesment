# Review bundle contract

The version 1.0 bundle is a JSON object described by the [JSON Schema](../schemas/bundle.schema.json). The reviewer checks field types, reference integrity, revision scope, and predicate semantics in addition to the schema. The CLI accepts a file or standard input. The Python entry point is `reviewer.app.review(bundle)`.

## Artifacts and references

| Property | Contents |
| --- | --- |
| `revision` | The target revision identifier used to select approvals. |
| `fields` | Ticket field names and the corresponding `type`, optional `nullable`, and optional numeric time `unit`. |
| `requirements` | Versioned source clauses with unique `id`, `order`, `location`, and controlled-language `text`. |
| `specification` | Versioned ordered clauses with independent `id`, `order`, source `rule_id` or `rule_ids`, `when`, and `route`. Multiple clauses may refer to one rule. A combined clause can link multiple rules. |
| `workflow` | Versioned graph with an `entry` node ID. A `decision` has `when`, `true` and `false` destinations, and an optional `snippet_id`. A `route` node declares a terminal `route`. |
| `snippets` | Versioned collection of Python sources with unique `id` and `source`. The graph links these using `snippet_id`. |
| `approvals` | Versioned replacement records with unique `id`, `status`, `revision`, `requirement_version`, `rule_id`, `order`, and replacement `text`. |

Source and specification precedence follows increasing `order` and the first true clause. Array position conveys evidence location. Unique order values keep behavior stable when records are serialized in a different order. Workflow nodes and snippets may include explicit source rule links for traceability. The review also relates reachable behavior to the effective source rule that selects the ticket route.

A branch destination is a node ID or an object such as `{"to":"escalation","location":"diagram#aged-yes"}`. Missing edges are accepted as review input so the reviewer can report a concrete failure when such an edge is reachable. A path reaching a missing destination or repeat visit yields a blocker. Report coverage lists nodes visited and branches exercised in the declared graph or graph with linked Python decisions.

An evidence reference includes an artifact name, version, location, excerpt, and source rule ID when available. A supplied `location` takes precedence over the artifact-relative pointer, for example `/rules/0`, `/nodes/2/false`, `/items/0`, or `/records/1`. Anchor collisions are an input gap. A missing specification clause points to the original source requirement and includes applicable replacement evidence.

## Fields and conditions

Supported field types are `number`, `string`, and `boolean`. Nullable fields permit both omitted keys and explicit JSON nulls. The default is a required, non-nullable field.

- A missing test is true for an absent key or a null. A present test is true for a non-null value.
- Ordinary declarative comparisons against missing values yield false. Negation, including negation of a comparison, applies to the resulting Boolean.
- Numbers support `==`, `!=`, `<`, `<=`, `>`, and `>=`. Strings and Booleans support `==` and `!=`.
- Numeric values and literals use exact rationals in the declared base unit. A threshold may give another time unit for a field with a time unit, with exact conversion. Unit names are case-insensitive. Supported spellings include `ms`, `millisecond(s)`, `s`, `sec`, `second(s)`, `m`, `min`, `minute(s)`, `h`, `hr`, and `hour(s)`.
- A counterexample ticket uses JSON scalars in the field base unit. A rational value without an exact scalar representation uses `{"numerator":1,"denominator":60}`, suitable for exact rechecking with `reviewer.logic.decode_ticket`. An omitted key represents an absent field.

Source rules and approved replacements each use a single controlled sentence.

```text
IF category IS MISSING THEN manual
IF urgent == true THEN priority
IF waiting >= 30 minutes AND (category == "billing" OR urgent == true) THEN escalation
IF NOT (waiting IS MISSING OR waiting < 1800 seconds) THEN escalation
OTHERWISE THEN normal
```

Use case-insensitive keywords, case-sensitive field names and route IDs, JSON double-quoted strings, finite numeric literals, and `true` or `false` for Boolean literals. `NOT` binds before `AND`, which binds before `OR`. Parentheses group expressions. `missing(field)`, `present(field)`, and `IS [NOT] NULL` are supported. The fallback also accepts `ELSE normal` or `OTHERWISE normal`. A trailing period is optional. Place exceptions earlier in the explicit order so they win on overlapping inputs.

Structured conditions express the same combinations with JSON Booleans, `all`, `any`, `not`, or an atomic field test.

```json
{
  "all": [
    {"field": "waiting", "op": ">=", "value": 1800, "unit": "seconds"},
    {"any": [
      {"field": "category", "op": "==", "value": "billing"},
      {"field": "urgent", "op": "==", "value": true}
    ]}
  ]
}
```

An unsupported source clause retains its anchor and creates a review gap. Tickets reaching an unresolved clause leave the effective route undetermined. Subsequent cases that could depend on it are marked unreviewed.

## Effective requirements

A record applies only if its top-level `status` is `approved` and both `revision` and `requirement_version` match this review. `pending`, `proposed`, and `rejected` records are valid metadata and leave the source policy in force. A replacement controls the full condition and destination for its source rule, which keeps its existing priority.

Applicable records for the same rule apply in increasing approval order. The greatest order gives the active replacement after a successful reconciliation. Same-order versions that provably agree on behavior can share an order, with the approval ID providing a stable evidence tie-break. Unresolved same-order disagreements are surfaced and the affected rule remains undetermined. Approval words inside source or code are artifact content. The application uses the trusted top-level approval metadata to grant a replacement.

## Linked Python

A linked snippet defines exactly one undecorated `def decide(ticket):` with Boolean results. Statements are `return` and `if` with optional `elif` and `else`. Expressions support scalar literals in comparisons, `ticket.get("field"[, default])`, `ticket["field"]`, truth tests, `and`, `or`, `not`, chained comparisons, and identity tests with `None`, `True`, or `False`. An ordered comparison with a ticket field uses a numeric field. The inspection permits comments and docstrings as content.

```python
def decide(ticket):
    if ticket.get("waiting") is None:
        return False
    return ticket["waiting"] >= 30
```

The Python syntax parser produces an AST. The inspector accepts the listed nodes and translates them to an internal program. Evaluation interprets that program on tickets with exact rational base units. Python float constants use their binary values when compared with a rational. The interpretation models short circuit behavior, absent-key failures, type errors, and the required Boolean return. The reviewer reports syntax, inspection, and behavior as distinct checks. The code coverage field records `arbitrary_python_executed: false`.

## Equivalence and limits

For each numeric field, the reviewer collects the thresholds used in the effective policy, specification, workflow conditions, and linked Python. It builds a singleton at each threshold and one class for each intervening and exterior interval. Each comparison has the same result throughout its class. String classes cover each compared literal and one value distinct from every literal. Booleans use both values. Nullable inputs also include null and absence. The Cartesian product covers every combination of these classes. One representative per cell suffices because every supported atom, first-match choice, and safe branch interpretation is invariant inside a cell. This is exhaustive relative to the supported language and the declared domain.

Equivalent routes can use different IDs, conditions, or graph arrangements. A discrepancy includes the relevant source and observed artifact evidence and a representative ticket with expected and actual routes. A null actual route indicates the observed artifact selected no terminal route, for example due to a broken edge or a modeled Python failure.

The application bounds the input at 256 KiB, fields at 32, artifact collections at 256 items, source and snippet text at 8,000 characters, and the full partition at 20,000 cells. Further bounds apply to expression size, nesting, literal size, and the Python AST. Oversized or unsupported scopes remain visible as review gaps. The evaluation ablation `omit_boundaries` removes numeric singleton cells and records `exhaustive: false` in coverage.

## Minimal complete example

```json
{
  "schema_version": "1.0",
  "revision": "support-rev2",
  "fields": {"waiting": {"type": "number", "unit": "minutes", "nullable": true}},
  "requirements": {
    "version": "policy-v1",
    "rules": [
      {"id": "age", "order": 10, "location": "policy#age",
       "text": "IF waiting >= 30 minutes THEN escalation"},
      {"id": "default", "order": 20, "location": "policy#default",
       "text": "OTHERWISE THEN normal"}
    ]
  },
  "specification": {
    "version": "spec-v2",
    "rules": [
      {"id": "elapsed", "rule_id": "age", "order": 10,
       "when": {"field": "waiting", "op": ">=", "value": 1800, "unit": "seconds"},
       "route": "escalation"},
      {"id": "remaining", "rule_id": "default", "order": 20,
       "when": true, "route": "normal"}
    ]
  },
  "workflow": {
    "version": "workflow-v2",
    "entry": "age-check",
    "nodes": [
      {"id": "age-check", "kind": "decision", "rule_id": "age",
       "when": {"field": "waiting", "op": ">=", "value": 30},
       "true": {"to": "aged", "location": "drawing#aged"},
       "false": "remaining", "snippet_id": "is-aged"},
      {"id": "aged", "kind": "route", "route": "escalation"},
      {"id": "remaining", "kind": "route", "route": "normal"}
    ]
  },
  "snippets": {
    "version": "code-v2",
    "items": [
      {"id": "is-aged", "source": "def decide(ticket):\n    return ticket.get('waiting') is not None and ticket['waiting'] >= 30\n"}
    ]
  },
  "approvals": {"version": "approval-v2", "records": []}
}
```
