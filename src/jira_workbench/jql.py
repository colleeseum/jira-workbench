from __future__ import annotations

import re
from typing import Any

_TOKEN_SPEC = [
    ("LPAREN", r"\("),
    ("RPAREN", r"\)"),
    ("NEQ", r"!="),
    ("EQ", r"="),
    ("QUOTED", r'"[^"]*"'),
    ("AND", r"(?i:AND)\b"),
    ("OR", r"(?i:OR)\b"),
    ("WORD", r'[^\s()=!"]+'),
]
_TOKEN_RE = re.compile("|".join(f"(?P<{name}>{pattern})" for name, pattern in _TOKEN_SPEC))
_ORDER_BY_RE = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)

_VALUE_TOKENS = ("WORD", "QUOTED")


class UnsupportedJqlError(Exception):
    pass


def _strip_order_by(jql: str) -> str:
    match = _ORDER_BY_RE.search(jql)
    return jql[: match.start()] if match else jql


def _tokenize(expr: str) -> list[tuple[str, str]]:
    tokens = []
    for match in _TOKEN_RE.finditer(expr):
        kind = match.lastgroup
        value = match.group()
        if kind == "QUOTED":
            value = value[1:-1]
        tokens.append((kind, value))
    return tokens


class _Parser:
    """Recursive-descent parser for a narrow JQL subset: `field op value`
    clauses combined with AND/OR (AND binds tighter, matching JQL), with
    optional parentheses. Anything outside this subset raises
    UnsupportedJqlError rather than being guessed at.
    """

    def __init__(self, tokens: list[tuple[str, str]], *, component_field_names: list[str]) -> None:
        self._tokens = tokens
        self._pos = 0
        self._component_field_names = {name.strip().lower() for name in component_field_names}

    def parse(self) -> dict[str, Any]:
        if not self._tokens:
            raise UnsupportedJqlError("empty filter")
        node = self._parse_or()
        if self._pos != len(self._tokens):
            raise UnsupportedJqlError(f"unexpected token {self._peek()[1]!r}")
        return node

    def _peek(self) -> tuple[str, str] | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _advance(self) -> tuple[str, str]:
        token = self._tokens[self._pos]
        self._pos += 1
        return token

    def _parse_or(self) -> dict[str, Any]:
        clauses = [self._parse_and()]
        while self._peek() is not None and self._peek()[0] == "OR":
            self._advance()
            clauses.append(self._parse_and())
        return clauses[0] if len(clauses) == 1 else {"op": "or", "clauses": clauses}

    def _parse_and(self) -> dict[str, Any]:
        clauses = [self._parse_term()]
        while self._peek() is not None and self._peek()[0] == "AND":
            self._advance()
            clauses.append(self._parse_term())
        return clauses[0] if len(clauses) == 1 else {"op": "and", "clauses": clauses}

    def _parse_term(self) -> dict[str, Any]:
        token = self._peek()
        if token is None:
            raise UnsupportedJqlError("unexpected end of filter")
        if token[0] == "LPAREN":
            self._advance()
            node = self._parse_or()
            closing = self._peek()
            if closing is None or closing[0] != "RPAREN":
                raise UnsupportedJqlError("unbalanced parentheses")
            self._advance()
            return node
        return self._parse_clause()

    def _parse_clause(self) -> dict[str, Any]:
        field_token = self._advance()
        if field_token[0] not in _VALUE_TOKENS:
            raise UnsupportedJqlError(f"expected a field name, got {field_token[1]!r}")
        op_token = self._peek()
        if op_token is None or op_token[0] not in ("EQ", "NEQ"):
            raise UnsupportedJqlError(f"unsupported operator after field {field_token[1]!r}")
        self._advance()
        value_token = self._peek()
        if value_token is None or value_token[0] not in _VALUE_TOKENS:
            raise UnsupportedJqlError(f"expected a value after {field_token[1]!r} {op_token[1]}")
        self._advance()

        field_name = field_token[1].strip().lower()
        negate = op_token[0] == "NEQ"
        if field_name == "project":
            leaf: dict[str, Any] = {"op": "true"}
        elif field_name == "labels":
            leaf = {"op": "eq", "field": "labels", "value": value_token[1]}
        elif field_name in self._component_field_names:
            leaf = {"op": "eq", "field": "component", "value": value_token[1]}
        else:
            raise UnsupportedJqlError(f"unsupported field: {field_token[1]}")
        return {"op": "not", "clause": leaf} if negate else leaf


def compile_jql(jql: str, *, component_field_names: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    """Compile a narrow subset of JQL into a locally-evaluable predicate tree.

    component_field_names should be every valid JQL clause name for the
    configured component field (Jira's own field name and its JQL clause
    name can differ -- e.g. a custom field's plain name might be
    "Components" while the name actually usable in JQL is
    "Components[Dropdown]", to disambiguate from the native field of the
    same name), so a filter can be matched regardless of which alias it uses.

    Returns (predicate, None) on success, or (None, reason) if the filter
    uses anything outside the supported subset -- callers should treat
    that board as unsupported rather than silently misevaluating it.
    """
    expr = _strip_order_by(jql).strip()
    if not expr:
        return {"op": "true"}, None
    tokens = _tokenize(expr)
    try:
        tree = _Parser(tokens, component_field_names=component_field_names).parse()
    except UnsupportedJqlError as exc:
        return None, str(exc)
    return tree, None


def evaluate_predicate(predicate: dict[str, Any], *, component: str | None, labels: list[str]) -> bool:
    op = predicate.get("op")
    if op == "true":
        return True
    if op == "and":
        return all(evaluate_predicate(clause, component=component, labels=labels) for clause in predicate["clauses"])
    if op == "or":
        return any(evaluate_predicate(clause, component=component, labels=labels) for clause in predicate["clauses"])
    if op == "not":
        return not evaluate_predicate(predicate["clause"], component=component, labels=labels)
    if op == "eq":
        field = predicate["field"]
        value = str(predicate["value"]).lower()
        if field == "component":
            return bool(component) and component.lower() == value
        if field == "labels":
            return any(label.lower() == value for label in labels)
        raise ValueError(f"unknown predicate field: {field!r}")
    raise ValueError(f"unknown predicate node: {predicate!r}")
