from __future__ import annotations

import pytest

from jira_workbench.jql import (
    compile_jql,
    evaluate_predicate,
)

COMPONENT_FIELD_NAME = "Components[Dropdown]"


def test_compile_whole_project_filter_scopes_to_that_project() -> None:
    # A board's own JQL filter usually starts with "project = X" -- this
    # must actually be evaluated (not treated as always-true), otherwise an
    # issue from a *different* synced project sharing the same jira_dir can
    # incorrectly show up as a member of this board too, the moment more
    # than one project is being synced into the same store.
    predicate, reason = compile_jql("project = SAT ORDER BY Rank ASC", component_field_names=[COMPONENT_FIELD_NAME])

    assert reason is None
    assert predicate == {"op": "eq", "field": "project", "value": "SAT"}
    assert evaluate_predicate(predicate, component=None, labels=[], project="SAT") is True
    assert evaluate_predicate(predicate, component=None, labels=[], project="OTHERPROJ") is False
    assert evaluate_predicate(predicate, component=None, labels=[], project=None) is False


def test_compile_compound_board_filter_matches_real_jql() -> None:
    jql = (
        'project = SAT AND ( "Components[Dropdown]" = helm-chart OR "Components[Dropdown]" = puppy )  '
        "OR  labels=k8s_sprints \n"
    )

    predicate, reason = compile_jql(jql, component_field_names=[COMPONENT_FIELD_NAME])

    assert reason is None
    # matches via component -- but only within the board's own project
    assert evaluate_predicate(predicate, component="helm-chart", labels=[], project="SAT") is True
    assert evaluate_predicate(predicate, component="puppy", labels=[], project="SAT") is True
    # case-insensitive
    assert evaluate_predicate(predicate, component="Helm-Chart", labels=[], project="SAT") is True
    # same component, but a different project -- the "project = SAT" clause excludes it
    assert evaluate_predicate(predicate, component="helm-chart", labels=[], project="OTHERPROJ") is False
    # matches via label, regardless of component or project (the label arm
    # of the OR has no project clause of its own)
    assert evaluate_predicate(predicate, component="unrelated", labels=["k8s_sprints"], project=None) is True
    # matches neither
    assert evaluate_predicate(predicate, component="unrelated", labels=["other"], project="SAT") is False
    assert evaluate_predicate(predicate, component=None, labels=[], project="SAT") is False


def test_compile_and_binds_tighter_than_or() -> None:
    # component=a AND labels=x  OR  labels=y
    # should be (component=a AND labels=x) OR labels=y, not component=a AND (labels=x OR labels=y)
    jql = f'"{COMPONENT_FIELD_NAME}" = a AND labels = x OR labels = y'

    predicate, reason = compile_jql(jql, component_field_names=[COMPONENT_FIELD_NAME])

    assert reason is None
    assert evaluate_predicate(predicate, component=None, labels=["y"]) is True
    assert evaluate_predicate(predicate, component="a", labels=["x"]) is True
    assert evaluate_predicate(predicate, component="a", labels=[]) is False


def test_compile_negation() -> None:
    jql = f'"{COMPONENT_FIELD_NAME}" != a'

    predicate, reason = compile_jql(jql, component_field_names=[COMPONENT_FIELD_NAME])

    assert reason is None
    assert evaluate_predicate(predicate, component="a", labels=[]) is False
    assert evaluate_predicate(predicate, component="b", labels=[]) is True


@pytest.mark.parametrize(
    "jql",
    [
        "labels != blocked",
        "labels NOT IN (blocked, wip)",
        f'"{COMPONENT_FIELD_NAME}" != foo',
        f'"{COMPONENT_FIELD_NAME}" NOT IN (foo, bar)',
        "project != SAT",
    ],
)
def test_negation_does_not_match_an_empty_field(jql: str) -> None:
    # Jira's != and NOT IN never match an empty field (that needs an explicit IS EMPTY), so an
    # unlabelled or component-less item must not land on a board whose filter excludes a value.
    predicate, reason = compile_jql(jql, component_field_names=[COMPONENT_FIELD_NAME])

    assert reason is None
    assert evaluate_predicate(predicate, component=None, labels=[], project=None) is False


@pytest.mark.parametrize(
    ("jql", "component", "labels", "expected"),
    [
        ("labels != blocked", None, ["ready"], True),
        ("labels != blocked", None, ["ready", "blocked"], False),
        ("labels NOT IN (blocked, wip)", None, ["ready"], True),
        (f'"{COMPONENT_FIELD_NAME}" NOT IN (foo, bar)', "baz", [], True),
        (f'"{COMPONENT_FIELD_NAME}" NOT IN (foo, bar)', "foo", [], False),
        # Null semantics apply per negated clause, not to the whole expression.
        (f'labels != blocked OR "{COMPONENT_FIELD_NAME}" = foo', "foo", [], True),
    ],
)
def test_negation_matches_a_non_empty_field_without_the_value(
    jql: str, component: str | None, labels: list[str], expected: bool
) -> None:
    predicate, _ = compile_jql(jql, component_field_names=[COMPONENT_FIELD_NAME])

    assert evaluate_predicate(predicate, component=component, labels=labels) is expected


def test_negation_null_semantics_apply_to_a_cached_predicate() -> None:
    # Compiled predicates are cached in meta/<project>/boards.json; the fix must hold for them too.
    cached = {"op": "not", "clause": {"op": "eq", "field": "labels", "value": "blocked"}}

    assert evaluate_predicate(cached, component=None, labels=[]) is False


def test_compile_parentheses_override_precedence() -> None:
    # (labels=x OR labels=y) AND component=a
    jql = f'( labels = x OR labels = y ) AND "{COMPONENT_FIELD_NAME}" = a'

    predicate, reason = compile_jql(jql, component_field_names=[COMPONENT_FIELD_NAME])

    assert reason is None
    assert evaluate_predicate(predicate, component="a", labels=["x"]) is True
    assert evaluate_predicate(predicate, component="a", labels=["y"]) is True
    assert evaluate_predicate(predicate, component="a", labels=["z"]) is False
    assert evaluate_predicate(predicate, component="other", labels=["x"]) is False


def test_compile_empty_filter_is_always_true() -> None:
    predicate, reason = compile_jql("   ", component_field_names=[COMPONENT_FIELD_NAME])

    assert reason is None
    assert predicate == {"op": "true"}


@pytest.mark.parametrize(
    "jql",
    [
        "assignee = currentUser()",
        "created >= -7d",
        'status IN ("To Do", "In Progress")',
        '"Components[Dropdown]" = a AND (',
        "project =",
    ],
)
def test_compile_rejects_unsupported_constructs(jql: str) -> None:
    predicate, reason = compile_jql(jql, component_field_names=[COMPONENT_FIELD_NAME])

    assert predicate is None
    assert reason


def test_compile_project_in_single_value() -> None:
    predicate, reason = compile_jql("project IN (SAT)", component_field_names=[COMPONENT_FIELD_NAME])

    assert reason is None
    assert predicate == {"op": "eq", "field": "project", "value": "SAT"}
    assert evaluate_predicate(predicate, component=None, labels=[], project="SAT") is True
    assert evaluate_predicate(predicate, component=None, labels=[], project="PLAT") is False


def test_compile_project_in_multiple_values_desugars_to_or() -> None:
    # project IN (SAT, PLAT) is equivalent to (project = SAT OR project = PLAT)
    predicate, reason = compile_jql("project IN (SAT, PLAT)", component_field_names=[COMPONENT_FIELD_NAME])

    assert reason is None
    assert predicate == {
        "op": "or",
        "clauses": [
            {"op": "eq", "field": "project", "value": "SAT"},
            {"op": "eq", "field": "project", "value": "PLAT"},
        ],
    }
    assert evaluate_predicate(predicate, component=None, labels=[], project="SAT") is True
    assert evaluate_predicate(predicate, component=None, labels=[], project="PLAT") is True
    assert evaluate_predicate(predicate, component=None, labels=[], project="OTHERPROJ") is False


def test_compile_project_not_in() -> None:
    predicate, reason = compile_jql("project NOT IN (SAT, PLAT)", component_field_names=[COMPONENT_FIELD_NAME])

    assert reason is None
    assert predicate == {
        "op": "not",
        "clause": {
            "op": "or",
            "clauses": [
                {"op": "eq", "field": "project", "value": "SAT"},
                {"op": "eq", "field": "project", "value": "PLAT"},
            ],
        },
    }
    assert evaluate_predicate(predicate, component=None, labels=[], project="SAT") is False
    assert evaluate_predicate(predicate, component=None, labels=[], project="OTHERPROJ") is True


def test_compile_in_scopes_project_like_a_plain_eq_clause() -> None:
    # project IN (...) constrains the project exactly like OR-ed "project = X" clauses.
    predicate, reason = compile_jql("project IN (SAT, PLAT)", component_field_names=[COMPONENT_FIELD_NAME])
    assert reason is None
    assert evaluate_predicate(predicate, component=None, labels=[], project="SAT") is True
    assert evaluate_predicate(predicate, component=None, labels=[], project="PLAT") is True
    assert evaluate_predicate(predicate, component=None, labels=[], project="OTHER") is False


def test_compile_real_ps_tools_board_jql_with_project_in() -> None:
    # The real motivating case: a board filter combining "project IN (SAT)"
    # with an unrelated OR'd clause -- previously rejected outright because
    # IN was unsupported at all.
    jql = (
        'project IN (SAT) AND (project = SAT AND ( "Components[Dropdown]" = helm-chart '
        'OR "Components[Dropdown]" = puppy )  OR  labels=k8s_sprints )'
    )

    predicate, reason = compile_jql(jql, component_field_names=[COMPONENT_FIELD_NAME])

    assert reason is None
    assert evaluate_predicate(predicate, component="helm-chart", labels=[], project="SAT") is True
    assert evaluate_predicate(predicate, component="puppy", labels=[], project="SAT") is True
    assert evaluate_predicate(predicate, component="unrelated", labels=["k8s_sprints"], project="SAT") is True
    assert evaluate_predicate(predicate, component="unrelated", labels=["other"], project="SAT") is False
    # the outer "project IN (SAT)" excludes any other project entirely
    assert evaluate_predicate(predicate, component="helm-chart", labels=[], project="PLAT") is False


@pytest.mark.parametrize(
    "jql",
    [
        "project IN SAT",
        "project IN (",
        "project IN ()",
        "project NOT SAT",
        "project IN (SAT PLAT)",
    ],
)
def test_compile_rejects_malformed_in_expressions(jql: str) -> None:
    predicate, reason = compile_jql(jql, component_field_names=[COMPONENT_FIELD_NAME])

    assert predicate is None
    assert reason


def test_evaluate_raises_on_malformed_predicate_node() -> None:
    with pytest.raises(ValueError):
        evaluate_predicate({"op": "bogus"}, component=None, labels=[])


def test_compile_matches_any_configured_clause_name_alias() -> None:
    # Regression test: Jira's field "name" and its JQL "clauseNames" can
    # differ (a custom field can be named "Components" but only queryable
    # in JQL as "Components[Dropdown]", to disambiguate from the native
    # field of the same name) -- compile_jql must match any known alias,
    # not just one.
    predicate, reason = compile_jql(
        '"Components[Dropdown]" = helm-chart',
        component_field_names=["Components", "Components[Dropdown]", "cf[10071]"],
    )

    assert reason is None
    assert evaluate_predicate(predicate, component="helm-chart", labels=[]) is True
