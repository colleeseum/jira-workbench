from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEV_STATUS_SUMMARY_PATH = "rest/dev-status/1.0/issue/summary"
DEV_STATUS_DETAIL_PATH = "rest/dev-status/1.0/issue/detail"


@dataclass(frozen=True)
class DevBranch:
    name: str
    url: str
    repo_name: str
    repo_url: str


@dataclass(frozen=True)
class DevPullRequest:
    name: str
    url: str
    status: str  # OPEN / MERGED / DECLINED
    repo_name: str
    repo_url: str


@dataclass(frozen=True)
class DevStatus:
    pull_requests: list[DevPullRequest] = field(default_factory=list)
    branches: list[DevBranch] = field(default_factory=list)
    error: str | None = None


def fetch_dev_status(client: Any, issue_id: str) -> DevStatus:
    """Best-effort read of Jira's Development panel data for an issue.

    This calls Jira's internal, undocumented dev-status API (there is no
    officially supported equivalent for reading data another app already
    synced -- see the Boards workstream's README notes on `devinfo` vs
    `dev-status`). It can change shape or disappear without notice, so
    every step degrades to an empty/partial result instead of raising --
    callers should never need a try/except around this.
    """
    try:
        summary = client.get(DEV_STATUS_SUMMARY_PATH, params={"issueId": issue_id})
    except Exception as exc:
        return DevStatus(error=str(exc))
    if not isinstance(summary, dict):
        return DevStatus(error="unexpected dev-status summary response")

    instance_types = _instance_types_with_data(summary)
    pull_requests: list[DevPullRequest] = []
    branches: list[DevBranch] = []
    for app_type in instance_types:
        try:
            pr_payload = client.get(
                DEV_STATUS_DETAIL_PATH,
                params={"issueId": issue_id, "applicationType": app_type, "dataType": "pullrequest"},
            )
            pull_requests.extend(_parse_pull_requests(pr_payload))
        except Exception:
            pass
        try:
            branch_payload = client.get(
                DEV_STATUS_DETAIL_PATH,
                params={"issueId": issue_id, "applicationType": app_type, "dataType": "branch"},
            )
            branches.extend(_parse_branches(branch_payload))
        except Exception:
            pass
    return DevStatus(pull_requests=pull_requests, branches=branches)


def _instance_types_with_data(summary: dict[str, Any]) -> list[str]:
    inner = summary.get("summary")
    if not isinstance(inner, dict):
        return []
    seen: list[str] = []
    for data_type in ("pullrequest", "branch"):
        entry = inner.get(data_type)
        if not isinstance(entry, dict):
            continue
        by_instance = entry.get("byInstanceType")
        if not isinstance(by_instance, dict):
            continue
        for app_type in by_instance:
            if app_type not in seen:
                seen.append(app_type)
    return seen


def _parse_pull_requests(payload: Any) -> list[DevPullRequest]:
    results: list[DevPullRequest] = []
    if not isinstance(payload, dict):
        return results
    for entry in payload.get("detail") or []:
        if not isinstance(entry, dict):
            continue
        for pr in entry.get("pullRequests") or []:
            if not isinstance(pr, dict):
                continue
            name = pr.get("name") or pr.get("id")
            url = pr.get("url")
            if not isinstance(name, str) or not isinstance(url, str):
                continue
            results.append(
                DevPullRequest(
                    name=name,
                    url=url,
                    status=str(pr.get("status") or "UNKNOWN"),
                    repo_name=str(pr.get("repositoryName") or ""),
                    repo_url=str(pr.get("repositoryUrl") or ""),
                )
            )
    return results


def _parse_branches(payload: Any) -> list[DevBranch]:
    results: list[DevBranch] = []
    if not isinstance(payload, dict):
        return results
    for entry in payload.get("detail") or []:
        if not isinstance(entry, dict):
            continue
        for branch in entry.get("branches") or []:
            if not isinstance(branch, dict):
                continue
            name = branch.get("name")
            url = branch.get("url")
            if not isinstance(name, str) or not isinstance(url, str):
                continue
            repository = branch.get("repository")
            repository = repository if isinstance(repository, dict) else {}
            results.append(
                DevBranch(
                    name=name,
                    url=url,
                    repo_name=str(repository.get("name") or ""),
                    repo_url=str(repository.get("url") or ""),
                )
            )
    return results
