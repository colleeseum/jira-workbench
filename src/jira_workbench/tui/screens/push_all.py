from __future__ import annotations

from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, RichLog, Static

from ...shadow import PushResult, push_shadows


class PushAllProgressScreen(ModalScreen[PushResult | None]):
    """Pushes committed shadow changes for multiple items, streaming progress live.

    push_shadows() runs synchronously and can take one Jira API round trip per
    item, so it is offloaded to a worker thread; progress lines are marshalled
    back to the UI thread via call_from_thread since Textual widgets are not
    thread-safe to touch directly from a worker.
    """

    DEFAULT_CSS = """
    PushAllProgressScreen {
        align: center middle;
    }
    PushAllProgressScreen > Vertical {
        width: 80%;
        height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $panel;
    }
    PushAllProgressScreen .title {
        text-style: bold;
    }
    PushAllProgressScreen RichLog {
        height: 1fr;
        margin-top: 1;
        border: round $primary;
    }
    PushAllProgressScreen .hint {
        color: $text-muted;
    }
    PushAllProgressScreen .dialog-buttons {
        margin-top: 1;
        height: auto;
        align-horizontal: right;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("enter", "close", "Close"),
    ]

    def __init__(self, jira_dir: Path, keys: list[str], client: Any, component_field: str) -> None:
        super().__init__()
        self._jira_dir = jira_dir
        self._keys = keys
        self._client = client
        self._component_field = component_field
        self._done = False
        self._result: PushResult | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"Pushing {len(self._keys)} item(s)...", classes="title", id="push-all-title")
            yield RichLog(id="push-all-log", wrap=True, markup=False)
            yield Static("Working, please wait...", classes="hint", id="push-all-hint")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Close (Esc)", id="close-button", variant="primary")

    def on_mount(self) -> None:
        self._run_push()

    @on(Button.Pressed, "#close-button")
    def _close_pressed(self) -> None:
        self.action_close()

    @work(thread=True)
    def _run_push(self) -> None:
        log = self.query_one(RichLog)

        def progress(line: str) -> None:
            self.app.call_from_thread(log.write, line)

        try:
            result = push_shadows(
                self._jira_dir,
                self._keys,
                self._client,
                progress=progress,
                component_field=self._component_field,
            )
        except Exception as exc:
            self.app.call_from_thread(self._finish, None, f"push all failed: {exc}")
            return
        summary = f"pushed={result.pushed} skipped={result.skipped} blocked={result.blocked} failed={result.failed}"
        self.app.call_from_thread(self._finish, result, summary)

    def _finish(self, result: PushResult | None, summary: str) -> None:
        self._done = True
        self._result = result
        self.query_one(RichLog).write(summary)
        self.query_one("#push-all-title", Static).update("Push complete")
        self.query_one("#push-all-hint", Static).update("Done.")

    def action_close(self) -> None:
        if not self._done:
            self.notify("still pushing, please wait...")
            return
        self.dismiss(self._result)
