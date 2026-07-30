from __future__ import annotations

import re
from typing import Any

_TOKEN_SPEC = [
    ("LPAREN", r"\("),
    ("RPAREN", r"\)"),
    ("COMMA", r","),
    ("NEQ", r"!="),
    ("EQ", r"="),
    ("QUOTED", r'"[^"]*"'),
    ("AND", r"(?i:AND)\b"),
    ("OR", r"(?i:OR)\b"),
    ("IN", r"(?i:IN)\b"),
    ("NOT", r"(?i:NOT)\b"),
    ("WORD", r'[^\s(),=!"]+'),
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
    optional parentheses. `field IN (a, b, ...)` / `field NOT IN (...)` are
    supported too, desugared into an OR (or NOT-of-OR) of the same `eq`
    leaves a chain of `field = a OR field = b OR ...` would produce --
    evaluate_predicate never needs to know IN existed. Anything outside
    this subset raises UnsupportedJqlError rather than being guessed at.
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
        field_name = field_token[1].strip().lower()

        op_token = self._peek()
        if op_token is None:
            raise UnsupportedJqlError(f"unsupported operator after field {field_token[1]!r}")

        negate = False
        if op_token[0] == "NOT":
            self._advance()
            negate = True
            op_token = self._peek()
            if op_token is None or op_token[0] != "IN":
                raise UnsupportedJqlError(f"expected IN after NOT for field {field_token[1]!r}")

        if op_token[0] == "IN":
            self._advance()
            values = self._parse_value_list(field_token[1])
            leaves = [self._make_leaf(field_name, field_token[1], value) for value in values]
            node = leaves[0] if len(leaves) == 1 else {"op": "or", "clauses": leaves}
            return {"op": "not", "clause": node} if negate else node

        if op_token[0] not in ("EQ", "NEQ"):
            raise UnsupportedJqlError(f"unsupported operator after field {field_token[1]!r}")
        self._advance()
        value_token = self._peek()
        if value_token is None or value_token[0] not in _VALUE_TOKENS:
            raise UnsupportedJqlError(f"expected a value after {field_token[1]!r} {op_token[1]}")
        self._advance()

        leaf = self._make_leaf(field_name, field_token[1], value_token[1])
        return {"op": "not", "clause": leaf} if op_token[0] == "NEQ" else leaf

    def _parse_value_list(self, field_text: str) -> list[str]:
        open_paren = self._peek()
        if open_paren is None or open_paren[0] != "LPAREN":
            raise UnsupportedJqlError(f"expected '(' after IN for field {field_text!r}")
        self._advance()
        values = []
        while True:
            value_token = self._peek()
            if value_token is None or value_token[0] not in _VALUE_TOKENS:
                raise UnsupportedJqlError(f"expected a value in {field_text!r} IN (...) list")
            self._advance()
            values.append(value_token[1])
            next_token = self._peek()
            if next_token is not None and next_token[0] == "COMMA":
                self._advance()
                continue
            break
        closing = self._peek()
        if closing is None or closing[0] != "RPAREN":
            raise UnsupportedJqlError(f"unbalanced parentheses in {field_text!r} IN (...) list")
        self._advance()
        return values

    def _make_leaf(self, field_name: str, field_text: str, value: str) -> dict[str, Any]:
        if field_name == "project":
            return {"op": "eq", "field": "project", "value": value}
        if field_name == "labels":
            return {"op": "eq", "field": "labels", "value": value}
        if field_name in self._component_field_names:
            return {"op": "eq", "field": "component", "value": value}
        raise UnsupportedJqlError(f"unsupported field: {field_text}")


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


def predicate_scopes_project(predicate: dict[str, Any]) -> bool:
    """True if this predicate tree already constrains "project" somewhere
    -- an explicit OR of several `project = X` clauses (a genuine
    multi-project board) counts as scoped, same as a single one."""
    op = predicate.get("op")
    if op == "eq":
        return predicate.get("field") == "project"
    if op == "not":
        return predicate_scopes_project(predicate["clause"])
    if op in ("and", "or"):
        return any(predicate_scopes_project(clause) for clause in predicate["clauses"])
    return False


def scope_predicate_to_project(predicate: dict[str, Any], project: str) -> dict[str, Any]:
    """A Jira board only ever shows issues from its own project, even when
    its saved filter's JQL doesn't literally say so (a board's filter is
    already implicitly scoped by Jira itself) -- if the compiled predicate
    doesn't already reference "project" anywhere, AND in that implicit
    constraint so evaluate_predicate enforces it too. A no-op if the
    filter already scopes by project on its own (e.g. a genuine
    multi-project board's own "project = A OR project = B" clause)."""
    if predicate_scopes_project(predicate):
        return predicate
    return {"op": "and", "clauses": [{"op": "eq", "field": "project", "value": project}, predicate]}


def evaluate_predicate(
    predicate: dict[str, Any], *, component: str | None, labels: list[str], project: str | None = None
) -> bool:
    op = predicate.get("op")
    if op == "true":
        return True
    if op == "and":
        return all(
            evaluate_predicate(clause, component=component, labels=labels, project=project)
            for clause in predicate["clauses"]
        )
    if op == "or":
        return any(
            evaluate_predicate(clause, component=component, labels=labels, project=project)
            for clause in predicate["clauses"]
        )
    if op == "not":
        return not evaluate_predicate(predicate["clause"], component=component, labels=labels, project=project)
    if op == "eq":
        field = predicate["field"]
        value = str(predicate["value"]).lower()
        if field == "component":
            return bool(component) and component.lower() == value
        if field == "labels":
            return any(label.lower() == value for label in labels)
        if field == "project":
            return bool(project) and project.lower() == value
        raise ValueError(f"unknown predicate field: {field!r}")
    raise ValueError(f"unknown predicate node: {predicate!r}")
