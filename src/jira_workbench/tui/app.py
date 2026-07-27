from __future__ import annotations

from pathlib import Path
from typing import Any

from textual.app import App

from ..metadata import JiraApiConfig, MetadataError, jira_api_client
from ..view import normalize_swimlane

DEFAULT_PREVIEW_LINES = 10


class JiraWorkbenchApp(App):
    """Textual TUI for browsing and editing locally synced Jira work items."""

    TITLE = "Jira Workbench"

    def __init__(
        self,
        jira_dir: Path,
        *,
        component_field: str | None = None,
        component: str | None = None,
        pattern: str | None = None,
        active: bool = True,
        swimlane: str | None = None,
        initial_key: str | None = None,
        initial_mode: str = "shadow",
        jira_url: str | None = None,
        jira_email: str | None = None,
        jira_api_token: str | None = None,
        initial_screen: str = "index",
        project: str | None = None,
        versions_filter: str | None = None,
        preview_lines: int | None = None,
    ) -> None:
        super().__init__()
        self.jira_dir = jira_dir
        self.component_field = component_field
        self.preview_lines = preview_lines if preview_lines and preview_lines > 0 else DEFAULT_PREVIEW_LINES
        self.initial_component = component
        self.initial_pattern = pattern
        self.initial_active = active
        self.initial_swimlane = normalize_swimlane(swimlane)
        self.initial_key = initial_key
        self.initial_mode = initial_mode if initial_mode in {"shadow", "original", "diff"} else "shadow"
        self.jira_url = jira_url
        self.jira_email = jira_email
        self.jira_api_token = jira_api_token
        self.initial_screen = initial_screen if initial_screen in {"index", "meta"} else "index"
        self.project = project
        self.versions_filter = versions_filter
        self._changed_keys: set[str] = set()

    def mark_changed(self, key: str) -> None:
        self._changed_keys.add(key)

    def drain_changed_keys(self) -> set[str]:
        keys, self._changed_keys = self._changed_keys, set()
        return keys

    def on_mount(self) -> None:
        if self.initial_screen == "meta":
            from .screens.meta import MetaScreen

            self.push_screen(MetaScreen(standalone=True))
            return

        from .screens.index import IndexScreen

        self.push_screen(
            IndexScreen(
                component=self.initial_component,
                pattern=self.initial_pattern,
                active=self.initial_active,
                swimlane=self.initial_swimlane,
            )
        )
        if self.initial_key:
            from .screens.detail import DetailScreen

            self.push_screen(DetailScreen(key=self.initial_key, mode=self.initial_mode))

    def can_push(self) -> bool:
        return bool(self.jira_url and self.jira_email and self.jira_api_token)

    def get_api_client(self) -> Any:
        if not self.can_push():
            raise MetadataError("cannot push: missing Jira API configuration")
        return jira_api_client(
            JiraApiConfig(url=str(self.jira_url), email=str(self.jira_email), api_token=str(self.jira_api_token))
        )


def run_view(
    jira_dir: Path,
    *,
    component_field: str | None = None,
    component: str | None = None,
    pattern: str | None = None,
    active: bool = True,
    swimlane: str | None = None,
    initial_key: str | None = None,
    initial_mode: str = "shadow",
    jira_url: str | None = None,
    jira_email: str | None = None,
    jira_api_token: str | None = None,
    project: str | None = None,
    versions_filter: str | None = None,
    preview_lines: int | None = None,
) -> None:
    app = JiraWorkbenchApp(
        jira_dir,
        component_field=component_field,
        component=component,
        pattern=pattern,
        active=active,
        swimlane=swimlane,
        initial_key=initial_key,
        initial_mode=initial_mode,
        jira_url=jira_url,
        jira_email=jira_email,
        jira_api_token=jira_api_token,
        project=project,
        versions_filter=versions_filter,
        preview_lines=preview_lines,
    )
    app.run()


def run_meta_app(
    jira_dir: Path,
    *,
    project: str | None = None,
    jira_url: str | None = None,
    jira_email: str | None = None,
    jira_api_token: str | None = None,
    versions_filter: str | None = None,
    component_field: str | None = None,
) -> int:
    app = JiraWorkbenchApp(
        jira_dir,
        component_field=component_field,
        jira_url=jira_url,
        jira_email=jira_email,
        jira_api_token=jira_api_token,
        initial_screen="meta",
        project=project,
        versions_filter=versions_filter,
    )
    app.run()
    return 0
