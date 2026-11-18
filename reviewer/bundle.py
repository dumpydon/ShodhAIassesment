"""Read versioned artifacts without evaluating untrusted prose or Python."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from .logic import (
    MAX_TEXT, ContractError, Fields, Predicate, cell_tickets, holds, parse_fields,
    parse_predicate, parse_source, partition, require_keys, require_route, tests_in,
)
from .python_subset import Program, SnippetSyntaxError, inspect_python, parse_python
from .report import Ref, Report


SCHEMA_VERSION = "1.0"
MAX_ITEMS = 256
ID_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}\Z")
SOURCE = "effective_requirements"


@dataclass(frozen=True)
class Section:
    artifact: str
    raw: dict
    ref: Ref
    items: list | None


@dataclass(frozen=True)
class Rule:
    id: str
    order: int
    when: Predicate | None
    route: str | None
    ref: Ref
    rule_ids: tuple[str, ...]
    approvals: tuple[str, ...] = ()


@dataclass(frozen=True)
class Change:
    id: str
    order: int
    when: Predicate | None
    route: str | None
    ref: Ref


@dataclass(frozen=True)
class Edge:
    target: str | None
    ref: Ref


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    ref: Ref
    rule_ids: tuple[str, ...]
    when: Predicate | None = None
    route: str | None = None
    yes: Edge | None = None
    no: Edge | None = None
    snippet_id: str | None = None


@dataclass(frozen=True)
class Snippet:
    id: str
    ref: Ref
    program: Program | None


def ident(value: Any, name: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ContractError(f"{name} must be a nonempty artifact identifier.")
    return value


def order(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 2**31 - 1:
        raise ContractError("Order must be a nonnegative 32-bit integer.")
    return value


def location(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 240 or "\n" in raw or "\r" in raw:
        raise ContractError("Location must be nonempty, single-line text of at most 240 characters.")
    return raw


def ref_location(raw: Any, default: str) -> str:
    try:
        return location(raw["location"])
    except (ContractError, KeyError, TypeError):
        return default


def references(raw: dict, *, required: bool = False) -> tuple[str, ...]:
    one = "rule_id" in raw
    many = "rule_ids" in raw
    if one and many:
        raise ContractError("Use rule_id or rule_ids once per artifact item.")
    if one:
        return (ident(raw["rule_id"], "rule_id"),)
    if many:
        values = raw["rule_ids"]
        if not isinstance(values, list) or not values or len(values) > MAX_ITEMS:
            raise ContractError("rule_ids must be a nonempty, bounded list.")
        ids = tuple(ident(item, "rule_ids item") for item in values)
        if len(ids) != len(set(ids)):
            raise ContractError("rule_ids cannot contain duplicate IDs.")
        return ids
    if required:
        raise ContractError("A specification rule requires a source rule_id or rule_ids.")
    return ()


def _check_location(raw: dict) -> None:
    if "location" in raw:
        location(raw["location"])


class Bundle:
    def __init__(self, raw: Any, report: Report):
        self.report = report
        self.raw = raw if isinstance(raw, dict) else {}
        version = self.raw.get("schema_version")
        self.ref = report.ref(
            "bundle",
            version if isinstance(version, str) and len(version) <= 128 else "unknown",
            "/",
            {"schema_version": version, "revision": self.raw.get("revision")},
        )
        self.fields: Fields | None = None
        self.revision: str | None = None
        try:
            require_keys(
                raw,
                {"schema_version", "revision", "fields", "requirements", "specification",
                 "workflow", "snippets", "approvals"},
                {"id", "case_id", "bundle_id"},
            )
            if version != SCHEMA_VERSION:
                raise ContractError(f"Expected schema_version {SCHEMA_VERSION!r}.")
        except ContractError as error:
            report.issue("input_contract", "INVALID_INPUT", str(error), self.ref, "Use the v1 bundle schema.")
        try:
            self.revision = ident(self.raw.get("revision"), "Target revision")
        except ContractError as error:
            report.issue(
                SOURCE, "REVISION_MISSING", str(error), self.ref,
                "Provide the target revision identifier for approval scoping.",
            )
        fields_ref = self.report.ref("bundle", self.ref["version"], "/fields", self.raw.get("fields"))
        try:
            self.fields = parse_fields(self.raw.get("fields"))
        except ContractError as error:
            report.issue(
                "semantic_domain", "INVALID_FIELDS", str(error), fields_ref,
                "Declare each input field, type, nullability, and applicable time unit.",
            )

    def section(self, artifact: str, collection: str, check: str, *required: str) -> Section:
        supplied = self.raw.get(artifact)
        raw = supplied if isinstance(supplied, dict) else {}
        version = "unknown"
        try:
            version = ident(raw.get("version"), "Artifact version")
        except ContractError:
            pass
        root = self.report.ref(
            artifact,
            version,
            ref_location(raw, "/"),
            {k: v for k, v in raw.items() if k != collection}
            if isinstance(supplied, dict)
            else f"No {artifact} object supplied.",
        )
        items = None
        try:
            require_keys(supplied, {"version", collection, *required}, {"location"})
            _check_location(raw)
            ident(raw["version"], "Artifact version")
            values = raw[collection]
            if not isinstance(values, list) or len(values) > MAX_ITEMS:
                raise ContractError(f"{collection} must be an array of at most {MAX_ITEMS} items.")
            items = values
        except ContractError as error:
            self.report.issue(
                check, "ARTIFACT_INVALID", str(error), root, f"Supply a versioned {artifact} artifact."
            )
        return Section(artifact, raw, root, items)

    def item_ref(
        self,
        section: Section,
        path: str,
        raw: Any,
        *,
        text: str | None = None,
        rule_id: str | None = None,
    ) -> Ref:
        contents = raw
        if isinstance(raw, dict) and text and isinstance(raw.get(text), str):
            contents = raw[text]
        return self.report.ref(
            section.artifact, section.ref["version"], ref_location(raw, path), contents, rule_id=rule_id
        )

    def source_rules(self, section: Section) -> tuple[list[Rule], bool]:
        if section.items is None:
            return [], False
        result = []
        unique_ids = set()
        unique_orders = set()
        ordered = True
        for index, raw in enumerate(section.items):
            source_id = raw.get("id") if isinstance(raw, dict) else None
            if not isinstance(source_id, str) or not ID_PATTERN.fullmatch(source_id):
                source_id = None
            ref = self.item_ref(section, f"/rules/{index}", raw, text="text", rule_id=source_id)
            try:
                obj = require_keys(raw, {"id", "order", "location", "text"})
                _check_location(obj)
                rule_id = ident(obj["id"], "Source rule ID")
                priority = order(obj["order"])
                if rule_id in unique_ids or priority in unique_orders:
                    raise ContractError("Source rule IDs and order values must be unique.")
                unique_ids.add(rule_id)
                unique_orders.add(priority)
            except ContractError as error:
                self.report.issue(
                    SOURCE, "SOURCE_ANCHOR_INVALID", str(error), ref,
                    "Give each requirement a unique ID, location, and explicit order.",
                )
                if source_id is not None and source_id not in unique_ids:
                    unique_ids.add(source_id)
                    result.append(Rule(source_id, index, None, None, ref, (source_id,)))
                ordered = False
                continue
            when = route = None
            if self.fields is not None:
                try:
                    when, route = parse_source(obj["text"], self.fields)
                except ContractError as error:
                    self.report.issue(
                        SOURCE, "SOURCE_UNSUPPORTED", str(error), ref,
                        "State the anchored rule in the supported controlled language.",
                    )
            result.append(Rule(rule_id, priority, when, route, ref, (rule_id,)))
        if not result:
            self.report.issue(
                SOURCE, "SOURCE_EMPTY", "No anchored requirements could be reviewed.", section.ref,
                "Supply at least one anchored routing rule.",
            )
            ordered = False
        return sorted(result, key=lambda item: item.order), ordered

    def effective_rules(self, source: list[Rule], section: Section) -> list[Rule]:
        if section.items is None or self.revision is None or self.fields is None:
            return [replace(rule, when=None, route=None) for rule in source]
        rules = {rule.id: rule for rule in source}
        grouped: dict[str, list[Change]] = {}
        seen: set[str] = set()
        invalid_scope = False
        for index, raw in enumerate(section.items):
            source_id = raw.get("rule_id") if isinstance(raw, dict) else None
            source_id = source_id if isinstance(source_id, str) and source_id in rules else None
            ref = self.item_ref(section, f"/records/{index}", raw, text="text", rule_id=source_id)
            try:
                obj = require_keys(
                    raw, {"id", "status", "revision", "requirement_version", "rule_id", "order", "text"},
                    {"location"},
                )
                _check_location(obj)
                change_id = ident(obj["id"], "Approval ID")
                if change_id in seen:
                    raise ContractError("Approval IDs must be unique.")
                seen.add(change_id)
                status = obj["status"]
                if status not in ("approved", "pending", "proposed", "rejected"):
                    raise ContractError("Unsupported approval status.")
                target = ident(obj["revision"], "Approval revision")
                requirement_version = ident(obj["requirement_version"], "Approval requirement version")
                ident(obj["rule_id"], "Approval source rule ID")
                sequence = order(obj["order"])
                if not isinstance(obj["text"], str) or not obj["text"].strip() or len(obj["text"]) > MAX_TEXT:
                    raise ContractError("Approval text must be nonempty and within the source size limit.")
            except ContractError as error:
                self.report.issue(
                    SOURCE, "APPROVAL_INVALID", str(error), ref,
                    "Provide unique, top-level approval metadata and bounded replacement text.",
                )
                invalid_scope = True
                continue
            if status != "approved" or target != self.revision:
                continue
            if source and requirement_version != source[0].ref["version"]:
                continue
            if source_id is None:
                self.report.issue(
                    SOURCE, "APPROVAL_REFERENCE_UNKNOWN",
                    f"Applicable approval references unknown source rule {obj['rule_id']!r}.", ref,
                    "Correct the rule reference or scope the change to its intended source version.",
                )
                continue
            when = route = None
            try:
                when, route = parse_source(obj["text"], self.fields)
            except ContractError as error:
                self.report.issue(
                    SOURCE, "APPROVAL_UNSUPPORTED", str(error), ref,
                    "Write the approved replacement as one supported routing rule.",
                    refs=[rules[source_id].ref],
                )
            grouped.setdefault(source_id, []).append(Change(change_id, sequence, when, route, ref))

        if invalid_scope:
            return [replace(rule, when=None, route=None) for rule in source]

        resolved = []
        for original in source:
            records = sorted(grouped.get(original.id, ()), key=lambda change: (change.order, change.id))
            if not records:
                resolved.append(original)
                continue
            unresolved = False
            previous = None
            for change in records:
                if change.when is None:
                    unresolved = True
                if previous and previous.order == change.order:
                    if previous.when is not None and change.when is not None and not self._equivalent(previous, change):
                        self.report.issue(
                            SOURCE, "APPROVAL_CONFLICT",
                            f"Approved replacements disagree at change order {change.order}.", change.ref,
                            "Reconcile the approvals by approving one explicit, ordered replacement.",
                            refs=[original.ref, previous.ref],
                        )
                        unresolved = True
                previous = change
            active = records[-1]
            resolved.append(
                replace(
                    original,
                    when=None if unresolved else active.when,
                    route=None if unresolved else active.route,
                    ref=original.ref if unresolved else active.ref,
                    approvals=tuple(record.id for record in records),
                )
            )
        return resolved

    def _equivalent(self, a: Change, b: Change) -> bool:
        assert self.fields is not None and a.when is not None and b.when is not None
        if a.when == b.when and a.route == b.route:
            return True
        atoms = [*tests_in(a.when), *tests_in(b.when)]
        fields = {test.field: self.fields[test.field] for test in atoms}
        try:
            cells = partition(fields, atoms)
        except ContractError:
            return False
        return all(
            (a.route if holds(a.when, item) else None)
            == (b.route if holds(b.when, item) else None)
            for item in cell_tickets(cells)
        )

    def spec_rules(
        self, section: Section, known_ids: set[str]
    ) -> tuple[list[Rule], bool, set[str] | None]:
        if section.items is None:
            return [], False, None
        result = []
        unique_ids = set()
        unique_orders = set()
        covered = set()
        ordered = True
        mapping_known = True
        for index, raw in enumerate(section.items):
            links: tuple[str, ...] = ()
            try:
                links = references(raw, required=True) if isinstance(raw, dict) else ()
                if not links:
                    raise ContractError("Expected a specification rule object.")
                covered.update(links)
            except ContractError:
                mapping_known = False
            ref = self.item_ref(section, f"/rules/{index}", raw, rule_id=links[0] if len(links) == 1 else None)
            ordered_item = True
            try:
                if not isinstance(raw, dict):
                    raise ContractError("Expected a specification rule object.")
                rule_id = ident(raw.get("id"), "Specification rule ID")
                priority = order(raw.get("order"))
                if rule_id in unique_ids or priority in unique_orders:
                    raise ContractError("Specification rule IDs and order values must be unique.")
                unique_ids.add(rule_id)
                unique_orders.add(priority)
            except ContractError as error:
                self.report.issue(
                    "specification", "SPEC_ORDER_INVALID", str(error), ref,
                    "Give each specification rule a unique ID and explicit order.",
                )
                ordered_item = False
                ordered = False
                rule_id, priority = "", index
            when = route = None
            if ordered_item:
                try:
                    obj = require_keys(raw, {"id", "order", "when", "route"}, {"rule_id", "rule_ids", "location"})
                    _check_location(obj)
                    references(obj, required=True)
                    if self.fields is not None:
                        when = parse_predicate(obj["when"], self.fields)
                        route = require_route(obj["route"])
                except ContractError as error:
                    self.report.issue(
                        "specification", "SPEC_UNSUPPORTED", str(error), ref,
                        "Use a supported predicate, route, and source rule link.",
                    )
                self._check_links("specification", links, known_ids, ref)
            result.append(Rule(rule_id, priority, when, route, ref, links))
        return sorted(result, key=lambda item: item.order), ordered, covered if mapping_known else None

    def _check_links(self, check: str, links: tuple[str, ...], known_ids: set[str], ref: Ref) -> None:
        missing = sorted(set(links) - known_ids)
        if missing:
            self.report.issue(
                check, "RULE_REFERENCE_UNKNOWN", f"Unknown source rule references {', '.join(missing)}.",
                ref, "Link to the IDs of anchored source requirements.",
            )

    def graph(self, section: Section, known_ids: set[str]) -> tuple[str | None, dict[str, Node]]:
        if section.items is None:
            return None, {}
        entry = None
        try:
            entry = ident(section.raw["entry"], "Workflow entry")
        except ContractError as error:
            self.report.issue(
                "workflow", "WORKFLOW_ENTRY_INVALID", str(error), section.ref,
                "Provide an entry node ID for the workflow.",
            )
        result = {}
        duplicates = set()
        for index, raw in enumerate(section.items):
            path = f"/nodes/{index}"
            links: tuple[str, ...] = ()
            try:
                if isinstance(raw, dict):
                    links = references(raw)
            except ContractError:
                pass
            ref = self.item_ref(section, path, raw, rule_id=links[0] if len(links) == 1 else None)
            node_id = None
            try:
                if not isinstance(raw, dict):
                    raise ContractError("Expected a workflow node object.")
                node_id = ident(raw.get("id"), "Workflow node ID")
                if node_id in result:
                    duplicates.add(node_id)
                    raise ContractError("Workflow node IDs must be unique.")
                kind = raw.get("kind")
                _check_location(raw)
                links = references(raw)
                optional = {"location", "rule_id", "rule_ids"}
                if kind == "route":
                    require_keys(raw, {"id", "kind", "route"}, optional)
                    node = Node(node_id, kind, ref, links, route=require_route(raw["route"]))
                elif kind == "decision":
                    require_keys(raw, {"id", "kind", "when"}, optional | {"true", "false", "snippet_id"})
                    yes = self.edge(section, path + "/true", raw.get("true"), "true" in raw, ref)
                    no = self.edge(section, path + "/false", raw.get("false"), "false" in raw, ref)
                    snippet_id = ident(raw["snippet_id"], "Workflow snippet_id") if "snippet_id" in raw else None
                    when = parse_predicate(raw["when"], self.fields) if self.fields is not None else None
                    node = Node(node_id, kind, ref, links, when=when, yes=yes, no=no, snippet_id=snippet_id)
                else:
                    raise ContractError("Workflow kind must be decision or route.")
            except ContractError as error:
                self.report.issue(
                    "workflow", "WORKFLOW_NODE_UNSUPPORTED", str(error), ref,
                    "Use a unique decision or route node with supported properties.",
                )
                if node_id is None or node_id in duplicates:
                    continue
                node = Node(node_id, "unknown", ref, links)
            self._check_links("workflow", links, known_ids, ref)
            result[node.id] = node
        for node_id in duplicates:
            result[node_id] = replace(result[node_id], kind="unknown")
        return entry, result

    def edge(self, section: Section, path: str, raw: Any, provided: bool, source: Ref) -> Edge:
        ref = self.item_ref(
            section, path, raw if provided else "Branch destination is absent.",
            rule_id=source.get("rule_id"),
        )
        if not provided:
            return Edge(None, ref)
        try:
            if isinstance(raw, dict):
                require_keys(raw, {"to"}, {"location"})
                _check_location(raw)
                raw = raw["to"]
            return Edge(ident(raw, "Branch destination"), ref)
        except ContractError as error:
            self.report.issue(
                "workflow", "WORKFLOW_BRANCH_INVALID", str(error), ref,
                "Supply a valid node ID as the branch destination.", refs=[source],
            )
            return Edge(None, ref)

    def snippets(self, section: Section, known_ids: set[str]) -> dict[str, Snippet]:
        result = {}
        for index, raw in enumerate(section.items or ()):
            links = ()
            try:
                links = references(raw) if isinstance(raw, dict) else ()
            except ContractError:
                pass
            ref = self.item_ref(
                section, f"/items/{index}", raw, text="source",
                rule_id=links[0] if len(links) == 1 else None,
            )
            snippet_id = None
            program = None
            check, code = "code_inspection", "CODE_UNSUPPORTED"
            try:
                obj = require_keys(raw, {"id", "source"}, {"location", "rule_id", "rule_ids"})
                _check_location(obj)
                snippet_id = ident(obj["id"], "Snippet ID")
                if snippet_id in result:
                    raise ContractError("Snippet IDs must be unique.")
                links = references(obj)
                self._check_links("code_inspection", links, known_ids, ref)
                check, code = "code_syntax", "CODE_SYNTAX"
                tree = parse_python(obj["source"])
                check, code = "code_inspection", "CODE_UNSUPPORTED"
                if self.fields is not None:
                    program = inspect_python(tree, self.fields)
            except ContractError as error:
                if isinstance(error, SnippetSyntaxError):
                    code = "CODE_SYNTAX"
                self.report.issue(
                    check, code, str(error), ref,
                    "Supply a syntactically valid linked decision using the restricted Python subset.",
                )
            if snippet_id is not None:
                result[snippet_id] = Snippet(snippet_id, ref, program)
        return result
