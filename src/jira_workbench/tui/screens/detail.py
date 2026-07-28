from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from ...devstatus import DevStatus, fetch_dev_status
from ...issue import IssueError, fetch_all_field_names, fetch_issue_edit_fields
from ...metadata import MetadataError, remember_field_names
from ...shadow import ShadowError, add_comment, delete_shadow, load_shadow, push_key, render_diff, set_field, set_status_change
from ...view import (
    DEFAULT_RESOLUTIONS,
    DONE_STATUSES,
    apply_shadow,
    as_dict,
    comma_parts,
    comments_text,
    detail_field_rows,
    display_name,
    editable_detail_fields,
    editable_field_value,
    encode_edit_value,
    extract_field_names,
    field_label_options,
    has_shadow_changes,
    hierarchy_section,
    issue_identity,
    issue_parent_key,
    label_type_fields,
    load_issue,
    other_field_rows,
    pill_values,
    resolve_fix_version_names,
    selectable_field_options,
    shadow_change_summary,
    wrap_preview_lines,
)
from ..render import priority_option_render, render_pills
from ..widgets.prompts import (
    ConfirmScreen,
    LabelsPickerScreen,
    OptionPickerScreen,
    StatusChangeScreen,
    TextAreaPromptScreen,
    TextPromptScreen,
    TextViewScreen,
)
from ..widgets.tables import ClickableRowDataTable

EXPANDABLE_FIELDS = {"description", "comments"}
PREVIEW_WIDTH = 100
DUE_DATE_FORMAT = "%Y-%m-%d"


class DetailScreen(Screen[None]):
    """Work item detail view: field browser with inline shadow editing."""

    BINDINGS = [
        Binding("s", "show_shadow", "Shadow"),
        Binding("o", "show_original", "Original"),
        Binding("d", "show_diff", "Diff"),
        Binding("x", "view_field", "View full text"),
        Binding("c", "add_comment", "Comment"),
        Binding("r", "revert", "Revert"),
        Binding("p", "push", "Push"),
        Binding("O", "toggle_other", "Other fields"),
        Binding("v", "view_key", "View key"),
        Binding("V", "open_parent", "Parent"),
        Binding("h", "show_help", "Help"),
        Binding("q", "quit_app", "Quit"),
        Binding("escape", "close", "Back"),
    ]

    def __init__(self, *, key: str, mode: str = "shadow") -> None:
        super().__init__()
        self.key = key
        self.mode = mode if mode in {"shadow", "original", "diff"} else "shadow"
        self.show_other_fields = False
        self.issue: dict[str, Any] = {}
        self.effective_issue: dict[str, Any] = {}
        self.shadow: dict[str, Any] | None = None
        self._field_values: dict[str, str] = {}
        self.edit_fields: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="detail-header")
        yield Static(id="detail-dev")
        yield ClickableRowDataTable(id="detail-table", cursor_type="row")
        yield Static(id="detail-diff")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Field", key="field", width=16)
        table.add_column("Value", key="value")
        self.query_one("#detail-dev", Static).display = False
        self._refresh_data()
        self._load_dev_status()
        self._load_edit_fields()

    def _refresh_data(self) -> None:
        jira_dir = self.app.jira_dir
        issue = load_issue(jira_dir, self.key)
        self.issue = issue
        shadow = None if self.mode == "original" else load_shadow(jira_dir, self.key)
        self.shadow = shadow
        effective = apply_shadow(issue, shadow) if shadow is not None else issue
        self.effective_issue = effective
        self.title = issue_identity(effective)

        header_lines = list(hierarchy_section(jira_dir, effective, self.app.component_field))
        if shadow is not None and has_shadow_changes(shadow):
            header_lines.append("")
            header_lines.extend(shadow_change_summary(shadow, jira_dir, self.key))
        self.query_one("#detail-header", Static).update(Text("\n".join(header_lines)))

        diff_widget = self.query_one("#detail-diff", Static)
        table = self.query_one(DataTable)
        if self.mode == "diff":
            table.display = False
            diff_widget.display = True
            diff_widget.update(Text(render_diff(jira_dir, self.key)))
            return
        table.display = True
        diff_widget.display = False

        comments = comments_text(jira_dir, self.key, shadow)
        rows = list(detail_field_rows(effective, self.app.component_field, comments, edit_fields=self.edit_fields))
        if self.show_other_fields:
            rows.extend((field, field, value) for field, value in other_field_rows(effective, self.app.component_field))

        preview_lines = self.app.preview_lines

        table.clear()
        self._field_values = {field: value for _, field, value in rows}
        for label, field, value in rows:
            max_lines = preview_lines if field in EXPANDABLE_FIELDS else 1
            content, height = self._row_content(field, value, max_lines)
            table.add_row(label, content, key=field, height=height)

    @work
    async def _load_dev_status(self) -> None:
        # Fetched once per screen instance (on_mount only) rather than on
        # every _refresh_data() call -- a local field edit or mode switch
        # doesn't change what's linked on GitHub, so re-fetching on those
        # would just hammer an unsupported, best-effort API for nothing.
        # Also cached per key on the app for the rest of the session, and
        # run in a thread -- fetch_dev_status makes several synchronous HTTP
        # calls, and atlassian-python-api is a blocking (non-asyncio) client,
        # so running it inline here would freeze the whole UI for the
        # duration of every round trip, not just delay this one panel.
        dev_widget = self.query_one("#detail-dev", Static)
        if not self.app.can_push():
            return
        issue_id = self.issue.get("id")
        if not isinstance(issue_id, (str, int)):
            return
        try:
            client = self.app.get_api_client()
        except MetadataError:
            return
        status = self.app.dev_status_cache.get(self.key)
        if status is None:
            status = await asyncio.to_thread(fetch_dev_status, client, str(issue_id))
            self.app.dev_status_cache[self.key] = status
        content = self._dev_status_content(status)
        if content is None:
            return
        dev_widget.update(content)
        dev_widget.display = True

    @work
    async def _load_edit_fields(self) -> None:
        # Fetched once per screen instance, same as dev status -- an issue's
        # edit-screen field configuration doesn't change from a local edit or
        # mode switch. Any failure just leaves the fixed field set in place
        # (native Labels included), never an error the user has to deal with.
        # Cached per key and offloaded to a thread for the same reason as
        # _load_dev_status above -- see its comment.
        if not self.app.can_push():
            return
        try:
            client = self.app.get_api_client()
        except MetadataError:
            return
        await self._ensure_all_field_names_cached(client)
        edit_fields = self.app.edit_fields_cache.get(self.key)
        if edit_fields is None:
            try:
                edit_fields = await asyncio.to_thread(fetch_issue_edit_fields, client, self.key)
            except IssueError:
                return
            self.app.edit_fields_cache[self.key] = edit_fields
            # Opportunistically remember custom fields' display names locally
            # (e.g. "customfield_10082" -> "Customers SAT") so shadow reports
            # can show them later -- including from the offline CLI, which
            # never makes this call itself.
            remember_field_names(self.app.jira_dir, extract_field_names(edit_fields))
        self.edit_fields = edit_fields
        self._refresh_data()

    async def _ensure_all_field_names_cached(self, client: Any) -> None:
        # Broader than the per-issue editmeta harvest above -- one instance-
        # wide fetch covering every custom field, not just the ones on this
        # particular issue's own edit screen, so a report about a *different*
        # issue's custom field still shows a friendly name even if Detail has
        # never been opened on an issue where that field applies. Fetched
        # once per session; the flag is set before the fetch even completes
        # so a failure doesn't retry on every single Detail open.
        if self.app.all_field_names_fetched:
            return
        self.app.all_field_names_fetched = True
        try:
            names = await asyncio.to_thread(fetch_all_field_names, client)
        except IssueError:
            return
        remember_field_names(self.app.jira_dir, names)

    def _dev_status_content(self, status: DevStatus) -> Text | None:
        if not status.pull_requests and not status.branches:
            return None
        status_colors = {"OPEN": "green", "MERGED": "magenta", "DECLINED": "red"}
        text = Text()
        text.append("Development\n", style="bold")
        for pr in status.pull_requests:
            text.append("  PR ")
            text.append(pr.status, style=status_colors.get(pr.status.upper(), "dim"))
            text.append(f"  {pr.name}")
            if pr.repo_name:
                text.append(f"  ({pr.repo_name})", style="dim")
            text.append(f"\n    {pr.url}\n", style="dim underline")
        for branch in status.branches:
            text.append(f"  branch  {branch.name}")
            if branch.repo_name:
                text.append(f"  ({branch.repo_name})", style="dim")
            text.append("\n")
        return text

    def _pill_fields(self) -> set[str]:
        component_field = self.app.component_field or "components"
        return {"labels", "fixVersions", component_field} | {
            field_id for field_id, _ in label_type_fields(self.edit_fields)
        }

    def _row_content(self, field: str, value: str, max_lines: int) -> tuple[Any, int]:
        if field in EXPANDABLE_FIELDS:
            lines = wrap_preview_lines(value, PREVIEW_WIDTH)
            shown = lines[:max_lines]
            more = len(lines) > max_lines
            text = "\n".join(shown) + (" [...]" if more else "")
            return Text(text), len(shown)
        if field in self._pill_fields():
            fields = as_dict(self.effective_issue.get("fields"))
            if field == "fixVersions":
                names = resolve_fix_version_names(self.app.jira_dir, fields.get(field))
            else:
                names = pill_values(fields.get(field))
            return render_pills(names), 1
        return Text(value), 1

    def _selected_field(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return row_key.value

    def action_show_shadow(self) -> None:
        self.mode = "shadow"
        self._refresh_data()

    def action_show_original(self) -> None:
        self.mode = "original"
        self._refresh_data()

    def action_show_diff(self) -> None:
        self.mode = "diff"
        self._refresh_data()

    @work
    async def action_view_field(self) -> None:
        field = self._selected_field()
        if field == "comments":
            await self._open_comments()
            return
        if field != "description":
            self.notify("select Description or Comments to view")
            return
        await self._view_field(field)

    def action_toggle_other(self) -> None:
        self.show_other_fields = not self.show_other_fields
        self._refresh_data()

    @work
    async def action_add_comment(self) -> None:
        comment = await self.app.push_screen_wait(TextAreaPromptScreen("Comment:"))
        if not comment:
            return
        add_comment(self.app.jira_dir, self.key, comment)
        self.app.mark_changed(self.key)
        self.mode = "shadow"
        self._refresh_data()
        self.notify("comment added to shadow")

    @work
    async def action_revert(self) -> None:
        confirmed = await self.app.push_screen_wait(ConfirmScreen(f"Revert local shadow for {self.key}?"))
        if not confirmed:
            return
        delete_shadow(self.app.jira_dir, self.key)
        self.app.mark_changed(self.key)
        self.mode = "shadow"
        self._refresh_data()
        self.notify("local shadow reverted")

    @work
    async def action_push(self) -> None:
        if not self.app.can_push():
            self.notify("cannot push: missing Jira API configuration", severity="warning")
            return
        confirmed = await self.app.push_screen_wait(ConfirmScreen(f"Push shadow for {self.key} to Jira?"))
        if not confirmed:
            return
        try:
            client = self.app.get_api_client()
            result = push_key(
                self.app.jira_dir,
                self.key,
                client,
                progress=None,
                component_field=self.app.component_field or "components",
            )
        except (MetadataError, ShadowError) as exc:
            self.notify(f"push failed: {exc}", severity="error")
            return
        self.app.mark_changed(self.key)
        self.mode = "shadow"
        self._refresh_data()
        self.notify(f"push result: {result}")

    @work
    async def action_view_key(self) -> None:
        key = await self.app.push_screen_wait(TextPromptScreen("View work item key:", initial=self.key))
        if key:
            self.app.push_screen(DetailScreen(key=key))

    def action_open_parent(self) -> None:
        parent_key = issue_parent_key(self.effective_issue)
        if not parent_key:
            self.notify("no parent on this item")
            return
        self.app.push_screen(DetailScreen(key=parent_key))

    def action_show_help(self) -> None:
        from .help import HelpScreen

        self.app.push_screen(HelpScreen())

    def action_quit_app(self) -> None:
        self.app.exit()

    def action_close(self) -> None:
        self.dismiss(None)

    @work
    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        field = event.row_key.value
        if field is None:
            return
        if field == "comments":
            await self._open_comments()
            return
        if field == "description":
            await self._view_field(field)
            return
        editable_fields = editable_detail_fields(self.app.component_field, self.edit_fields)
        if field not in editable_fields:
            self.notify(f"{field} is not editable")
            return
        await self._edit_field(field)

    async def _open_comments(self) -> None:
        from .comments import CommentsScreen

        screen = CommentsScreen(self.app.jira_dir, self.key)
        await self.app.push_screen_wait(screen)
        if screen.changed:
            self.app.mark_changed(self.key)
            self.mode = "shadow"
            self._refresh_data()

    async def _view_field(self, field: str) -> None:
        editable = field in editable_detail_fields(self.app.component_field, self.edit_fields)
        result = await self.app.push_screen_wait(
            TextViewScreen("Description", self._field_values.get(field, ""), editable=editable)
        )
        if result is None:
            return
        encoded = encode_edit_value(field, result, component_field=self.app.component_field)
        set_field(self.app.jira_dir, self.key, field, encoded)
        self.app.mark_changed(self.key)
        self.mode = "shadow"
        self._refresh_data()
        self.notify(f"shadow updated: {field}")

    async def _edit_field(self, field: str) -> None:
        current_value = editable_field_value(self.effective_issue, field)
        if field == "status":
            await self._edit_status(current_value)
            return
        if field == "duedate":
            await self._edit_due_date(current_value)
            return
        if field in {field_id for field_id, _ in label_type_fields(self.edit_fields)}:
            await self._edit_label_field(field, current_value)
            return
        options = selectable_field_options(
            self.app.jira_dir,
            field,
            self.app.component_field,
            current_issue=self.issue,
        )
        if options:
            render = priority_option_render(nerd_font=self.app.nerd_font_enabled) if field == "priority" else None
            value = await self.app.push_screen_wait(
                OptionPickerScreen(f"{field}:", options, current=current_value, render=render)
            )
        else:
            value = await self.app.push_screen_wait(TextPromptScreen(f"{field}:", initial=current_value))
        if value is None:
            return
        encoded = encode_edit_value(field, value, component_field=self.app.component_field)
        set_field(self.app.jira_dir, self.key, field, encoded)
        self.app.mark_changed(self.key)
        self.mode = "shadow"
        self._refresh_data()
        self.notify(f"shadow updated: {field}")

    async def _edit_due_date(self, current_value: str) -> None:
        value = await self.app.push_screen_wait(TextPromptScreen("Due date (YYYY-MM-DD):", initial=current_value))
        if value is None:
            return
        stripped = value.strip()
        if stripped and stripped != "(none)":
            try:
                datetime.strptime(stripped, DUE_DATE_FORMAT)
            except ValueError:
                self.notify("invalid due date, expected YYYY-MM-DD", severity="error")
                return
        encoded = None if not stripped or stripped == "(none)" else stripped
        set_field(self.app.jira_dir, self.key, "duedate", encoded)
        self.app.mark_changed(self.key)
        self.mode = "shadow"
        self._refresh_data()
        self.notify("shadow updated: duedate")

    async def _edit_label_field(self, field: str, current_value: str) -> None:
        known = field_label_options(self.app.jira_dir, field)
        selected = comma_parts(current_value)
        choice = await self.app.push_screen_wait(LabelsPickerScreen(known, selected=selected))
        if choice is None:
            return
        set_field(self.app.jira_dir, self.key, field, choice)
        self.app.mark_changed(self.key)
        self.mode = "shadow"
        self._refresh_data()
        self.notify(f"shadow updated: {field}")

    async def _edit_status(self, current_value: str) -> None:
        options = selectable_field_options(self.app.jira_dir, "status")
        result = await self.app.push_screen_wait(
            StatusChangeScreen(
                options,
                DEFAULT_RESOLUTIONS,
                current_status=current_value,
                is_done_status=lambda status: display_name(status).strip().lower() in DONE_STATUSES,
            )
        )
        if result is None:
            return
        status, resolution = result
        set_field(self.app.jira_dir, self.key, "status", status)
        if resolution:
            set_status_change(self.app.jira_dir, self.key, resolution=resolution)
        self.app.mark_changed(self.key)
        self.mode = "shadow"
        self._refresh_data()
        self.notify("shadow updated: status")
