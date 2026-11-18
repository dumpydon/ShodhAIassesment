"""Exact predicates and ticket partitions for the declared routing language."""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass
from fractions import Fraction
from math import prod
from typing import Any, Iterable


class ContractError(ValueError):
    """An artifact uses an invalid or unsupported contract feature."""


class DomainLimit(ContractError):
    def __init__(self, cells: int, limit: int):
        self.cells = cells
        self.limit = limit
        super().__init__(f"Ticket partition has {cells} cells and exceeds the limit of {limit}.")


ABSENT = object()
MAX_FIELDS = 32
MAX_CELLS = 20_000
MAX_DEPTH = 24
MAX_ATOMS = 128
MAX_NUMBER_DIGITS = 120
MAX_TEXT = 8_000
FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
ROUTE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*\Z")
NUMBER = r"-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
NUMBER_PATTERN = re.compile(fr"{NUMBER}\Z", re.ASCII)
TIME_SCALE = {
    "ms": Fraction(1, 1000),
    "millisecond": Fraction(1, 1000),
    "milliseconds": Fraction(1, 1000),
    "s": Fraction(1),
    "sec": Fraction(1),
    "second": Fraction(1),
    "seconds": Fraction(1),
    "m": Fraction(60),
    "min": Fraction(60),
    "minute": Fraction(60),
    "minutes": Fraction(60),
    "h": Fraction(3600),
    "hr": Fraction(3600),
    "hour": Fraction(3600),
    "hours": Fraction(3600),
}
RELATIONS = {"==", "!=", "<", "<=", ">", ">="}


@dataclass(frozen=True)
class Field:
    kind: str
    nullable: bool
    unit: str | None


@dataclass(frozen=True)
class Test:
    field: str
    op: str
    value: Any = None


@dataclass(frozen=True)
class Group:
    op: str
    terms: tuple[Predicate, ...]


Predicate = bool | Test | Group
Fields = dict[str, Field]
Ticket = dict[str, Any]


def exact_number(value: Any, *, from_text: bool = False) -> Fraction:
    if type(value) not in (int, float) and not (from_text and isinstance(value, str)):
        raise ContractError("A numeric comparison requires a finite number.")
    try:
        text = str(value)
    except ValueError as error:
        raise ContractError("Numeric literal is too large.") from error
    if len(text) > MAX_NUMBER_DIGITS or not NUMBER_PATTERN.fullmatch(text):
        raise ContractError("Numeric literal is invalid or too large.")
    exponent = re.search(r"[eE]([+-]?\d+)$", text)
    if exponent and abs(int(exponent[1])) > MAX_NUMBER_DIGITS:
        raise ContractError("Numeric exponent is too large.")
    return Fraction(text)


def require_route(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 128 or not ROUTE_NAME.fullmatch(value):
        raise ContractError("Route must be a nonempty route identifier.")
    return value


def require_keys(obj: Any, required: set[str], optional: set[str] | None = None) -> dict:
    if not isinstance(obj, dict) or any(not isinstance(key, str) for key in obj):
        raise ContractError("Expected a JSON object.")
    missing = required - obj.keys()
    if missing:
        raise ContractError(f"Missing properties: {', '.join(sorted(missing))}.")
    unknown = obj.keys() - required - (optional or set())
    if unknown:
        raise ContractError(f"Unsupported properties: {', '.join(sorted(unknown))}.")
    return obj


def parse_fields(raw: Any) -> Fields:
    if not isinstance(raw, dict) or len(raw) > MAX_FIELDS:
        raise ContractError(f"Expected a fields object with at most {MAX_FIELDS} fields.")
    fields = {}
    for name, metadata in raw.items():
        if not isinstance(name, str) or len(name) > 128 or not FIELD_NAME.fullmatch(name):
            raise ContractError("Field names must use letters, digits, or underscores.")
        obj = require_keys(metadata, {"type"}, {"unit", "nullable"})
        kind = obj["type"]
        if kind not in ("number", "string", "boolean"):
            raise ContractError(f"Unsupported type for field {name!r}.")
        nullable = obj.get("nullable", False)
        if not isinstance(nullable, bool):
            raise ContractError(f"Field {name!r} has a non-Boolean nullable value.")
        unit = obj.get("unit")
        if "unit" in obj and (
            kind != "number" or not isinstance(unit, str) or unit.lower() not in TIME_SCALE
        ):
            raise ContractError(f"Unsupported time unit for field {name!r}.")
        fields[name] = Field(kind, nullable, unit.lower() if unit else None)
    return fields


def test_predicate(
    fields: Fields, name: Any, op: Any, value: Any = ABSENT, unit: Any = ABSENT
) -> Test:
    if not isinstance(name, str) or name not in fields:
        raise ContractError(f"Undeclared field {name!r}.")
    field = fields[name]
    if op in ("missing", "present"):
        if value is not ABSENT or unit is not ABSENT:
            raise ContractError("A missing/present test takes no value or unit.")
        return Test(name, op)
    if not isinstance(op, str) or op not in RELATIONS or value is ABSENT:
        raise ContractError("Comparison requires a supported operator and a value.")
    if field.kind == "number":
        number = value if isinstance(value, Fraction) else exact_number(value)
        if unit is not ABSENT:
            if not isinstance(unit, str) or unit.lower() not in TIME_SCALE or field.unit is None:
                raise ContractError(f"Cannot use unit {unit!r} for field {name!r}.")
            number = number * TIME_SCALE[unit.lower()] / TIME_SCALE[field.unit]
        return Test(name, op, number)
    if unit is not ABSENT or op not in ("==", "!="):
        raise ContractError(f"Field {name!r} only supports equality comparisons without a unit.")
    if field.kind == "string" and type(value) is str and len(value) <= MAX_TEXT:
        return Test(name, op, value)
    if field.kind == "boolean" and type(value) is bool:
        return Test(name, op, value)
    raise ContractError(f"Comparison value has the wrong type for field {name!r}.")


def parse_predicate(raw: Any, fields: Fields) -> Predicate:
    remaining = MAX_ATOMS

    def parse(value: Any, depth: int) -> Predicate:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_DEPTH:
            raise ContractError("Predicate exceeds the size or nesting limit.")
        if isinstance(value, bool):
            return value
        if not isinstance(value, dict):
            raise ContractError("Predicate must be a Boolean or an expression object.")
        if set(value) == {"not"}:
            return Group("not", (parse(value["not"], depth + 1),))
        for op in ("all", "any"):
            if set(value) == {op}:
                items = value[op]
                if not isinstance(items, list) or not items:
                    raise ContractError(f"{op} requires a nonempty array.")
                return Group(op, tuple(parse(item, depth + 1) for item in items))
        obj = require_keys(value, {"field", "op"}, {"value", "unit"})
        return test_predicate(
            fields, obj["field"], obj["op"], obj.get("value", ABSENT), obj.get("unit", ABSENT)
        )

    return parse(raw, 0)


TOKEN = re.compile(
    fr'\s*(?:(?P<string>"(?:[^"\\]|\\.)*")|(?P<number>{NUMBER})|'
    r"(?P<word>[A-Za-z_][A-Za-z0-9_]*)|(?P<symbol>>=|<=|==|!=|[<>(),]))",
    re.ASCII,
)
ROUTE_PATTERN = ROUTE_NAME.pattern.removesuffix(r"\Z")
SOURCE_RULE = re.compile(fr"IF\s+(.+)\s+THEN\s+({ROUTE_PATTERN})\.?", re.I | re.S)
SOURCE_FALLBACK = re.compile(fr"(?:OTHERWISE(?:\s+THEN)?|ELSE)\s+({ROUTE_PATTERN})\.?", re.I)


class _SourceParser:
    def __init__(self, text: str, fields: Fields):
        self.tokens = []
        self.fields = fields
        self.position = 0
        index = 0
        while index < len(text):
            match = TOKEN.match(text, index)
            if not match:
                raise ContractError(f"Unrecognized source expression near character {index + 1}.")
            self.tokens.append((match.lastgroup, match[match.lastgroup]))
            if len(self.tokens) > MAX_ATOMS:
                raise ContractError("Source expression exceeds the token limit.")
            index = match.end()

    def peek(self) -> tuple[str, str]:
        if self.position == len(self.tokens):
            return "", ""
        return self.tokens[self.position]

    def take(self) -> tuple[str, str]:
        token = self.peek()
        if not token[0]:
            raise ContractError("Source expression ended early.")
        self.position += 1
        return token

    def accept(self, text: str) -> bool:
        kind, value = self.peek()
        if kind in ("word", "symbol") and value.lower() == text:
            self.take()
            return True
        return False

    def expect(self, text: str) -> None:
        if not self.accept(text):
            raise ContractError(f"Expected {text!r} in the source expression.")

    def parse(self, depth: int = 0) -> Predicate:
        if depth > MAX_DEPTH:
            raise ContractError("Source expression exceeds the nesting limit.")
        terms = [self._conjunction(depth)]
        while self.accept("or"):
            terms.append(self._conjunction(depth))
        return terms[0] if len(terms) == 1 else Group("any", tuple(terms))

    def _conjunction(self, depth: int) -> Predicate:
        items = [self._term(depth)]
        while self.accept("and"):
            items.append(self._term(depth))
        return items[0] if len(items) == 1 else Group("all", tuple(items))

    def _term(self, depth: int) -> Predicate:
        if depth > MAX_DEPTH:
            raise ContractError("Source expression exceeds the nesting limit.")
        if self.accept("not"):
            return Group("not", (self._term(depth + 1),))
        if self.accept("("):
            expr = self.parse(depth + 1)
            self.expect(")")
            return expr
        kind, first = self.take()
        if kind != "word":
            raise ContractError("Expected a field name or Boolean literal.")
        keyword = first.lower()
        if keyword in ("true", "false") and first not in self.fields:
            return keyword == "true"
        if keyword in ("missing", "present") and self.accept("("):
            field_kind, name = self.take()
            if field_kind != "word":
                raise ContractError("Expected a field name in the missing/present test.")
            self.expect(")")
            return test_predicate(self.fields, name, keyword)
        if self.accept("is"):
            negate = self.accept("not")
            if self.accept("missing") or self.accept("null"):
                return test_predicate(self.fields, first, "present" if negate else "missing")
            if self.accept("present"):
                return test_predicate(self.fields, first, "missing" if negate else "present")
            raise ContractError("IS requires MISSING, PRESENT, or NULL.")
        _, op = self.take()
        literal_kind, literal = self.take()
        unit = ABSENT
        if literal_kind == "number":
            value = exact_number(literal, from_text=True)
            if self.peek()[0] == "word" and self.peek()[1].lower() not in ("and", "or"):
                _, unit = self.take()
        elif literal_kind == "string":
            try:
                value = json.loads(literal)
            except ValueError as error:
                raise ContractError("Invalid quoted string in the source expression.") from error
        elif literal_kind == "word" and literal.lower() in ("true", "false"):
            value = literal.lower() == "true"
        else:
            raise ContractError("Expected a number, quoted string, or Boolean literal.")
        return test_predicate(self.fields, first, op, value, unit)


def parse_source(text: Any, fields: Fields) -> tuple[Predicate, str]:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ContractError("Source rule must contain a supported, size-bounded sentence.")
    text = text.strip()
    fallback = SOURCE_FALLBACK.fullmatch(text)
    if fallback:
        return True, require_route(fallback[1])
    rule = SOURCE_RULE.fullmatch(text)
    if not rule:
        raise ContractError("Expected IF <condition> THEN <route> or OTHERWISE THEN <route>.")
    parser = _SourceParser(rule[1].strip(), fields)
    expr = parser.parse()
    if parser.peek()[0]:
        raise ContractError("Unsupported text follows the source condition.")
    return expr, require_route(rule[2])


def compare(op: str, left: Any, right: Any) -> bool:
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    return left >= right


def holds(predicate: Predicate, ticket: Ticket) -> bool:
    if isinstance(predicate, bool):
        return predicate
    if isinstance(predicate, Group):
        if predicate.op == "all":
            return all(holds(term, ticket) for term in predicate.terms)
        if predicate.op == "any":
            return any(holds(term, ticket) for term in predicate.terms)
        return not holds(predicate.terms[0], ticket)
    value = ticket.get(predicate.field, ABSENT)
    missing = value is ABSENT or value is None
    if predicate.op == "missing":
        return missing
    if predicate.op == "present":
        return not missing
    return not missing and compare(predicate.op, value, predicate.value)


def tests_in(predicate: Predicate | None) -> Iterable[Test]:
    if isinstance(predicate, Test):
        yield predicate
    elif isinstance(predicate, Group):
        for term in predicate.terms:
            yield from tests_in(term)


def partition(
    fields: Fields, tests: Iterable[Test], *, omit_boundaries: bool = False
) -> dict[str, tuple]:
    literals: dict[str, set] = {field: set() for field in fields}
    for test in tests:
        if test.op not in ("missing", "present"):
            literals[test.field].add(test.value)
    cells = {}
    for name, field in sorted(fields.items()):
        points = sorted(literals[name])
        if field.kind == "number":
            if points:
                values = [points[0] - 1]
                for index, point in enumerate(points):
                    if not omit_boundaries:
                        values.append(point)
                    if index + 1 < len(points):
                        values.append((point + points[index + 1]) / 2)
                values.append(points[-1] + 1)
            else:
                values = [Fraction(0)]
        elif field.kind == "string":
            other = "__other__"
            while other in literals[name]:
                other += "_"
            values = [*points, other]
        else:
            values = [False, True]
        if field.nullable:
            values.extend([None, ABSENT])
        cells[name] = tuple(values)
    count = prod(map(len, cells.values()))
    if count > MAX_CELLS:
        raise DomainLimit(count, MAX_CELLS)
    return cells


def cell_tickets(cells: dict[str, tuple]) -> Iterable[Ticket]:
    for values in itertools.product(*cells.values()):
        yield dict(zip(cells, values))


def encode_ticket(ticket: Ticket) -> dict[str, Any]:
    result = {}
    for field, value in ticket.items():
        if value is ABSENT:
            continue
        if isinstance(value, Fraction):
            try:
                scalar = value.numerator if value.denominator == 1 else float(value)
                if exact_number(scalar) == value:
                    result[field] = scalar
                    continue
            except (ContractError, OverflowError):
                pass
            value = {"numerator": value.numerator, "denominator": value.denominator}
        result[field] = value
    return result


def decode_ticket(raw: Any, fields: Fields) -> Ticket:
    if not isinstance(raw, dict) or not raw.keys() <= fields.keys():
        raise ContractError("Ticket must be an object containing only declared fields.")
    ticket = {}
    for name, field in fields.items():
        value = raw.get(name, ABSENT)
        if value is None or value is ABSENT:
            if not field.nullable:
                raise ContractError(f"Ticket field {name!r} is required and non-nullable.")
        elif field.kind == "number":
            if isinstance(value, dict):
                fraction = require_keys(value, {"numerator", "denominator"}, set())
                top, bottom = fraction["numerator"], fraction["denominator"]
                if type(top) is not int or type(bottom) is not int or bottom <= 0:
                    raise ContractError(f"Ticket field {name!r} requires an exact positive denominator.")
                value = Fraction(top, bottom)
            else:
                value = exact_number(value)
        elif (field.kind == "string" and type(value) is not str) or (
            field.kind == "boolean" and type(value) is not bool
        ):
            raise ContractError(f"Ticket field {name!r} has the wrong type.")
        ticket[name] = value
    return ticket
