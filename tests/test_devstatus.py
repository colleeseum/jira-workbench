from __future__ import annotations

from jira_workbench.devstatus import DevBranch, DevPullRequest, DevStatus, fetch_dev_status

APP_TYPE = "oAuth-com.github.integration.production"

REAL_SUMMARY = {
    "errors": [],
    "configErrors": [],
    "summary": {
        "pullrequest": {
            "overall": {"count": 1, "state": "OPEN", "open": True, "dataType": "pullrequest"},
            "byInstanceType": {APP_TYPE: {"count": 1, "name": "GitHub"}},
        },
        "branch": {
            "overall": {"count": 2, "dataType": "branch"},
            "byInstanceType": {APP_TYPE: {"count": 2, "name": "GitHub"}},
        },
        "repository": {"overall": {"count": 0, "dataType": "repository"}, "byInstanceType": {}},
    },
}

REAL_PR_DETAIL = {
    "errors": [],
    "detail": [
        {
            "pullRequests": [
                {
                    "id": "#15",
                    "name": "Support for hardened image",
                    "status": "OPEN",
                    "url": "https://github.com/stardog-oss/kube-stardog-stack/pull/15",
                    "repositoryId": "1173031191",
                    "repositoryName": "stardog-oss/kube-stardog-stack",
                    "repositoryUrl": "https://github.com/stardog-oss/kube-stardog-stack",
                }
            ],
        }
    ],
}

REAL_BRANCH_DETAIL = {
    "errors": [],
    "detail": [
        {
            "branches": [
                {
                    "name": "SAT-741-Chainguard-zookeeper",
                    "url": "https://github.com/stardog-oss/kube-stardog-stack/tree/SAT-741-Chainguard-zookeeper",
                    "repository": {
                        "id": "1173031191",
                        "name": "stardog-oss/kube-stardog-stack",
                        "url": "https://github.com/stardog-oss/kube-stardog-stack",
                    },
                },
                {
                    "name": "SAT-741-Chainguard-zookeeper",
                    "url": "https://github.com/stardog-union/helm_training/tree/SAT-741-Chainguard-zookeeper",
                    "repository": {
                        "id": "1131362692",
                        "name": "stardog-union/helm_training",
                        "url": "https://github.com/stardog-union/helm_training",
                    },
                },
            ],
        }
    ],
}


class FakeDevStatusClient:
    def __init__(self, *, summary=None, pr_detail=None, branch_detail=None, raise_on=None) -> None:
        self.summary = summary if summary is not None else REAL_SUMMARY
        self.pr_detail = pr_detail if pr_detail is not None else REAL_PR_DETAIL
        self.branch_detail = branch_detail if branch_detail is not None else REAL_BRANCH_DETAIL
        self.raise_on = raise_on or set()
        self.calls: list[tuple[str, object]] = []

    def get(self, path: str, params: dict[str, object] | None = None) -> object:
        self.calls.append((path, params))
        if path in self.raise_on:
            raise RuntimeError(f"boom: {path}")
        if path == "rest/dev-status/1.0/issue/summary":
            return self.summary
        if path == "rest/dev-status/1.0/issue/detail":
            data_type = params["dataType"] if params else None
            if data_type == "pullrequest":
                return self.pr_detail
            if data_type == "branch":
                return self.branch_detail
        raise AssertionError(f"unexpected call: {path} {params}")


def test_fetch_dev_status_parses_real_shaped_pull_requests_and_branches() -> None:
    client = FakeDevStatusClient()

    status = fetch_dev_status(client, "78547")

    assert status.error is None
    assert status.pull_requests == [
        DevPullRequest(
            name="Support for hardened image",
            url="https://github.com/stardog-oss/kube-stardog-stack/pull/15",
            status="OPEN",
            repo_name="stardog-oss/kube-stardog-stack",
            repo_url="https://github.com/stardog-oss/kube-stardog-stack",
        )
    ]
    assert status.branches == [
        DevBranch(
            name="SAT-741-Chainguard-zookeeper",
            url="https://github.com/stardog-oss/kube-stardog-stack/tree/SAT-741-Chainguard-zookeeper",
            repo_name="stardog-oss/kube-stardog-stack",
            repo_url="https://github.com/stardog-oss/kube-stardog-stack",
        ),
        DevBranch(
            name="SAT-741-Chainguard-zookeeper",
            url="https://github.com/stardog-union/helm_training/tree/SAT-741-Chainguard-zookeeper",
            repo_name="stardog-union/helm_training",
            repo_url="https://github.com/stardog-union/helm_training",
        ),
    ]


def test_fetch_dev_status_discovers_application_type_from_summary() -> None:
    client = FakeDevStatusClient()

    fetch_dev_status(client, "78547")

    detail_calls = [params for path, params in client.calls if path == "rest/dev-status/1.0/issue/detail"]
    assert all(params["applicationType"] == APP_TYPE for params in detail_calls)


def test_fetch_dev_status_returns_empty_when_no_linked_data() -> None:
    empty_summary = {
        "summary": {
            "pullrequest": {"overall": {"count": 0}, "byInstanceType": {}},
            "branch": {"overall": {"count": 0}, "byInstanceType": {}},
        }
    }
    client = FakeDevStatusClient(summary=empty_summary)

    status = fetch_dev_status(client, "1")

    assert status == DevStatus(pull_requests=[], branches=[], error=None)
    # no detail calls should have been made at all -- nothing to look up
    assert not any(path == "rest/dev-status/1.0/issue/detail" for path, _ in client.calls)


def test_fetch_dev_status_sets_error_when_summary_call_fails() -> None:
    client = FakeDevStatusClient(raise_on={"rest/dev-status/1.0/issue/summary"})

    status = fetch_dev_status(client, "78547")

    assert status.pull_requests == []
    assert status.branches == []
    assert status.error is not None
    assert "boom" in status.error


def test_fetch_dev_status_ignores_a_single_failing_instance_type_without_losing_others() -> None:
    # Two instance types report data in the summary; only one detail call
    # actually succeeds -- the other should just be silently omitted.
    two_type_summary = {
        "summary": {
            "pullrequest": {
                "overall": {"count": 1},
                "byInstanceType": {APP_TYPE: {"count": 1}, "other-instance": {"count": 1}},
            },
            "branch": {"overall": {"count": 0}, "byInstanceType": {}},
        }
    }

    class PartiallyFailingClient(FakeDevStatusClient):
        def get(self, path: str, params: dict[str, object] | None = None) -> object:
            if path == "rest/dev-status/1.0/issue/detail" and params.get("applicationType") == "other-instance":
                raise RuntimeError("this instance type is broken")
            return super().get(path, params)

    client = PartiallyFailingClient(summary=two_type_summary)

    status = fetch_dev_status(client, "78547")

    assert status.error is None
    assert len(status.pull_requests) == 1


def test_fetch_dev_status_handles_unexpected_response_shapes_without_raising() -> None:
    client = FakeDevStatusClient(summary={"unexpected": "shape"})

    status = fetch_dev_status(client, "78547")

    assert status.pull_requests == []
    assert status.branches == []
    assert status.error is None


def test_fetch_dev_status_handles_non_dict_summary() -> None:
    client = FakeDevStatusClient(summary=["not", "a", "dict"])

    status = fetch_dev_status(client, "78547")

    assert status.error == "unexpected dev-status summary response"
