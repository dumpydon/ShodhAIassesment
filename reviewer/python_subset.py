"""Parse and interpret a bounded, side-effect-free subset of linked Python."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .logic import ABSENT, MAX_DEPTH, MAX_TEXT, ContractError, Fields, Test, Ticket, compare, exact_number


MAX_AST_NODES = 320
PY_COMPARE = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Is: "is",
    ast.IsNot: "is not",
}


class SnippetSyntaxError(ContractError):
    """The linked source failed Python syntax parsing."""


class SnippetRuntimeError(ValueError):
    """A modeled input does not produce a Boolean decision."""


@dataclass(frozen=True)
class Lookup:
    field: str
    bracket: bool
    default: Any = None


@dataclass(frozen=True)
class Comparison:
    op: str
    left: Any
    right: Any


@dataclass(frozen=True)
class Boolean:
    op: str
    operands: tuple


@dataclass(frozen=True)
class Return:
    value: Any


@dataclass(frozen=True)
class If:
    guard: Any
    yes: tuple[Return | If, ...]
    no: tuple[Return | If, ...]


@dataclass(frozen=True)
class Program:
    body: tuple[Return | If, ...]
    tests: tuple[Test, ...]

    def decide(self, ticket: Ticket) -> bool:
        result = _run(self.body, ticket)
        if type(result) is not bool:
            raise SnippetRuntimeError("A linked decision must return a Boolean on this input.")
        return result


def parse_python(source: Any) -> ast.Module:
    if not isinstance(source, str) or not source.strip() or len(source) > MAX_TEXT:
        raise ContractError("Linked Python source is missing, empty, or exceeds the size limit.")
    try:
        tree = ast.parse(source, filename="<linked snippet>", mode="exec")
    except SyntaxError as error:
        raise SnippetSyntaxError(f"Python syntax error at line {error.lineno}: {error.msg}.") from error
    except (ValueError, RecursionError) as error:
        raise ContractError("Linked Python exceeds the parser limits.") from error
    if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
        raise ContractError("Linked Python exceeds the AST size limit.")
    return tree


def _without_docstring(statements: list[ast.stmt]) -> list[ast.stmt]:
    if statements and isinstance(statements[0], ast.Expr):
        value = statements[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return statements[1:]
    return statements


class _Inspector:
    def __init__(self, fields: Fields):
        self.fields = fields
        self.tests: list[Test] = []

    def constant(self, expr: ast.expr) -> Any:
        sign = 1
        if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, (ast.USub, ast.UAdd)):
            sign = -1 if isinstance(expr.op, ast.USub) else 1
            expr = expr.operand
            if not isinstance(expr, ast.Constant) or type(expr.value) not in (int, float):
                raise ContractError("Unary signs may only be used on numeric literals.")
        if not isinstance(expr, ast.Constant):
            raise ContractError("Expected a scalar literal or a ticket field lookup.")
        value = expr.value
        if type(value) in (int, float):
            exact_number(value)
            # Ticket numbers are exact rationals in the declared base unit. A Python
            # float literal retains its binary value when compared with a rational.
            return sign * Fraction(value)
        if value is None or type(value) in (bool, str):
            return value
        raise ContractError("Unsupported Python literal.")

    def operand(self, x: ast.expr) -> Any:
        if isinstance(x, ast.Subscript):
            if not isinstance(x.value, ast.Name) or x.value.id != "ticket":
                raise ContractError("Subscripts may only look up fields on ticket.")
            return self.lookup(x.slice, bracket=True)
        if isinstance(x, ast.Call):
            if (
                not isinstance(x.func, ast.Attribute)
                or not isinstance(x.func.value, ast.Name)
                or x.func.value.id != "ticket"
                or x.func.attr != "get"
                or x.keywords
                or len(x.args) not in (1, 2)
            ):
                raise ContractError("Only ticket.get(field[, scalar_default]) calls are supported.")
            default = self.constant(x.args[1]) if len(x.args) == 2 else None
            return self.lookup(x.args[0], bracket=False, default=default)
        return self.constant(x)

    def lookup(self, key: ast.expr, *, bracket: bool, default: Any = None) -> Lookup:
        field = self.constant(key)
        if not isinstance(field, str) or field not in self.fields:
            raise ContractError(f"Python references an undeclared or nonliteral field {field!r}.")
        return Lookup(field, bracket, default)

    def comparison(self, op: str, left: Any, right: Any) -> Comparison:
        if isinstance(left, Lookup) and isinstance(right, Lookup):
            raise ContractError("Python cannot compare two ticket fields in this subset.")
        lookup = left if isinstance(left, Lookup) else right if isinstance(right, Lookup) else None
        literal = right if isinstance(left, Lookup) else left
        if op in ("is", "is not"):
            if not (literal is None or type(literal) is bool):
                raise ContractError("Python identity tests only support None, True, and False.")
        elif lookup is not None:
            kind = self.fields[lookup.field].kind
            if op not in ("==", "!=") and kind != "number":
                raise ContractError("Python ordered comparisons require a numeric ticket field.")
            if kind == "number" and isinstance(literal, (Fraction, bool)):
                self.tests.append(Test(lookup.field, op, Fraction(literal)))
            if kind == "string" and isinstance(literal, str):
                self.tests.append(Test(lookup.field, op, literal))
        return Comparison(op, left, right)

    def expression(self, x: ast.expr, depth: int = 0) -> Any:
        if depth > MAX_DEPTH:
            raise ContractError("Python expression exceeds the nesting limit.")
        if isinstance(x, ast.BoolOp):
            op = "and" if isinstance(x.op, ast.And) else "or"
            return Boolean(op, tuple(self.expression(item, depth + 1) for item in x.values))
        if isinstance(x, ast.UnaryOp) and isinstance(x.op, ast.Not):
            return Boolean("not", (self.expression(x.operand, depth + 1),))
        if isinstance(x, ast.Compare):
            operands = [self.operand(item) for item in [x.left, *x.comparators]]
            parts = []
            for i, operator in enumerate(x.ops):
                op = PY_COMPARE.get(type(operator))
                if op is None:
                    raise ContractError("Unsupported Python comparison operator.")
                parts.append(self.comparison(op, operands[i], operands[i + 1]))
            return parts[0] if len(parts) == 1 else Boolean("and", tuple(parts))
        operand = self.operand(x)
        if isinstance(operand, Lookup):
            kind = self.fields[operand.field].kind
            if kind == "number":
                self.tests.append(Test(operand.field, "==", Fraction(0)))
            elif kind == "string":
                self.tests.append(Test(operand.field, "==", ""))
            return operand
        if type(operand) is not bool:
            raise ContractError("Python decision expressions require a Boolean or field lookup.")
        return operand

    def block(self, source: list[ast.stmt], depth: int = 0) -> tuple[Return | If, ...]:
        if depth > MAX_DEPTH:
            raise ContractError("Python statements exceed the nesting limit.")
        statements = []
        for statement in source:
            if isinstance(statement, ast.Return) and statement.value is not None:
                statements.append(Return(self.expression(statement.value)))
            elif isinstance(statement, ast.If):
                statements.append(
                    If(
                        self.expression(statement.test),
                        self.block(statement.body, depth + 1),
                        self.block(statement.orelse, depth + 1),
                    )
                )
            else:
                raise ContractError(
                    f"Unsupported Python statement {type(statement).__name__} at line {statement.lineno}."
                )
        return tuple(statements)


def inspect_python(tree: ast.Module, fields: Fields) -> Program:
    body = _without_docstring(tree.body)
    if len(body) != 1 or not isinstance(body[0], ast.FunctionDef):
        raise ContractError("Expected exactly one undecorated def decide(ticket) function.")
    fn = body[0]
    args = fn.args
    if (
        fn.name != "decide"
        or fn.decorator_list
        or fn.returns is not None
        or getattr(fn, "type_params", [])
        or args.posonlyargs
        or len(args.args) != 1
        or args.args[0].arg != "ticket"
        or args.args[0].annotation is not None
        or args.kwonlyargs
        or args.defaults
        or args.vararg is not None
        or args.kwarg is not None
    ):
        raise ContractError("Expected exactly one undecorated def decide(ticket) function.")
    inspector = _Inspector(fields)
    return Program(inspector.block(_without_docstring(fn.body)), tuple(inspector.tests))


def _value(expr: Any, ticket: Ticket) -> Any:
    if isinstance(expr, Lookup):
        value = ticket.get(expr.field, ABSENT)
        if value is not ABSENT:
            return value
        if expr.bracket:
            raise SnippetRuntimeError(f"Bracket lookup raises KeyError for absent field {expr.field!r}.")
        return expr.default
    if isinstance(expr, Comparison):
        left = _value(expr.left, ticket)
        right = _value(expr.right, ticket)
        if expr.op == "is":
            return left is right
        if expr.op == "is not":
            return left is not right
        try:
            return compare(expr.op, left, right)
        except TypeError as error:
            raise SnippetRuntimeError(f"Python comparison raises TypeError: {error}.") from error
    if isinstance(expr, Boolean):
        if expr.op == "not":
            return not _value(expr.operands[0], ticket)
        for operand in expr.operands:
            value = _value(operand, ticket)
            if expr.op == "and" and not value or expr.op == "or" and value:
                return value
        return value
    return expr


def _run(body: tuple[Return | If, ...], ticket: Ticket) -> Any:
    for statement in body:
        if isinstance(statement, Return):
            return _value(statement.value, ticket)
        branch = statement.yes if _value(statement.guard, ticket) else statement.no
        value = _run(branch, ticket)
        if value is not ABSENT:
            return value
    return ABSENT
