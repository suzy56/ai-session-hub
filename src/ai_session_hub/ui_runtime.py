from __future__ import annotations

import datetime
import time

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Label, TextArea

from ai_session_hub.i18n import tr
from ai_session_hub.runtime_monitor import CollectionIssue, RuntimeSnapshot


class RuntimeView(Vertical):
    """Read-only rendering of a single, immutable runtime observation."""

    def __init__(self, language: str):
        super().__init__(id="runtime-view")
        self.language = language
        self.snapshot: RuntimeSnapshot | None = None
        self.selected_identity: tuple[int, str] | None = None
        self._selection_initialized = False
        self._detail_identity: tuple[int, str] | None = None
        self.scanning = False
        self.failure: str | None = None
        self._row_identities: dict[str, tuple[int, str]] = {}

    def t(self, key: str, **values) -> str:
        return tr(self.language, key, **values)

    def compose(self) -> ComposeResult:
        with Horizontal(id="runtime-toolbar"):
            yield Label("", id="runtime-status")
            yield Button(self.t("Refresh"), id="runtime-refresh")
        with Horizontal(id="runtime-content"):
            yield DataTable(id="runtime-processes", cursor_type="row")
            yield TextArea(id="runtime-details", read_only=True, show_line_numbers=False, soft_wrap=True)

    def on_mount(self) -> None:
        self.render_snapshot()

    def render_snapshot(self) -> None:
        table = self.query_one("#runtime-processes", DataTable)
        position = table.scroll_offset
        records = self.snapshot.processes if self.snapshot else ()
        identities = {row.process.identity for row in records}
        if self.selected_identity not in identities:
            # Do not silently replace a disappeared process with another PID.
            self.selected_identity = records[0].process.identity if not self._selection_initialized and records else None
        if records:
            self._selection_initialized = True
        self._row_identities.clear()
        with table.prevent(DataTable.RowHighlighted):
            table.clear(columns=True)
            for label, width in (("PID", 7), ("Program", 14), ("Elapsed", 12)):
                table.add_column(self.t(label), width=width)
            for index, row in enumerate(records):
                process = row.process
                key = str(index)
                self._row_identities[key] = process.identity
                program = process.executable.rsplit("/", 1)[-1]
                table.add_row(str(process.pid), Text(program, no_wrap=True), Text(process.elapsed), key=key)
            if self.selected_identity is not None:
                table.move_cursor(row=next(i for i, row in enumerate(records) if row.process.identity == self.selected_identity))
        table.scroll_to(x=position.x, y=position.y, animate=False)
        self.query_one("#runtime-refresh", Button).label = self.t("Refresh")
        self.render_status()
        self.render_details()

    def render_status(self) -> None:
        snapshot = self.snapshot
        parts = []
        if self.scanning:
            parts.append(self.t("Collecting…"))
        if self.failure:
            parts.append(self.t("Collection failed"))
        if snapshot:
            partial = not snapshot.process_complete or bool(snapshot.issues) or any(
                not row.network_complete or not row.ancestry_complete or row.issues or any(ccb.issues for ccb in row.ccb)
                for row in snapshot.processes
            )
            parts.append(self.t("Partial") if partial else self.t("Complete"))
            stamp = datetime.datetime.fromtimestamp(snapshot.collected_at).astimezone().strftime("%H:%M:%S")
            age = max(0, int(time.time() - snapshot.collected_at))
            parts.append(self.t("Collected {time} · {age}s ago", time=stamp, age=age))
            if age >= 6 or self.failure:
                parts.append(self.t("Stale"))
        elif not self.scanning and not self.failure:
            parts.append(self.t("Not collected"))
        self.query_one("#runtime-status", Label).update(Text(" · ".join(parts), no_wrap=True, overflow="ellipsis"))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "runtime-processes":
            return
        self.selected_identity = self._row_identities.get(str(event.row_key.value))
        self.render_details()
        event.stop()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "runtime-processes":
            self.selected_identity = self._row_identities.get(str(event.row_key.value))
            self.render_details()
            self.query_one("#runtime-details", TextArea).focus()
            event.stop()

    def render_details(self) -> None:
        lines: list[str] = []
        snapshot = self.snapshot
        selected = next((row for row in snapshot.processes if row.process.identity == self.selected_identity), None) if snapshot else None

        def field(label: str, value) -> None:
            lines.append(f"{self.t(label)}: {value if value is not None else self.t('Unavailable')}")

        def issues(items: tuple[CollectionIssue, ...]) -> None:
            for issue in items:
                pid = f" · PID {issue.pid}" if issue.pid is not None else ""
                lines.append(f"{issue.source} / {self.t(issue.code)}{pid}: {issue.message}")

        if selected:
            process = selected.process
            field("PID", process.pid)
            field("Started", process.started)
            field("Elapsed", process.elapsed)
            field("Program", process.executable)
            field("Working directory", process.cwd)
            field("Command", process.command)
            field("Direct PPID", process.ppid)
            lines += ["", self.t("Observed ancestors · direct parent first")]
            if not selected.ancestry_complete:
                lines.append(self.t("Incomplete ancestry; original launcher is not inferred."))
            for ancestor in selected.ancestors:
                lines.append(f"PID {ancestor.pid} · {ancestor.executable}\n{ancestor.command}")
            if not selected.ancestors:
                lines.append(self.t("No ancestors observed"))
            lines += ["", self.t("PID-scoped sockets · not request history")]
            if not selected.network_complete:
                lines.append(self.t("Socket collection unavailable or partial"))
            if not selected.connections and selected.network_complete:
                lines.append(self.t("No sockets observed in this sample"))
            for connection in selected.connections:
                # Never render a socket as belonging to a different selected PID.
                if connection.pid != process.pid:
                    lines.append(self.t("Conflicting socket PID; record omitted"))
                    continue
                state = connection.state or self.t("No state reported")
                remote = connection.remote_address or self.t("No remote endpoint")
                lines.append(f"PID {connection.pid} · {connection.protocol} · {state}\n{connection.local_address} → {remote}")
            lines += ["", self.t("CCB evidence · separate from OS parentage")]
            if not selected.ccb:
                lines.append(self.t("No CCB ownership evidence observed"))
            for ccb in selected.ccb:
                field("Project", ccb.project)
                field("Ownership evidence", self.t("Verified") if ccb.verified else self.t("Unverified"))
                field("tmux session", ccb.session)
                field("tmux pane", ccb.pane)
                clients = ccb.attached_clients
                field("Attached clients", f"{clients} · {self.t('Detached, not stopped')}" if clients == 0 else clients)
                for label, value in (("Desired state", ccb.desired_state), ("Phase", ccb.phase),
                                     ("Shutdown intent", ccb.shutdown_intent), ("Mount state", ccb.mount_state)):
                    field(label, value)
                lines.append(self.t("Controller references · persisted PID alone is not proof"))
                for controller in ccb.controllers:
                    field(controller.role, f"PID {controller.pid} · {self.t('Verified') if controller.verified else self.t('Unverified')}")
                issues(ccb.issues)
                lines.append("")
            if selected.issues:
                lines.append(self.t("Process source issues"))
                issues(selected.issues)
        elif snapshot and not snapshot.processes:
            lines.append(self.t("No local Claude processes observed.") if snapshot.process_complete else self.t("Process discovery incomplete; absence is not verified."))
        else:
            lines.append(self.t("Select a process to inspect its current evidence."))
        if self.failure:
            lines += ["", self.t("Collection failed"), self.failure]
        if snapshot and snapshot.issues:
            lines += ["", self.t("Collection source issues")]
            issues(snapshot.issues)
        area = self.query_one("#runtime-details", TextArea)
        body = "\n".join(lines)
        if body != area.text:
            position = area.scroll_offset if self._detail_identity == self.selected_identity else None
            area.text = body
            if position is not None:
                area.scroll_to(x=position.x, y=position.y, animate=False, immediate=True)
        self._detail_identity = self.selected_identity
