"""Deterministic cross-artifact semantic review."""

from __future__ import annotations

from dataclasses import dataclass
from math import prod
from typing import Any

from .bundle import SOURCE, Bundle, Edge, Node, Rule, Snippet
from .logic import MAX_CELLS, DomainLimit, Ticket, cell_tickets, holds, partition, tests_in
from .python_subset import SnippetRuntimeError
from .report import Ref, Report


@dataclass(frozen=True)
class Selection:
    route: str | None
    rule: Rule | None = None
    unknown: bool = False


@dataclass(frozen=True)
class Step:
    node: Node
    answer: bool
    edge: Edge


@dataclass(frozen=True)
class Failure:
    code: str
    message: str
    ref: Ref
    blocker: bool = True


@dataclass
class Trace:
    steps: list[Step]
    visited: list[Node]
    route: str | None = None
    failure: Failure | None = None

    @property
    def evidence(self) -> list[Ref]:
        return [*(node.ref for node in self.visited), *(step.edge.ref for step in self.steps)]


def _select(rules: list[Rule], ticket: Ticket) -> Selection:
    for rule in rules:
        if rule.when is None:
            return Selection(None, rule, unknown=True)
        if holds(rule.when, ticket):
            return Selection(rule.route, rule, unknown=rule.route is None)
    return Selection(None)


def _snippet_value(snippet: Snippet, ticket: Ticket) -> bool | SnippetRuntimeError:
    assert snippet.program is not None
    try:
        return snippet.program.decide(ticket)
    except SnippetRuntimeError as error:
        return error


def _walk(
    entry: str,
    nodes: dict[str, Node],
    root: Ref,
    ticket: Ticket,
    code_values: dict[str, bool | SnippetRuntimeError] | None = None,
) -> Trace:
    result = Trace([], [])
    entered = set()
    current = entry
    while True:
        branch_ref = result.steps[-1].edge.ref if result.steps else root
        if current in entered:
            result.failure = Failure(
                "WORKFLOW_CYCLE", f"The ticket revisits workflow node {current!r}.", branch_ref
            )
            return result
        node = nodes.get(current)
        if node is None:
            result.failure = Failure(
                "WORKFLOW_MISSING_BRANCH", f"The path targets absent node {current!r}.", branch_ref
            )
            return result
        result.visited.append(node)
        entered.add(current)
        if node.kind == "route":
            result.route = node.route
            return result
        if node.kind != "decision" or node.when is None:
            result.failure = Failure(
                "WORKFLOW_UNREVIEWED", "The path reaches an unsupported node.", node.ref, blocker=False
            )
            return result
        if code_values is not None and node.snippet_id is not None:
            decision = code_values.get(node.snippet_id)
            if decision is None or isinstance(decision, SnippetRuntimeError):
                result.failure = Failure(
                    "CODE_UNREVIEWED", "The path could not obtain a linked Boolean result.",
                    node.ref, blocker=False,
                )
                return result
            answer = decision
        else:
            answer = holds(node.when, ticket)
        edge = node.yes if answer else node.no
        assert edge is not None
        result.steps.append(Step(node, answer, edge))
        if edge.target is None:
            result.failure = Failure(
                "WORKFLOW_MISSING_BRANCH",
                f"The reachable {str(answer).lower()} branch has no valid destination.",
                edge.ref,
            )
            return result
        current = edge.target


def _rule_evidence(rule: Rule | None, source_ref: dict[str, Ref]) -> list[Ref]:
    return [source_ref[rule.id], rule.ref] if rule is not None else []


def _spec_ref(
    expected: Rule, selected: Rule | None, specification: list[Rule], ticket: Ticket, root: Ref
) -> Ref:
    for item in specification:
        if item is selected:
            return item.ref
        if expected.id in item.rule_ids and item.when is not None and not holds(item.when, ticket):
            return item.ref
    return root


def _workflow_ref(
    expected: Rule, declared: Trace, ticket: Ticket, effective: dict[str, Rule], root: Ref,
) -> Ref:
    for step in declared.steps:
        for rule_id in step.node.rule_ids:
            rule = effective.get(rule_id)
            if rule is not None and rule.when is not None and holds(rule.when, ticket) != step.answer:
                return step.node.ref
        if expected.id in step.node.rule_ids and step.answer:
            return step.edge.ref
    if declared.steps:
        return declared.steps[-1].edge.ref
    return declared.visited[-1].ref if declared.visited else root


def _check_specification(
    report: Report, expected: Selection, spec: Selection, specification: list[Rule],
    root: Ref, ticket: Ticket, source_ref: dict[str, Ref],
) -> None:
    rule = expected.rule
    if expected.unknown or rule is None or spec.unknown or expected.route == spec.route:
        return
    report.issue(
        "specification", "SPEC_ROUTE_MISMATCH",
        "The ordered specification sends this ticket to a different route.",
        _spec_ref(rule, spec.rule, specification, ticket, root),
        "Align the effective predicate, precedence, and route for the cited source rule.",
        blocker=True, rule_id=rule.id,
        refs=[*_rule_evidence(rule, source_ref), *([spec.rule.ref] if spec.rule else [])],
        ticket=ticket, expected_route=expected.route, actual_route=spec.route,
    )


def _check_workflow(
    report: Report, expected: Selection, trace: Trace, effective: dict[str, Rule],
    root: Ref, ticket: Ticket, source_ref: dict[str, Ref], *, linked: bool = False,
) -> None:
    rule = expected.rule if not expected.unknown else None
    if trace.failure is not None:
        failure = trace.failure
        if failure.blocker:
            report.issue(
                "workflow", failure.code, failure.message, failure.ref,
                "Connect each reachable branch to a terminating route.",
                blocker=True, rule_id=rule.id if rule else None,
                refs=[*_rule_evidence(rule, source_ref), *trace.evidence],
                ticket=ticket, expected_route=expected.route, actual_route=None,
                details={"traversal": "linked_python" if linked else "declared_workflow"},
            )
        return
    if linked or rule is None or expected.route == trace.route:
        return
    report.issue(
        "workflow", "WORKFLOW_ROUTE_MISMATCH",
        "The reachable workflow path sends this ticket to a different route.",
        _workflow_ref(rule, trace, ticket, effective, root),
        "Align the cited decision or branch with the effective requirement.",
        blocker=True, rule_id=rule.id,
        refs=[*_rule_evidence(rule, source_ref), *trace.evidence],
        ticket=ticket, expected_route=expected.route, actual_route=trace.route,
    )


def _check_code(
    report: Report, ticket: Ticket, expected: Selection, source_ref: dict[str, Ref],
    declared: Trace, linked: Trace, snippets: dict[str, Snippet],
    code_values: dict[str, bool | SnippetRuntimeError], interpreted: set[str],
) -> None:
    visited = {node.id: node for node in (*declared.visited, *linked.visited)}
    for node in visited.values():
        snippet_id = node.snippet_id
        if snippet_id is None or snippet_id not in code_values or node.when is None:
            continue
        interpreted.add(snippet_id)
        snippet = snippets[snippet_id]
        actual_branch = code_values[snippet_id]
        expected_branch = holds(node.when, ticket)
        rule = expected.rule if not expected.unknown else None
        rule_id = node.rule_ids[0] if len(node.rule_ids) == 1 else rule.id if rule else None
        linked_source = source_ref.get(rule_id, {}) if rule_id else {}
        refs = [*_rule_evidence(rule, source_ref), node.ref]
        if linked_source:
            refs.append(linked_source)
        if isinstance(actual_branch, SnippetRuntimeError):
            code, message = "CODE_EXECUTION_ERROR", str(actual_branch)
            actual = None
        elif actual_branch != expected_branch:
            code, message = "CODE_BEHAVIOR_MISMATCH", "Linked Python selects a different branch."
            actual = linked.route
        else:
            continue
        report.issue(
            "code_behavior", code, message, snippet.ref,
            "Align the linked Boolean decision with the declared predicate on reachable inputs.",
            blocker=True, rule_id=rule_id,
            refs=[*refs, *declared.evidence, *linked.evidence],
            ticket=ticket, expected_route=declared.route or expected.route, actual_route=actual,
            details={
                "workflow_node_id": node.id,
                "expected_branch": expected_branch,
                "actual_branch": None if isinstance(actual_branch, SnippetRuntimeError) else actual_branch,
                "effective_route": expected.route,
            },
        )


def review_core(bundle: Any, *, ablation: str | None = None) -> dict:
    """Return deterministic findings, checks, and scope for a JSON review bundle."""
    if ablation not in (None, "omit_boundaries"):
        raise ValueError(f"Unknown deterministic ablation {ablation!r}.")
    report = Report()
    data = Bundle(bundle, report)
    source = data.section("requirements", "rules", SOURCE)
    approval = data.section("approvals", "records", SOURCE)
    spec = data.section("specification", "rules", "specification")
    workflow = data.section("workflow", "nodes", "workflow", "entry")
    code = data.section("snippets", "items", "code_inspection")

    original, source_ordered = data.source_rules(source)
    effective = data.effective_rules(original, approval)
    source_ref = {item.id: item.ref for item in original}
    effective_by_id = {item.id: item for item in effective}
    specification, spec_ordered, mapped = data.spec_rules(spec, set(source_ref))
    entry, nodes = data.graph(workflow, set(source_ref))
    snippets = data.snippets(code, set(source_ref))

    for rule in original:
        if mapped is not None and rule.id not in mapped and rule.when is not None:
            report.issue(
                "specification", "SPEC_OMISSION",
                f"No specification clause is linked to source requirement {rule.id!r}.",
                rule.ref, "Add a specification clause or link the clause that implements this requirement.",
                blocker=True, refs=[effective_by_id[rule.id].ref, spec.ref],
            )

    linked_ids = {
        node.snippet_id for node in nodes.values() if node.snippet_id is not None
    }
    for node in nodes.values():
        if node.snippet_id is not None and node.snippet_id not in snippets:
            report.issue(
                "code_inspection", "SNIPPET_MISSING",
                f"Workflow node {node.id!r} references absent snippet {node.snippet_id!r}.",
                node.ref, "Supply the linked snippet in the versioned snippets artifact.", refs=[code.ref],
            )

    rules = {
        item.id: {
            "rule_id": item.id,
            "source": item.ref,
            "effective": effective_by_id[item.id].ref if effective_by_id[item.id].when is not None else None,
            "approval_ids": list(effective_by_id[item.id].approvals),
            "specification_linked": None if mapped is None else item.id in mapped,
            "matched_cells": 0,
            "unreviewed_checks": set(),
        }
        for item in original
    }
    domain = {
        "strategy": "numeric_intervals_only" if ablation else "exact_predicate_partition",
        "exhaustive": False,
        "candidate_cells": None,
        "checked_cells": 0,
        "limit": MAX_CELLS,
    }
    reachable_nodes = set()
    reachable_branches = set()
    interpreted = set()
    if data.fields is not None:
        predicates = (
            *[item.when for item in effective],
            *[item.when for item in specification],
            *[node.when for node in nodes.values()],
        )
        programs = {
            snippet.id: snippet for snippet in snippets.values()
            if snippet.id in linked_ids and snippet.program is not None
        }
        atoms = [atom for predicate in predicates for atom in tests_in(predicate)]
        for snippet in programs.values():
            assert snippet.program is not None
            atoms.extend(snippet.program.tests)
        try:
            cells = partition(data.fields, atoms, omit_boundaries=ablation is not None)
        except DomainLimit as error:
            domain["candidate_cells"] = error.cells
            report.issue(
                "semantic_domain", "DOMAIN_LIMIT", str(error), data.ref,
                "Reduce the declared field domain or split the workflow into independent review bundles.",
            )
        else:
            domain["candidate_cells"] = prod(map(len, cells.values()))
            domain["exhaustive"] = ablation is None
            domain["cells_by_field"] = {name: len(values) for name, values in cells.items()}
            for ticket in cell_tickets(cells):
                domain["checked_cells"] += 1
                expected = _select(effective, ticket) if source_ordered else Selection(None, unknown=True)
                if expected.unknown and expected.rule is not None:
                    for rule in effective:
                        if rule.order >= expected.rule.order:
                            rules[rule.id]["unreviewed_checks"].add(SOURCE)
                if not expected.unknown:
                    if expected.rule is not None:
                        rules[expected.rule.id]["matched_cells"] += 1
                    else:
                        report.issue(
                            SOURCE, "SOURCE_NO_ROUTE",
                            "The effective policy does not specify a route for this ticket.", source.ref,
                            "Add a supported rule or a final fallback for the uncovered input.",
                            ticket=ticket,
                        )
                if spec_ordered:
                    selected = _select(specification, ticket)
                    _check_specification(report, expected, selected, specification, spec.ref, ticket, source_ref)
                    if selected.unknown and not expected.unknown and expected.rule is not None:
                        rules[expected.rule.id]["unreviewed_checks"].add("specification")
                if entry is None:
                    continue
                declared = _walk(entry, nodes, workflow.ref, ticket)
                _check_workflow(report, expected, declared, effective_by_id, workflow.ref, ticket, source_ref)
                if declared.failure is not None and not declared.failure.blocker and expected.rule:
                    rules[expected.rule.id]["unreviewed_checks"].add("workflow")
                traces = [declared]
                if linked_ids:
                    values = {
                        identifier: _snippet_value(snippet, ticket)
                        for identifier, snippet in programs.items()
                    }
                    linked = _walk(entry, nodes, workflow.ref, ticket, values)
                    traces.append(linked)
                    _check_workflow(
                        report, expected, linked, effective_by_id, workflow.ref, ticket, source_ref, linked=True
                    )
                    _check_code(report, ticket, expected, source_ref, declared, linked, snippets, values, interpreted)
                    if linked.failure and not linked.failure.blocker and expected.rule:
                        rules[expected.rule.id]["unreviewed_checks"].add("code_behavior")
                for trace in traces:
                    reachable_nodes.update(node.id for node in trace.visited)
                    reachable_branches.update(step.edge.ref["location"] for step in trace.steps)

    for coverage in rules.values():
        coverage["unreviewed_checks"] = sorted(coverage["unreviewed_checks"])
    return report.finish(
        list(rules.values()),
        domain,
        {
            "workflow": {
                "reachable_nodes": sorted(reachable_nodes),
                "reachable_branches": sorted(reachable_branches),
                "unvisited_nodes": sorted(nodes.keys() - reachable_nodes),
            },
            "code": {
                "linked_snippets": sorted(linked_ids),
                "interpreted_snippets": sorted(interpreted),
                "unlinked_snippets": sorted(snippets.keys() - linked_ids),
                "arbitrary_python_executed": False,
            },
        },
    )
