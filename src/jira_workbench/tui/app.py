from __future__ import annotations

from pathlib import Path
from typing import Any

from textual import events
from textual.app import App

from ..metadata import JiraApiConfig, MetadataError, jira_api_client, load_project_registry
from ..view import DEV_STATUS_FIELD_DEFAULT, normalize_swimlane

DEFAULT_PREVIEW_LINES = 10


class JiraWorkbenchApp(App):
    """Textual TUI for browsing and editing locally synced Jira work items."""

    TITLE = "Jira Workbench"

    def __init__(
        self,
        jira_dir: Path,
        *,
        component_field: str | None = None,
        project_filter: str | tuple[str, ...] | None = None,
        status_filter: str | tuple[str, ...] | None = None,
        component: str | tuple[str, ...] | None = None,
        fix_version: str | tuple[str, ...] | None = None,
        assignee: str | tuple[str, ...] | None = None,
        board: str | None = None,
        board_scope: str | None = None,
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
        hide_done_after_days: int | None = None,
        config_path: Path | None = None,
        nerd_font: bool = False,
        dev_status_field: str | None = None,
    ) -> None:
        super().__init__()
        self.jira_dir = jira_dir
        self.component_field = component_field
        self.preview_lines = preview_lines if preview_lines and preview_lines > 0 else DEFAULT_PREVIEW_LINES
        self.hide_done_after_days = hide_done_after_days if hide_done_after_days and hide_done_after_days > 0 else None
        self.nerd_font_enabled = nerd_font
        self.dev_status_field = dev_status_field or DEV_STATUS_FIELD_DEFAULT
        self.initial_project_filter = project_filter
        self.initial_status_filter = status_filter
        self.initial_component = component
        self.initial_fix_version = fix_version
        self.initial_assignee = assignee
        self.initial_board = board
        self.initial_board_scope = board_scope
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
        self.config_path = config_path
        self._changed_keys: set[str] = set()
        self._suppress_focus_restoring_click = False
        self._api_client: Any | None = None
        # Session-scoped caches for metadata that doesn't change mid-session
        # (or rarely enough that a session cache is worth the tradeoff) --
        # without these, opening Detail on an issue or the New-issue screen
        # re-runs the same live dev-status/createmeta/editmeta/current-user
        # API calls from scratch every single time, which is both slow and
        # unnecessary. No invalidation beyond restarting the app, same as
        # every other "explicit reload, not automatic" convention here.
        self.current_user: dict[str, object] | None = None
        self.issue_type_fields_cache: dict[str, dict[str, dict[str, object]]] = {}
        self.edit_fields_cache: dict[str, dict[str, dict[str, object]]] = {}
        self.dev_status_cache: dict[str, Any] = {}
        self.all_field_names_fetched = False
        # Loaded once at startup (written by `jira-wb sync`, not expected to
        # change mid-session) -- this is a proactive UX layer only. The real
        # enforcement is shadow.py's raise_if_project_read_only, which every
        # field/status edit already goes through regardless of entry point;
        # checking here just avoids surfacing a raw ShadowError to the user.
        self._read_only_projects = load_project_registry(self.jira_dir)

    def is_project_read_only(self, project_key: str | None) -> bool:
        if not project_key:
            return False
        return bool(self._read_only_projects.get(project_key, {}).get("readOnly"))

    def mark_changed(self, key: str) -> None:
        self._changed_keys.add(key)

    def drain_changed_keys(self) -> set[str]:
        keys, self._changed_keys = self._changed_keys, set()
        return keys

    async def on_event(self, event: events.Event) -> None:
        # When the terminal window itself doesn't have OS focus, the click
        # that restores focus to it is still delivered to us as an ordinary
        # mouse event -- without this, that single click both refocuses the
        # terminal *and* acts on whatever row/field happens to be under the
        # cursor, which is surprising: the user only meant to refocus.
        # Textual tracks `app_focus` already (flipped True on the first
        # Key/MouseDown it sees while unfocused) but doesn't itself suppress
        # that first click from also being dispatched -- this fills that
        # gap. Swallowing MouseDown here also stops Textual's own MouseUp
        # handler from ever running for the matching MouseUp (since we
        # return before calling super().on_event), which is what would
        # otherwise synthesize a Click message -- so there's nothing left
        # for the widget under the cursor to react to.
        if isinstance(event, events.MouseDown) and not self.app_focus:
            self.app_focus = True
            self._suppress_focus_restoring_click = True
            return
        if self._suppress_focus_restoring_click and isinstance(event, (events.MouseUp, events.Click)):
            self._suppress_focus_restoring_click = False
            return
        await super().on_event(event)

    def on_mount(self) -> None:
        if self.initial_screen == "meta":
            from .screens.meta import MetaScreen

            self.push_screen(MetaScreen(standalone=True))
            return

        from .screens.index import IndexScreen

        self.push_screen(
            IndexScreen(
                project=self.initial_project_filter,
                status=self.initial_status_filter,
                component=self.initial_component,
                fix_version=self.initial_fix_version,
                assignee=self.initial_assignee,
                board=self.initial_board,
                board_scope=self.initial_board_scope,
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
        if self._api_client is None:
            self._api_client = jira_api_client(
                JiraApiConfig(url=str(self.jira_url), email=str(self.jira_email), api_token=str(self.jira_api_token))
            )
        return self._api_client


def run_view(
    jira_dir: Path,
    *,
    component_field: str | None = None,
    project_filter: str | tuple[str, ...] | None = None,
    status_filter: str | tuple[str, ...] | None = None,
    component: str | tuple[str, ...] | None = None,
    fix_version: str | tuple[str, ...] | None = None,
    assignee: str | tuple[str, ...] | None = None,
    board: str | None = None,
    board_scope: str | None = None,
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
    hide_done_after_days: int | None = None,
    config_path: Path | None = None,
    nerd_font: bool = False,
    dev_status_field: str | None = None,
) -> None:
    app = JiraWorkbenchApp(
        jira_dir,
        component_field=component_field,
        project_filter=project_filter,
        status_filter=status_filter,
        component=component,
        fix_version=fix_version,
        assignee=assignee,
        board=board,
        board_scope=board_scope,
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
        hide_done_after_days=hide_done_after_days,
        config_path=config_path,
        nerd_font=nerd_font,
        dev_status_field=dev_status_field,
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
    config_path: Path | None = None,
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
        config_path=config_path,
    )
    app.run()
    return 0
