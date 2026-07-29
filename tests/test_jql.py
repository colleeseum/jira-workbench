from __future__ import annotations

import pytest

from jira_workbench.jql import (
    compile_jql,
    evaluate_predicate,
    predicate_scopes_project,
    scope_predicate_to_project,
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


def test_scope_predicate_to_project_wraps_a_filter_with_no_project_clause() -> None:
    # A Jira board only ever shows issues from its own project even when
    # its saved filter's JQL doesn't literally say so (real Jira board
    # behavior, not a JQL default) -- e.g. a board filter that's just
    # "component = helm-chart" still only ever shows that component's
    # issues within the one project the board itself belongs to.
    predicate, reason = compile_jql(
        f'"{COMPONENT_FIELD_NAME}" = helm-chart', component_field_names=[COMPONENT_FIELD_NAME]
    )
    assert reason is None
    assert predicate_scopes_project(predicate) is False

    scoped = scope_predicate_to_project(predicate, "SAT")

    assert scoped == {
        "op": "and",
        "clauses": [{"op": "eq", "field": "project", "value": "SAT"}, predicate],
    }
    assert evaluate_predicate(scoped, component="helm-chart", labels=[], project="SAT") is True
    # same component, wrong project -- the implicit scoping excludes it
    assert evaluate_predicate(scoped, component="helm-chart", labels=[], project="PLAT") is False


def test_scope_predicate_to_project_is_a_no_op_when_already_scoped() -> None:
    predicate, reason = compile_jql("project = SAT ORDER BY Rank ASC", component_field_names=[COMPONENT_FIELD_NAME])
    assert reason is None
    assert predicate_scopes_project(predicate) is True

    assert scope_predicate_to_project(predicate, "SAT") == predicate


def test_scope_predicate_to_project_is_a_no_op_for_a_genuine_multi_project_board() -> None:
    jql = f'project = SAT OR project = PLAT OR "{COMPONENT_FIELD_NAME}" = helm-chart'
    predicate, reason = compile_jql(jql, component_field_names=[COMPONENT_FIELD_NAME])
    assert reason is None
    assert predicate_scopes_project(predicate) is True

    assert scope_predicate_to_project(predicate, "SAT") == predicate
    # PLAT is still matched via its own explicit "project = PLAT" clause
    assert evaluate_predicate(predicate, component=None, labels=[], project="PLAT") is True
