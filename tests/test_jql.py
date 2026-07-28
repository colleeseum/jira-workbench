from __future__ import annotations

import pytest

from jira_workbench.jql import compile_jql, evaluate_predicate

COMPONENT_FIELD_NAME = "Components[Dropdown]"


def test_compile_whole_project_filter_is_always_true() -> None:
    predicate, reason = compile_jql("project = SAT ORDER BY Rank ASC", component_field_names=[COMPONENT_FIELD_NAME])

    assert reason is None
    assert predicate == {"op": "true"}
    assert evaluate_predicate(predicate, component=None, labels=[]) is True


def test_compile_compound_board_filter_matches_real_jql() -> None:
    jql = (
        'project = SAT AND ( "Components[Dropdown]" = helm-chart OR "Components[Dropdown]" = puppy )  '
        "OR  labels=k8s_sprints \n"
    )

    predicate, reason = compile_jql(jql, component_field_names=[COMPONENT_FIELD_NAME])

    assert reason is None
    # matches via component
    assert evaluate_predicate(predicate, component="helm-chart", labels=[]) is True
    assert evaluate_predicate(predicate, component="puppy", labels=[]) is True
    # case-insensitive
    assert evaluate_predicate(predicate, component="Helm-Chart", labels=[]) is True
    # matches via label, regardless of component
    assert evaluate_predicate(predicate, component="unrelated", labels=["k8s_sprints"]) is True
    # matches neither
    assert evaluate_predicate(predicate, component="unrelated", labels=["other"]) is False
    assert evaluate_predicate(predicate, component=None, labels=[]) is False


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
