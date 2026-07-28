from __future__ import annotations

from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from ...issue import IssueError, build_create_fields, create_issue
from ...shadow import ShadowError, refresh_local_issue_after_push, user_payload
from ...view import (
    comma_parts,
    component_options,
    issue_key_from_text,
    label_options,
    observed_field_options,
    parent_options,
    version_options,
)
from ..render import priority_option_render, render_pills
from ..widgets.prompts import (
    ConfirmScreen,
    LabelsPickerScreen,
    OptionPickerScreen,
    TextAreaPromptScreen,
    TextPromptScreen,
)
from ..widgets.tables import ClickableRowDataTable

HARD_CODED_STATUS = "To Do"

# Ordered to match DetailScreen's normal (non-Other, non-Comments) field
# layout: Summary, Description, Type, Status, Priority, Version, Assignee,
# Reporter, Parent, Component, Labels.
FIELD_LABELS = [
    ("summary", "Summary"),
    ("description", "Description"),
    ("type", "Type"),
    ("status", "Status"),
    ("priority", "Priority"),
    ("version", "Version"),
    ("assignee", "Assignee"),
    ("reporter", "Reporter"),
    ("parent", "Parent"),
    ("component", "Component"),
    ("labels", "Labels"),
]

# create_fields keys (Jira's own create-screen config) each row depends on,
# for the fields that only apply once a Type is chosen. "component" is
# special-cased since its real key is whatever component_field resolves to
# (native or custom). Summary/Description/Type/Status are unconditional.
FIELD_JIRA_KEYS = {
    "version": "fixVersions",
    "priority": "priority",
    "labels": "labels",
    "assignee": "assignee",
    "reporter": "reporter",
    "parent": "parent",
}

ALWAYS_VISIBLE = ("summary", "description", "type", "status")


def _preview(value: str) -> str:
    if not value:
        return ""
    first_line, _, rest = value.partition("\n")
    truncated = first_line[:100]
    more = bool(rest) or len(first_line) > 100
    return truncated + (" […]" if more else "")


class IssueCreateScreen(Screen[str | None]):
    """New-issue form: one screen, a Field/Value table edited in place like
    DetailScreen -- Enter opens a picker/prompt for the selected row, `p`
    creates the issue once ready. Returns the new issue's key, or None if
    cancelled.

    Summary, Description, and Type have no default and are required. Status
    is fixed to the workflow's initial status ("To Do") -- it is not a
    create-screen field in Jira at all (only reachable via a separate
    transition call after creation, which this form deliberately does not
    attempt), so it's shown for information only, not editable. Every other
    field applies only once Type is chosen (Jira's own create-screen config
    for that type, already fetched once by the caller) and appears/
    disappears live as Type changes.
    """

    BINDINGS = [
        Binding("p", "create", "Create"),
        Binding("q", "cancel", "Cancel"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        *,
        jira_dir: Any,
        project: str,
        component_field: str,
        type_fields: dict[str, dict[str, object]],
        client: Any,
        epic_item: dict[str, Any] | None,
        default_reporter: str,
        default_reporter_account_id: str | None,
        context_label: str,
    ) -> None:
        super().__init__()
        self._jira_dir = jira_dir
        self._project = project
        self._component_field = component_field
        self._type_fields = type_fields
        self._client = client
        self._default_reporter = default_reporter
        self._default_reporter_account_id = default_reporter_account_id
        self._context_label = context_label

        self.selected_type = ""

        epic_labels = epic_item.get("labels") if epic_item else None
        self.values: dict[str, str] = {
            "summary": "",
            "description": "",
            "component": display_name_or_empty(epic_item, "component") if epic_item else "",
            "version": display_name_or_empty(epic_item, "fixVersion") if epic_item else "",
            "priority": display_name_or_empty(epic_item, "priority") if epic_item else "",
            "labels": ", ".join(epic_labels) if isinstance(epic_labels, list) else "",
            "assignee": display_name_or_empty(epic_item, "assignee") if epic_item else "",
            "reporter": default_reporter,
            "parent": display_name_or_empty(epic_item, "key") if epic_item else "",
        }

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._context_label, id="issue-create-header")
        yield ClickableRowDataTable(id="issue-create-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Field", key="field", width=14)
        table.add_column("Value", key="value")
        if not self._context_label:
            self.query_one("#issue-create-header", Static).display = False
        self._rebuild_rows()

    def _create_fields(self) -> dict[str, object]:
        return self._type_fields.get(self.selected_type, {})

    def _visible_rows(self) -> list[str]:
        # Type must be chosen before any of these can be known -- Jira's own
        # create-screen config is what decides which of them are even valid,
        # and that's per issue type. Nothing to show for them yet without one.
        create_fields = self._create_fields()
        rows = list(ALWAYS_VISIBLE)
        for key, _label in FIELD_LABELS:
            if key in ALWAYS_VISIBLE:
                continue
            if key == "component":
                if self._component_field in create_fields:
                    rows.append(key)
            elif FIELD_JIRA_KEYS[key] in create_fields:
                rows.append(key)
        return rows

    def _row_value(self, key: str) -> Any:
        if key == "type":
            return self.selected_type or "(required)"
        if key == "status":
            return HARD_CODED_STATUS
        if key in ("summary", "description"):
            return _preview(self.values.get(key, "")) or "(required)"
        if key in ("component", "version", "labels"):
            return render_pills(comma_parts(self.values.get(key, ""))) or "(none)"
        return _preview(self.values.get(key, "")) or "(none)"

    def _rebuild_rows(self) -> None:
        table = self.query_one(DataTable)
        previous = self._current_key()
        table.clear()
        visible = self._visible_rows()
        for key in visible:
            label = dict(FIELD_LABELS)[key]
            table.add_row(label, self._row_value(key), key=key)
        if previous in visible:
            table.move_cursor(row=table.get_row_index(previous))

    def _current_key(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return row_key.value

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key is None:
            return
        self.action_edit(key)

    @work
    async def action_edit(self, key: str) -> None:
        if key == "status":
            self.notify("new issues always start at the workflow's initial status", severity="information")
            return

        if key == "type":
            choice = await self.app.push_screen_wait(
                OptionPickerScreen("Type:", sorted(self._type_fields), current=self.selected_type or None)
            )
            if choice is None or choice == self.selected_type:
                return
            self.selected_type = choice
            visible = self._visible_rows()
            for value_key in ("component", "version", "priority", "labels", "assignee", "reporter", "parent"):
                if value_key not in visible:
                    self.values[value_key] = ""
            self._rebuild_rows()
            return

        if key == "summary":
            value = await self.app.push_screen_wait(TextPromptScreen("Summary:", initial=self.values["summary"]))
            if value is not None:
                self.values["summary"] = value
            self._rebuild_rows()
            return

        if key == "description":
            value = await self.app.push_screen_wait(
                TextAreaPromptScreen("Description:", initial=self.values["description"])
            )
            if value is not None:
                self.values["description"] = value
            self._rebuild_rows()
            return

        if key == "component":
            options = component_options(self._jira_dir)
            choice = await self.app.push_screen_wait(
                OptionPickerScreen("Component:", ["(none)", *options], current=self.values["component"] or None)
            )
            if choice is not None:
                self.values["component"] = "" if choice == "(none)" else choice
            self._rebuild_rows()
            return

        if key == "version":
            options = version_options(self._jira_dir)
            choice = await self.app.push_screen_wait(
                OptionPickerScreen("Version:", options, current=self.values["version"] or None)
            )
            if choice is not None:
                self.values["version"] = "" if choice == "(none)" else choice
            self._rebuild_rows()
            return

        if key == "priority":
            options = observed_field_options(self._jira_dir, "priority")
            choice = await self.app.push_screen_wait(
                OptionPickerScreen(
                    "Priority:",
                    ["(none)", *options],
                    current=self.values["priority"] or None,
                    render=priority_option_render(nerd_font=self.app.nerd_font_enabled),
                )
            )
            if choice is not None:
                self.values["priority"] = "" if choice == "(none)" else choice
            self._rebuild_rows()
            return

        if key == "labels":
            known = label_options(self._jira_dir)
            current = comma_parts(self.values["labels"])
            choice = await self.app.push_screen_wait(LabelsPickerScreen(known, selected=current))
            if choice is not None:
                self.values["labels"] = ", ".join(choice)
            self._rebuild_rows()
            return

        if key == "assignee":
            options = observed_field_options(self._jira_dir, "assignee", include_empty="(unassigned)")
            choice = await self.app.push_screen_wait(
                OptionPickerScreen("Assignee:", options, current=self.values["assignee"] or "(unassigned)")
            )
            if choice is not None:
                self.values["assignee"] = "" if choice == "(unassigned)" else choice
            self._rebuild_rows()
            return

        if key == "reporter":
            options = observed_field_options(self._jira_dir, "reporter")
            choice = await self.app.push_screen_wait(
                OptionPickerScreen("Reporter:", ["(default)", *options], current=self.values["reporter"] or None)
            )
            if choice is not None:
                self.values["reporter"] = "" if choice == "(default)" else choice
            self._rebuild_rows()
            return

        if key == "parent":
            options = parent_options(self._jira_dir)
            current_parent = self.values["parent"]
            current_label = next(
                (option for option in options if issue_key_from_text(option) == current_parent), None
            )
            choice = await self.app.push_screen_wait(OptionPickerScreen("Parent:", options, current=current_label))
            if choice is not None:
                self.values["parent"] = (
                    ""
                    if choice == "(none)"
                    else (issue_key_from_text(choice) or choice.split(" ", 1)[0])
                )
            self._rebuild_rows()

    def _reporter_payload(self) -> dict[str, object] | None:
        value = self.values["reporter"].strip()
        if not value:
            return None
        if value == self._default_reporter and self._default_reporter_account_id:
            return {"accountId": self._default_reporter_account_id}
        return user_payload(self._jira_dir, value)

    @work
    async def action_create(self) -> None:
        if not self.selected_type:
            self.notify("issue type is required", severity="warning")
            return
        summary = self.values["summary"].strip()
        if not summary:
            self.notify("summary is required", severity="warning")
            return
        description = self.values["description"].strip()
        if not description:
            self.notify("description is required", severity="warning")
            return

        create_fields = self._create_fields()
        parent = self.values["parent"].strip() or None
        confirm_message = f"Create {self.selected_type} '{summary}'" + (f" under {parent}" if parent else "") + "?"
        confirmed = await self.app.push_screen_wait(ConfirmScreen(confirm_message))
        if not confirmed:
            return

        try:
            assignee_value = self.values["assignee"].strip()
            assignee = user_payload(self._jira_dir, assignee_value) if assignee_value else None
            fields = build_create_fields(
                project=self._project,
                issue_type=self.selected_type,
                summary=summary,
                description=description,
                component_field=self._component_field,
                component=self.values["component"].strip() or None,
                parent=parent,
                priority=self.values["priority"].strip() or None,
                fix_version=self.values["version"].strip() or None,
                labels=comma_parts(self.values["labels"]) or None,
                assignee=assignee,
                create_fields=create_fields,
                reporter=self._reporter_payload(),
            )
            key = create_issue(self._client, fields)
        except (IssueError, ShadowError) as exc:
            self.notify(f"create failed: {exc}", severity="error")
            return

        try:
            refresh_local_issue_after_push(self._jira_dir, key, self._client, self._component_field)
        except ShadowError as exc:
            self.notify(f"created {key}, but local refresh failed: {exc}", severity="warning")

        self.dismiss(key)

    def action_cancel(self) -> None:
        self.dismiss(None)


def display_name_or_empty(item: dict[str, Any], field: str) -> str:
    value = item.get(field)
    return value.strip() if isinstance(value, str) else ""
