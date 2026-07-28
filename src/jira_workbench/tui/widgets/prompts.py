from __future__ import annotations

from collections.abc import Callable

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, SelectionList, Static, TextArea
from textual.widgets.option_list import Option


class TextPromptScreen(ModalScreen[str | None]):
    """A single-line labeled text input modal. Returns the entered text, or None if cancelled."""

    DEFAULT_CSS = """
    TextPromptScreen {
        align: center middle;
    }
    TextPromptScreen > Vertical {
        width: 60%;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $panel;
    }
    TextPromptScreen .title {
        text-style: bold;
    }
    TextPromptScreen .dialog-buttons {
        margin-top: 1;
        height: auto;
        align-horizontal: right;
    }
    TextPromptScreen .dialog-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, label: str, *, initial: str = "") -> None:
        super().__init__()
        self._label = label
        self._initial = initial

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._label, classes="title")
            yield Input(value=self._initial, id="prompt-input")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Save (Enter)", id="save-button", variant="primary")
                yield Button("Cancel (Esc)", id="cancel-button")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def _submit_value(self, value: str) -> None:
        self.dismiss(value.strip() or None)

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        self._submit_value(event.value)

    @on(Button.Pressed, "#save-button")
    def _save_pressed(self) -> None:
        self._submit_value(self.query_one(Input).value)

    @on(Button.Pressed, "#cancel-button")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no confirmation modal."""

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    ConfirmScreen > Vertical {
        width: 60%;
        height: auto;
        border: round $warning;
        padding: 1 2;
        background: $panel;
    }
    ConfirmScreen .title {
        text-style: bold;
    }
    ConfirmScreen .dialog-buttons {
        margin-top: 1;
        height: auto;
        align-horizontal: right;
    }
    ConfirmScreen .dialog-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        ("y", "confirm", "Yes"),
        ("n", "cancel", "No"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._message, classes="title")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Confirm (Y)", id="confirm-button", variant="primary")
                yield Button("Cancel (N)", id="cancel-button")

    @on(Button.Pressed, "#confirm-button")
    def _confirm_pressed(self) -> None:
        self.action_confirm()

    @on(Button.Pressed, "#cancel-button")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class OptionPickerScreen(ModalScreen[str | None]):
    """Filterable single-select list. Returns the chosen option's text, or None if cancelled."""

    DEFAULT_CSS = """
    OptionPickerScreen {
        align: center middle;
    }
    OptionPickerScreen > Vertical {
        width: 70%;
        height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $panel;
    }
    OptionPickerScreen .title {
        text-style: bold;
    }
    OptionPickerScreen OptionList {
        height: 1fr;
        margin-top: 1;
    }
    OptionPickerScreen .dialog-buttons {
        margin-top: 1;
        height: auto;
        align-horizontal: right;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        label: str,
        options: list[str],
        *,
        current: str | None = None,
        render: Callable[[str], object] | None = None,
    ) -> None:
        super().__init__()
        self._label = label
        self._options = options
        self._current = current
        # Lets a caller show a richer prompt (e.g. an icon-prefixed label)
        # while the picker still filters/returns the plain option string --
        # Option's `id` is the round-trip identity, independent of `prompt`
        # (what's actually displayed).
        self._render_option = render or (lambda option: option)

    def _make_option(self, option: str) -> Option:
        return Option(self._render_option(option), id=option)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._label, classes="title")
            yield Input(placeholder="type to filter", id="picker-filter")
            yield OptionList(*(self._make_option(option) for option in self._options), id="picker-options")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel (Esc)", id="cancel-button")

    def on_mount(self) -> None:
        self.query_one("#picker-filter", Input).focus()
        options = self.query_one(OptionList)
        if self._current in self._options:
            options.highlighted = self._options.index(self._current)

    @on(Input.Changed, "#picker-filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        needle = event.value.strip().lower()
        options = self.query_one(OptionList)
        options.clear_options()
        for option in self._options:
            if needle in option.lower():
                options.add_option(self._make_option(option))

    @on(Input.Submitted, "#picker-filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        options = self.query_one(OptionList)
        if options.option_count:
            self.dismiss(options.get_option_at_index(0).id)

    @on(OptionList.OptionSelected)
    def _option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_id)

    @on(Button.Pressed, "#cancel-button")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


class LabelsPickerScreen(ModalScreen[list[str] | None]):
    """Multi-select label picker: toggle known labels with Space (a checkbox
    list, not free text) -- avoids accidentally minting a new label via a
    typo. Adding a genuinely new one is only possible through the separate
    input below the list, so doing that is always a deliberate, visible act.

    Returns the selected labels, or None if cancelled.
    """

    DEFAULT_CSS = """
    LabelsPickerScreen {
        align: center middle;
    }
    LabelsPickerScreen > Vertical {
        width: 70%;
        height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $panel;
    }
    LabelsPickerScreen .title {
        text-style: bold;
    }
    LabelsPickerScreen SelectionList {
        height: 1fr;
        margin-top: 1;
    }
    LabelsPickerScreen #new-label-input {
        margin-top: 1;
    }
    LabelsPickerScreen .dialog-buttons {
        margin-top: 1;
        height: auto;
        align-horizontal: right;
    }
    LabelsPickerScreen .dialog-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel"), ("ctrl+s", "save", "Save")]

    def __init__(self, known_labels: list[str], *, selected: list[str]) -> None:
        super().__init__()
        self._selected = [label for label in selected if label]
        self._known = sorted({*known_labels, *self._selected}, key=str.lower)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Labels:", classes="title")
            yield SelectionList(*[(label, label, label in self._selected) for label in self._known], id="labels-list")
            yield Input(placeholder="add a new label (Enter)", id="new-label-input")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Save (Ctrl+S)", id="save-button", variant="primary")
                yield Button("Cancel (Esc)", id="cancel-button")

    def on_mount(self) -> None:
        # Focused on the "add a new label" input by default, same convention
        # as OptionPickerScreen's own filter input -- plain typing goes
        # straight to adding a new label; Tab or a click reaches the
        # checklist to toggle an existing one.
        self.query_one("#new-label-input", Input).focus()

    @on(Input.Submitted, "#new-label-input")
    def _add_new_label(self, event: Input.Submitted) -> None:
        self._commit_new_label_input(event.value)

    def _commit_new_label_input(self, value: str) -> None:
        stripped = value.strip()
        input_widget = self.query_one("#new-label-input", Input)
        input_widget.value = ""
        if not stripped:
            return
        selection_list = self.query_one(SelectionList)
        if stripped in self._known:
            if stripped not in selection_list.selected:
                selection_list.select(stripped)
            return
        self._known.append(stripped)
        selection_list.add_option((stripped, stripped, True))

    @on(Button.Pressed, "#save-button")
    def _save_pressed(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel-button")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_save(self) -> None:
        # Commit whatever's still sitting in the "add a new label" input --
        # otherwise pressing Ctrl+S (or the Save button) right after typing a
        # new label, without pressing Enter first, would silently drop it.
        self._commit_new_label_input(self.query_one("#new-label-input", Input).value)
        self.dismiss(list(self.query_one(SelectionList).selected))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmWithInputScreen(ModalScreen[str | None]):
    """Confirmation with an optional inline text field, submitted together.

    Returns None if cancelled, or the (possibly empty) input text if confirmed
    -- callers distinguish "confirmed with no value" (empty string) from
    "cancelled" (None), instead of chaining a separate confirm-then-prompt pair.
    """

    DEFAULT_CSS = """
    ConfirmWithInputScreen {
        align: center middle;
    }
    ConfirmWithInputScreen > Vertical {
        width: 60%;
        height: auto;
        border: round $warning;
        padding: 1 2;
        background: $panel;
    }
    ConfirmWithInputScreen .title {
        text-style: bold;
    }
    ConfirmWithInputScreen .label {
        margin-top: 1;
    }
    ConfirmWithInputScreen .dialog-buttons {
        margin-top: 1;
        height: auto;
        align-horizontal: right;
    }
    ConfirmWithInputScreen .dialog-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, message: str, *, input_label: str, initial: str = "") -> None:
        super().__init__()
        self._message = message
        self._input_label = input_label
        self._initial = initial

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._message, classes="title")
            yield Static(self._input_label, classes="label")
            yield Input(value=self._initial, id="confirm-input")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Save (Enter)", id="save-button", variant="primary")
                yield Button("Cancel (Esc)", id="cancel-button")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def _submit_value(self, value: str) -> None:
        self.dismiss(value.strip())

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        self._submit_value(event.value)

    @on(Button.Pressed, "#save-button")
    def _save_pressed(self) -> None:
        self._submit_value(self.query_one(Input).value)

    @on(Button.Pressed, "#cancel-button")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


class StatusChangeScreen(ModalScreen[tuple[str, str | None] | None]):
    """Pick a status and, if it lands on a Done-like state, its resolution -- in one screen.

    Replaces what used to be two sequential modal pushes (status picker, then a
    second resolution picker) with a single modal whose resolution list only
    appears once a Done-like status is highlighted.
    """

    DEFAULT_CSS = """
    StatusChangeScreen {
        align: center middle;
    }
    StatusChangeScreen > Vertical {
        width: 70%;
        height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $panel;
    }
    StatusChangeScreen .title {
        text-style: bold;
    }
    StatusChangeScreen OptionList {
        height: 1fr;
        margin-top: 1;
    }
    StatusChangeScreen .dialog-buttons {
        margin-top: 1;
        height: auto;
        align-horizontal: right;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        statuses: list[str],
        resolutions: list[str],
        *,
        current_status: str | None = None,
        is_done_status: Callable[[str], bool],
    ) -> None:
        super().__init__()
        self._statuses = statuses
        self._resolutions = resolutions
        self._current_status = current_status
        self._is_done_status = is_done_status
        self._chosen_status: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Status:", classes="title")
            yield OptionList(*self._statuses, id="status-options")
            yield Static("Resolution (optional):", id="resolution-label")
            yield OptionList("(none)", *self._resolutions, id="resolution-options")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel (Esc)", id="cancel-button")

    def on_mount(self) -> None:
        status_options = self.query_one("#status-options", OptionList)
        if self._current_status in self._statuses:
            status_options.highlighted = self._statuses.index(self._current_status)
        status_options.focus()
        self._sync_resolution_visibility(self._current_status or "")

    def _sync_resolution_visibility(self, status: str) -> None:
        show = self._is_done_status(status)
        self.query_one("#resolution-label", Static).display = show
        self.query_one("#resolution-options", OptionList).display = show

    @on(OptionList.OptionHighlighted, "#status-options")
    def _status_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._sync_resolution_visibility(str(event.option.prompt))

    @on(OptionList.OptionSelected, "#status-options")
    def _status_selected(self, event: OptionList.OptionSelected) -> None:
        status = str(event.option.prompt)
        self._chosen_status = status
        self._sync_resolution_visibility(status)
        if self._is_done_status(status):
            self.query_one("#resolution-options", OptionList).focus()
        else:
            self.dismiss((status, None))

    @on(OptionList.OptionSelected, "#resolution-options")
    def _resolution_selected(self, event: OptionList.OptionSelected) -> None:
        if self._chosen_status is None:
            return
        resolution = str(event.option.prompt)
        self.dismiss((self._chosen_status, None if resolution == "(none)" else resolution))

    @on(Button.Pressed, "#cancel-button")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


class TextAreaPromptScreen(ModalScreen[str | None]):
    """A multiline text editor modal (Description/Comment), backed by Textual's TextArea."""

    DEFAULT_CSS = """
    TextAreaPromptScreen {
        align: center middle;
    }
    TextAreaPromptScreen > Vertical {
        width: 80%;
        height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $panel;
    }
    TextAreaPromptScreen .title {
        text-style: bold;
    }
    TextAreaPromptScreen TextArea {
        height: 1fr;
        margin-top: 1;
    }
    TextAreaPromptScreen .dialog-buttons {
        margin-top: 1;
        height: auto;
        align-horizontal: right;
    }
    TextAreaPromptScreen .dialog-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel"), ("ctrl+s", "save", "Save")]

    def __init__(self, label: str, *, initial: str = "") -> None:
        super().__init__()
        self._label = label
        self._initial = initial

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._label, classes="title")
            yield TextArea(self._initial, id="prompt-textarea")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Save (Ctrl+S)", id="save-button", variant="primary")
                yield Button("Cancel (Esc)", id="cancel-button")

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()

    @on(Button.Pressed, "#save-button")
    def _save_pressed(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel-button")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_save(self) -> None:
        self.dismiss(self.query_one(TextArea).text)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TextViewScreen(ModalScreen[str | None]):
    """Scrollable viewer for long-form field text (Description, Comments), which
    can switch to editing in place for fields that support it.

    Opens read-only (via Textual's TextArea in read-only mode, so long content
    gets real scrolling, selection, and copy instead of being silently
    truncated). If `editable`, an "Edit" button turns the same TextArea
    editable and swaps itself to "Save" -- no need to close the viewer and
    re-open a separate editor just to make a change after reading it.

    Returns None if closed without saving (whether or not editing was ever
    started), or the edited text if Save was used.
    """

    DEFAULT_CSS = """
    TextViewScreen {
        align: center middle;
    }
    TextViewScreen > Vertical {
        width: 80%;
        height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $panel;
    }
    TextViewScreen .title {
        text-style: bold;
    }
    TextViewScreen TextArea {
        height: 1fr;
        margin-top: 1;
    }
    TextViewScreen .dialog-buttons {
        margin-top: 1;
        height: auto;
        align-horizontal: right;
    }
    TextViewScreen .dialog-buttons Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        ("escape", "close", "Close"),
        ("q", "close", "Close"),
        ("e", "start_edit", "Edit"),
        ("ctrl+s", "save", "Save"),
    ]

    def __init__(self, label: str, text: str, *, editable: bool = False) -> None:
        super().__init__()
        self._label = label
        self._text = text
        self._editable = editable
        self._editing = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._label, classes="title")
            yield TextArea(self._text, read_only=True, id="view-textarea")
            with Horizontal(classes="dialog-buttons"):
                if self._editable:
                    yield Button("Edit", id="primary-button")
                yield Button("Close (Esc)", id="close-button", variant="primary")

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()

    @on(Button.Pressed, "#primary-button")
    def _primary_pressed(self) -> None:
        if self._editing:
            self._save()
        else:
            self._start_edit()

    @on(Button.Pressed, "#close-button")
    def _close_pressed(self) -> None:
        self.action_close()

    def _start_edit(self) -> None:
        if not self._editable or self._editing:
            return
        self._editing = True
        text_area = self.query_one(TextArea)
        text_area.read_only = False
        text_area.focus()
        # Close stays the emphasized/default button even while editing --
        # Save doesn't get the primary variant, so leaving is still the "easy" action.
        self.query_one("#primary-button", Button).label = "Save (Ctrl+S)"

    def _save(self) -> None:
        self.dismiss(self.query_one(TextArea).text)

    def action_start_edit(self) -> None:
        self._start_edit()

    def action_save(self) -> None:
        if self._editing:
            self._save()

    def action_close(self) -> None:
        self.dismiss(None)
