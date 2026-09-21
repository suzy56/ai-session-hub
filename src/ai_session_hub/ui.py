from __future__ import annotations

import datetime
import json
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, DataTable, Input, Label, Static, TextArea, Tree
from textual.widgets.tree import TreeNode
from textual.worker import WorkerCancelled, get_current_worker

from ai_session_hub.config import AppConfig, load_config
from ai_session_hub.i18n import load_language, save_language, tr
from ai_session_hub.indexer import IndexProgress, Indexer
from ai_session_hub.models import LaunchSpec, SessionSnapshot, Unavailable
from ai_session_hub.resume import prepare_resume, run_foreground
from ai_session_hub.source_io import clean_display_text
from ai_session_hub.store import SearchFilters, SearchPage, SearchResult, Store
from ai_session_hub.ui_dialogs import FilterScreen, FilterSelection, InfoScreen, SettingsScreen
from ai_session_hub.ui_layout import PLATFORM_LABELS, compose_hub_widgets
from rich.cells import cell_len
from ai_session_hub.usage import AnalyticsSnapshot, UsageSummary
from ai_session_hub.usage_view import (
    calculate_streak,
    compact_cost,
    compact_tokens,
    cost_text,
    horizontal_bar,
    render_calendar_heatmap,
    render_vertical_bar_chart,
    token_text,
    trend_text,
)


def _pad_cell(s: str, width: int, align: str = "left") -> str:
    clen = cell_len(s)
    if clen > width:
        cur = ""
        for ch in s:
            if cell_len(cur + ch) > width - 1:
                break
            cur += ch
        cur += "…"
        clen = cell_len(cur)
    spaces = " " * max(0, width - clen)
    return s + spaces if align == "left" else spaces + s


def _format_project_label(name: str, tokens: int | None, cost: float | None, sessions: int, share: str, path: str) -> Text:
    col_name = _pad_cell(name, 26, "left")
    col_tokens = _pad_cell(compact_tokens(tokens), 10, "right")
    col_cost = _pad_cell(compact_cost(cost), 10, "right")
    col_sess = _pad_cell(str(sessions), 7, "right")
    col_share = _pad_cell(share, 7, "right")
    res = Text()
    res.append(col_name, style="bold")
    res.append("  ")
    res.append(col_tokens, style="#80b6af")
    res.append("  ")
    res.append(col_cost, style="#d6d8da")
    res.append("  ")
    res.append(col_sess, style="#92989e")
    res.append("  ")
    res.append(col_share, style="#80b6af")
    if path:
        res.append("  ")
        res.append(path, style="dim")
    return res

def format_timestamp(ms: int | None) -> str:
    """Format UTC millisecond timestamp in local time."""
    if ms is None or ms <= 0:
        return "—"
    try:
        return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return "—"


class ResumeConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, tool: str, native_id: str, cwd: Path, profile: str | None, language: str = "en"):
        super().__init__()
        self.tool, self.native_id, self.cwd, self.profile = tool, native_id, cwd, profile
        self.language = language

    def compose(self) -> ComposeResult:
        t = lambda key: tr(self.language, key)
        with Vertical(id="confirm-dialog", classes="hub-dialog"):
            yield Label(t("Resume session?"), classes="dialog-title")
            for key, value in (("Tool", self.tool), ("Resume target", self.native_id),
                               ("Working directory", str(self.cwd)), ("Profile", self.profile or t("Default"))):
                yield Label(Text(f"{t(key)}: {clean_display_text(value)}"), classes="confirm-row")
            yield Label(t("The native CLI takes over this terminal. Exit it to return here."))
            with Horizontal(classes="dialog-actions"):
                yield Button(t("Cancel"), id="btn-cancel")
                yield Button(t("Resume"), id="btn-confirm", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class SearchResultsReady(Message):
    def __init__(self, generation: int, page: SearchPage, usage: dict[str, UsageSummary] | None = None):
        super().__init__()
        self.generation, self.page, self.usage = generation, page, usage or {}


class AnalyticsReady(Message):
    def __init__(self, generation: int, snapshot: AnalyticsSnapshot, projects: tuple[tuple[str, str], ...]):
        super().__init__()
        self.generation, self.snapshot, self.projects = generation, snapshot, projects


class SessionDetailReady(Message):
    def __init__(self, session_key: str, snapshot: SessionSnapshot | None, usage: UsageSummary,
                 view: str, generation: int):
        super().__init__()
        self.session_key, self.snapshot, self.usage = session_key, snapshot, usage
        self.view, self.generation = view, generation


class ProjectPageReady(Message):
    def __init__(self, generation: int, project_key: str, page: SearchPage | None,
                 usage: dict[str, UsageSummary], error: str | None = None):
        super().__init__()
        self.generation, self.project_key, self.page = generation, project_key, page
        self.usage, self.error = usage, error


class ScanStatusUpdate(Message):
    def __init__(self, progress: IndexProgress):
        super().__init__()
        self.progress = progress


class QueryFailed(Message):
    def __init__(self, generation: int, error: str):
        super().__init__()
        self.generation, self.error = generation, error


class ScanFinished(Message):
    pass


class HubApp(App):
    """A local, keyboard-first history workspace; displayed labels never identify data."""

    CSS_PATH = "ui.tcss"
    BINDINGS = [
        Binding("/", "focus_search", show=False),
        Binding("escape", "cancel_or_table", show=False),
        Binding("f", "filters", show=False),
        Binding("comma", "settings", show=False),
        Binding("i", "details", show=False),
        Binding("r", "resume_session", show=False),
        Binding("ctrl+r", "refresh_index", show=False),
        Binding("q", "quit_app", show=False),
        Binding("ctrl+q", "force_quit", show=False, priority=True),
        Binding("pageup", "preview_page_up", show=False),
        Binding("pagedown", "preview_page_down", show=False),
        Binding("[", "prev_page", show=False),
        Binding("]", "next_page", show=False),
        Binding("f1", "show_dashboard", show=False),
        Binding("f2", "show_sessions", show=False),
        Binding("f3", "show_projects", show=False),
        Binding("?", "help", show=False),
    ]
    _WORKSPACE_ACTIONS = frozenset(binding.action for binding in BINDINGS)

    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.store = Store(config.data_dir / "index.sqlite3")
        self.language, self._language_warning = load_language(config.data_dir)
        self.current_generation = 0
        self.page_offset, self.page_size = 0, 100
        self.current_results: list[SearchResult] = []
        self.selected_session_key: str | None = None
        self._selections: dict[str, str | None] = {"sessions-view": None, "projects-view": None}
        self._details: dict[str, tuple[SessionSnapshot, UsageSummary]] = {}
        self._detail_generations = {"sessions-view": 0, "projects-view": 0}
        self._preview_keys: dict[str, str] = {}
        self._filter_selection = FilterSelection()
        self._platform_options: tuple[tuple[str, str], ...] = ()
        self._project_options: tuple[tuple[str, str], ...] = ()
        self._analytics_generation = 0
        self._analytics = AnalyticsSnapshot()
        self._session_usage: dict[str, UsageSummary] = {}
        self._search_total = 0
        self._cancel_scan = self._is_indexing = self._closing = self._handoff = False
        self._index_worker = None
        self._scan_progress = IndexProgress(status="")
        self._scan_error: str | None = None
        self._debounce_timer = None
        self._project_generation = 0
        self._project_nodes: dict[str, TreeNode] = {}
        self._project_pages: dict[str, tuple[list[SearchResult], int, dict[str, UsageSummary]]] = {}
        self._project_loading: set[str] = set()
        self._rendering_projects = False

    def t(self, key: str, **values) -> str:
        return tr(self.language, key, **values)

    @property
    def current_view(self) -> str:
        return self.main_screen.query_one("#views", ContentSwitcher).current or "sessions-view"

    @property
    def main_screen(self):
        return self.screen_stack[0]

    def on_unmount(self) -> None:
        self._closing = self._cancel_scan = True
        self.store.close()

    def compose(self) -> ComposeResult:
        yield from compose_hub_widgets(self.language)

    def on_mount(self) -> None:
        self.main_screen.query_one("#project-tree", Tree).show_root = False
        self.main_screen.query_one("#project-tree", Tree).guide_depth = 2
        self._set_columns()
        self._update_platform_options()
        self.main_screen.set_class(self.size.width < 100, "narrow")
        self._show_view("sessions-view")
        self._render_scope()
        self.action_refresh_index()
        self._trigger_search()
        self._trigger_analytics()
        if self._language_warning:
            self.notify(clean_display_text(self._language_warning), title=self.t("Settings warning"), severity="warning", markup=False)
        if self.config.warnings:
            self.notify(clean_display_text("\n".join(self.config.warnings)), title=self.t("Source warnings"), severity="warning", timeout=10, markup=False)
        if not self.store.fts_available:
            self.notify(self.t("Search acceleration unavailable; literal search remains available."), severity="warning", markup=False)

    def _set_columns(self) -> None:
        specs = {
            "sessions": (("Time", 11), ("Tool", 7), ("Title", max(18, int(self.size.width * .45) - 27))),
            "model-table": (("Model / Provider", None), ("Bar", 12), ("Tokens", None), ("Share", None), ("Cost", None)),
            "tool-table": (("Tool", None), ("Bar", 12), ("Tokens", None), ("Share", None), ("Sessions", None)),
            "daily-table": (("Date", None), ("Tokens", None), ("Trend", None), ("Cost", None), ("Share", None)),
        }
        for widget_id, columns in specs.items():
            table = self.main_screen.query_one(f"#{widget_id}", DataTable)
            cursor, position = table.cursor_row, table.scroll_offset
            table.clear(columns=True)
            for label, width in columns:
                if widget_id == "sessions" and label == "Title" and self.size.width < 100:
                    width = max(20, self.size.width - 27)
                table.add_column(self.t(label), key=label, width=width)
            self.call_after_refresh(table.move_cursor, row=cursor)
            self.call_after_refresh(table.scroll_to, x=position.x, y=position.y, animate=False)

    def on_resize(self) -> None:
        if not self.is_mounted:
            return
        self.main_screen.set_class(self.size.width < 100, "narrow")
        if self.size.width < 100 and isinstance(self.focused, TextArea):
            self.main_screen.add_class("reading")
        self._set_columns()
        self._render_sessions()
        self._render_analytics()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action not in self._WORKSPACE_ACTIONS:
            return True
        if len(self.screen_stack) > 1:
            return action == "force_quit"
        if isinstance(self.focused, Input) and action not in {"cancel_or_table", "force_quit", "refresh_index", "show_dashboard", "show_sessions", "show_projects"}:
            return False
        return True

    def _show_view(self, view: str) -> None:
        self.main_screen.query_one("#views", ContentSwitcher).current = view
        self.main_screen.remove_class("reading")
        self.selected_session_key = self._selections.get(view)
        if view == "projects-view":
            node = self.main_screen.query_one("#project-tree", Tree).cursor_node
            if node is None or not node.data or node.data.get("type") != "session":
                self.selected_session_key = None
        for name in ("dashboard", "sessions", "projects"):
            self.main_screen.query_one(f"#nav-{name}", Button).variant = "primary" if view == f"{name}-view" else "default"
        self.main_screen.query_one({"dashboard-view": "#model-table", "sessions-view": "#sessions", "projects-view": "#project-tree"}[view]).focus()
        self._render_scope()
        self._render_hints()

    def action_show_dashboard(self) -> None:
        self._show_view("dashboard-view")

    def action_show_sessions(self) -> None:
        self._show_view("sessions-view")

    def action_show_projects(self) -> None:
        self._show_view("projects-view")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id
        if name in {"nav-dashboard", "nav-sessions", "nav-projects"}:
            self._show_view(name.removeprefix("nav-") + "-view")
        elif name == "dash-tab-overview":
            self.main_screen.query_one("#dash-switcher", ContentSwitcher).current = "dash-overview-pane"
            self.main_screen.query_one("#dash-tab-overview", Button).variant = "primary"
            self.main_screen.query_one("#dash-tab-trend", Button).variant = "default"
        elif name == "dash-tab-trend":
            self.main_screen.query_one("#dash-switcher", ContentSwitcher).current = "dash-trend-pane"
            self.main_screen.query_one("#dash-tab-trend", Button).variant = "primary"
            self.main_screen.query_one("#dash-tab-overview", Button).variant = "default"
        elif name == "nav-settings":
            self.action_settings()
        elif name == "open-filters":
            self.action_filters()
        elif name in {"preview-info", "project-preview-info"}:
            self.action_details()

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen(self.language), self._set_language)

    def _set_language(self, language: str | None) -> None:
        if language is None:
            return
        try:
            save_language(self.config.data_dir, language)
        except (OSError, ValueError) as err:
            self.notify(clean_display_text(str(err)), title=self.t("Could not save settings"), severity="error", markup=False)
            return
        if language == self.language:
            return
        self.language = language
        labels = {
            "nav-sessions": "Sessions", "nav-projects": "Projects", "nav-dashboard": "Dashboard", "nav-settings": "Settings",
            "open-filters": "Filters", "preview-info": "Details", "project-preview-info": "Details",
            "kpi-tokens-title": "Total tokens", "kpi-cost-title": "Known cost", "kpi-sessions-title": "Total sessions", "kpi-messages-title": "Total messages",
            "dash-tab-overview": "Overview", "dash-tab-trend": "Trend",
            "kpi-tokens-title": "Total tokens", "kpi-cost-title": "Known cost",
            "kpi-active-days-title": "Active days", "kpi-streak-title": "Consecutive days",
            "kpi-peak-day-title": "Peak single day", "kpi-top-model-title": "Top model",
            "kpi-messages-title": "Total messages", "kpi-sessions-title": "Total sessions",
            "heatmap-title": "Token Activity",
            "model-title": "Model breakdown", "tool-title": "Tool breakdown",
            "daily-title": "Daily activity", "trend-title": "Usage trend", "project-tree-header": "Projects",
        }
        for widget_id, key in labels.items():
            widget = self.main_screen.query_one(f"#{widget_id}")
            if isinstance(widget, Button):
                widget.label = self.t(key)
            else:
                widget.update(self.t(key))
        self.main_screen.query_one("#query", Input).placeholder = self.t("Search messages or title (/ to focus)")
        self._set_columns()
        self._render_sessions()
        self._render_analytics()
        self._render_projects()
        for view in ("sessions-view", "projects-view"):
            self._render_detail(view)
        self._render_scope()
        self._render_scan_status()
        self._render_hints()

    def action_filters(self) -> None:
        self.push_screen(FilterScreen(self.language, self._filter_selection, self._platform_options,
                                      tuple((self._project_display(key, label), key) for key, label in self._project_options)), self._apply_filters)

    def _apply_filters(self, selection: FilterSelection | None) -> None:
        if selection is None or selection == self._filter_selection:
            return
        self._filter_selection = selection
        self.page_offset = 0
        self._project_generation += 1
        self._project_pages.clear()
        self._project_loading.clear()
        self._project_nodes.clear()
        self.main_screen.query_one("#project-tree", Tree).clear()
        self._selections["projects-view"] = None
        self._details.pop("projects-view", None)
        self._render_detail("projects-view")
        if self.current_view == "projects-view":
            self.selected_session_key = None
        self._render_scope()
        self._trigger_search()
        self._trigger_analytics()

    def _get_current_filters(self) -> SearchFilters:
        choice = self._filter_selection
        seconds = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}.get(choice.time)
        bound = int(time.time() * 1000) - seconds * 1000 if seconds else None
        return SearchFilters(tool=choice.tool, project_key=choice.project_key, time_bound_ms=bound, kind=choice.kind)

    def _render_scope(self) -> None:
        choice = self._filter_selection
        parts = [PLATFORM_LABELS.get(choice.tool, choice.tool) if choice.tool else self.t("All tools")]
        if choice.project_key is not None:
            parts.append(Path(choice.project_key).name if choice.project_key else self.t("Unknown project"))
        parts.append(self.t({"24h": "Last 24 hours", "7d": "Last 7 days", "30d": "Last 30 days"}.get(choice.time, "All time")))
        if choice.kind:
            parts.append(self._state_label(choice.kind))
        self.main_screen.query_one("#scope-summary", Label).update(Text(clean_display_text(" · ".join(parts)), overflow="ellipsis", no_wrap=True))
        scope = "Usage time · totals include unknown coverage" if self.current_view == "dashboard-view" else "Activity time · session usage is lifetime"
        self.main_screen.query_one("#scope-summary", Label).tooltip = self.t(scope)

    def _render_hints(self) -> None:
        if self.main_screen.has_class("reading"):
            hint = self.t("Esc back   i details   r resume   Ctrl+C copy selection")
        elif self.current_view == "projects-view":
            hint = self.t("Enter open   f filters   r resume   , settings   ? help")
        elif self.current_view == "dashboard-view":
            hint = self.t("Tab next panel   f filters   F2 sessions   , settings   ? help")
        else:
            hint = self.t("/ search   f filters   Enter read   r resume   , settings   ? help")
        self.main_screen.query_one("#key-hints", Static).update(Text(hint, no_wrap=True, overflow="ellipsis"))

    def action_help(self) -> None:
        self.push_screen(InfoScreen(self.language, self.t(
            "F1 dashboard · F2 sessions · F3 projects\n/ search · f filters · , settings\nUp/Down select · Enter read or expand\nEsc close overlay or return to list\n[ previous page · ] next page\ni details · r confirm native resume\nCtrl+R refresh · q quit · Ctrl+Q always quit\nSelect text with mouse or Shift+arrows; Ctrl+C copies.\nConversation text, model names and paths are never translated."), "Keyboard help"))

    def _update_platform_options(self) -> None:
        clients = set(PLATFORM_LABELS)
        clients.update(row[0] for row in self.store.conn.execute("SELECT DISTINCT client FROM sessions"))
        self._platform_options = tuple((PLATFORM_LABELS.get(client, client), client) for client in sorted(clients))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "query":
            return
        self.page_offset = 0
        self.current_generation += 1
        if self._debounce_timer:
            self._debounce_timer.stop()
        self._debounce_timer = self.set_timer(.15, self._trigger_search)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "query":
            self.main_screen.query_one("#sessions", DataTable).focus()

    def _trigger_search(self) -> None:
        self.current_generation += 1
        self._run_search_worker(self.current_generation, self.main_screen.query_one("#query", Input).value.strip(),
                                self._get_current_filters(), self.page_offset, self.page_size)

    @work(thread=True, exclusive=True, group="search")
    def _run_search_worker(self, gen: int, query: str, filters: SearchFilters, offset: int, limit: int) -> None:
        worker = get_current_worker()
        def cancelled() -> bool:
            return worker.is_cancelled or self._closing or gen != self.current_generation
        try:
            with closing(Store(self.store.db_path, read_only=True)) as reader:
                reader.conn.set_progress_handler(cancelled, 1000)
                reader.conn.execute("BEGIN")
                page = reader.search(query, filters, offset, limit)
                usage = reader.session_usage([row.session_key for row in page.results], pricing=self.config.pricing)
            if not cancelled():
                self.post_message(SearchResultsReady(gen, page, usage))
        except Exception as err:
            if not cancelled():
                self.post_message(QueryFailed(gen, str(err)))

    def on_query_failed(self, message: QueryFailed) -> None:
        if message.generation == self.current_generation:
            self.notify(clean_display_text(message.error), title=self.t("Query failed"), severity="error", markup=False)

    def on_search_results_ready(self, message: SearchResultsReady) -> None:
        if message.generation != self.current_generation:
            return
        self.current_results = list(message.page.results)
        self._session_usage = message.usage
        self._search_total = message.page.total_count
        prior = self._selections["sessions-view"]
        selected = next((r.session_key for r in self.current_results if r.session_key == prior),
                        self.current_results[0].session_key if self.current_results else None)
        self._selections["sessions-view"] = selected
        if self.current_view == "sessions-view":
            self.selected_session_key = selected
        self._render_sessions()
        if selected:
            self._load_detail(selected, "sessions-view")
        else:
            self._details.pop("sessions-view", None)
            self._render_detail("sessions-view")

    def _render_sessions(self) -> None:
        table = self.main_screen.query_one("#sessions", DataTable)
        selected = self._selections["sessions-view"]
        position = table.scroll_offset
        with table.prevent(DataTable.RowHighlighted):
            table.clear()
            for row in self.current_results:
                title = ("> " if row.match_kind == "body" else "") + row.title
                stamp = format_timestamp(row.updated_ms)
                table.add_row(stamp[5:] if len(stamp) > 5 else stamp,
                              Text(clean_display_text(row.client), no_wrap=True),
                              Text(clean_display_text(title), no_wrap=True, overflow="ellipsis"), key=row.session_key)
            if self.current_results:
                table.move_cursor(row=next((i for i, r in enumerate(self.current_results) if r.session_key == selected), 0))
        table.scroll_to(x=position.x, y=position.y, animate=False)
        start = self.page_offset + 1 if self.current_results else 0
        self.main_screen.query_one("#session-page-status", Label).update(self.t(
            "{start}–{end} / {total}  ·  [ ] pages  ·  Activity time; lifetime usage",
            start=start, end=self.page_offset + len(self.current_results), total=self._search_total))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "sessions" or event.row_key.value is None:
            return
        key = str(event.row_key.value)
        self._selections["sessions-view"] = key
        if self.current_view == "sessions-view":
            self.selected_session_key = key
        self._load_detail(key, "sessions-view")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "sessions":
            self._read_preview()

    def _read_preview(self) -> None:
        if self.current_view not in self._selections or not self._selections[self.current_view]:
            return
        self.main_screen.add_class("reading")
        self.main_screen.query_one("#project-preview" if self.current_view == "projects-view" else "#preview", TextArea).focus()
        self._render_hints()

    def _load_detail(self, key: str, view: str) -> None:
        self._detail_generations[view] += 1
        self._run_detail_worker(key, view, self._detail_generations[view])

    @work(thread=True, group="detail")
    def _run_detail_worker(self, key: str, view: str, generation: int) -> None:
        worker = get_current_worker()
        def cancelled() -> bool:
            return worker.is_cancelled or self._closing or generation != self._detail_generations[view]
        try:
            with closing(Store(self.store.db_path, read_only=True)) as reader:
                reader.conn.set_progress_handler(cancelled, 1000)
                reader.conn.execute("BEGIN")
                snapshot = reader.get_session(key)
                usage = reader.session_usage([key], pricing=self.config.pricing).get(key, UsageSummary())
            if not cancelled():
                self.post_message(SessionDetailReady(key, snapshot, usage, view, generation))
        except Exception as err:
            if not cancelled():
                self.post_message(QueryFailed(self.current_generation, str(err)))

    def on_session_detail_ready(self, message: SessionDetailReady) -> None:
        if message.generation != self._detail_generations[message.view] or message.session_key != self._selections[message.view]:
            return
        if message.snapshot:
            self._details[message.view] = (message.snapshot, message.usage)
        else:
            self._details.pop(message.view, None)
        self._render_detail(message.view)

    def _state_label(self, value: str) -> str:
        return self.t({"native": "Native", "cached": "Cached", "partial": "Partial", "metadata-only": "Metadata only",
                       "unavailable": "Unavailable", "conversation": "Conversation", "subagent": "Subagent",
                       "advisor": "Advisor", "background-review": "Background review", "ide": "IDE",
                       "unknown": "Unknown", "archived": "Archived"}.get(value, value))

    def _render_detail(self, view: str) -> None:
        prefix = "project-preview" if view == "projects-view" else "preview"
        preview = self.main_screen.query_one(f"#{prefix}", TextArea)
        detail = self._details.get(view)
        if detail is None:
            self.main_screen.query_one(f"#{prefix}-header", Label).update(self.t("Conversation"))
            self.main_screen.query_one(f"#{prefix}-meta", Label).update("")
            preview.text = self.t("No matching conversations. Adjust the search or filters.") if view == "sessions-view" else self.t("Select a conversation in the project tree.")
            self._preview_keys.pop(view, None)
            return
        snap, usage = detail
        self.main_screen.query_one(f"#{prefix}-header", Label).update(Text(clean_display_text(snap.title), no_wrap=True, overflow="ellipsis"))
        self.main_screen.query_one(f"#{prefix}-header", Label).tooltip = Text(clean_display_text(snap.title))
        meta = f"{snap.client} · {snap.project_label or self.t('Unknown project')} · {self._state_label(snap.text_state)}"
        meta += f"\n{token_text(usage.total_tokens, language=self.language)} Tokens · {cost_text(usage, language=self.language)}"
        if usage.models:
            meta += f" · {', '.join(usage.models)}"
        if snap.archived:
            meta += f" · {self.t('Archived')}"
        self.main_screen.query_one(f"#{prefix}-meta", Label).update(Text(clean_display_text(meta), overflow="ellipsis"))
        lines: list[str] = []
        if not snap.messages:
            lines.append(self.t("No transcript available. Metadata only."))
        for message in snap.messages:
            lines.append(f"{self.t('User' if message.role == 'user' else 'Assistant')}  {format_timestamp(message.timestamp_ms)}")
            lines.append(clean_display_text(message.text))
            lines.append("")
        body = "\n".join(lines)
        same_session = self._preview_keys.get(view) == snap.ref.key
        if body != preview.text:
            selection, position = preview.selection, preview.scroll_offset
            preview.text = body
            if same_session:
                preview.selection = selection
                preview.scroll_to(x=position.x, y=position.y, animate=False, immediate=True)
        self._preview_keys[view] = snap.ref.key

    def action_details(self) -> None:
        detail = self._details.get(self.current_view)
        if not detail:
            return
        snap, usage = detail
        fields = [("Title", snap.title), ("Tool", snap.client), ("Native ID", snap.ref.native_id),
                  ("Source root", str(snap.ref.source.canonical_root)), ("Profile", snap.ref.source.profile or self.t("Default")),
                  ("Working directory", str(snap.cwd) if snap.cwd else self.t("Unknown")),
                  ("Kind", self._state_label(snap.kind)), ("Status", self._state_label(snap.text_state)),
                  ("Started", format_timestamp(snap.started_ms)), ("Last activity", format_timestamp(snap.updated_ms)),
                  ("Models", ", ".join(usage.models) or self.t("Unknown")), ("Messages", str(len(snap.messages))),
                  ("Tokens", token_text(usage.total_tokens, language=self.language)),
                  ("Cost", cost_text(usage, language=self.language))]
        for label, value in (("Input (including cache)", usage.input_tokens), ("Output (including reasoning)", usage.output_tokens),
                             ("Cache read", usage.cache_read_tokens), ("Cache write", usage.cache_write_tokens), ("Reasoning subset", usage.reasoning_tokens)):
            fields.append((label, token_text(value, language=self.language)))
        fields.extend((("Token coverage", f"{usage.known_token_records}/{usage.records}"),
                       ("Cost coverage", f"{usage.priced_records}/{usage.records}")))
        lines = [f"{self.t(key)}: {clean_display_text(value)}" for key, value in fields]
        if snap.warnings:
            lines += ["", self.t("Source warnings"), *map(clean_display_text, snap.warnings)]
        self.push_screen(InfoScreen(self.language, "\n".join(lines)))

    def _trigger_analytics(self) -> None:
        self._analytics_generation += 1
        self._run_analytics_worker(self._analytics_generation, self._get_current_filters())

    @work(thread=True, exclusive=True, group="analytics")
    def _run_analytics_worker(self, generation: int, filters: SearchFilters) -> None:
        worker = get_current_worker()
        def cancelled() -> bool:
            return worker.is_cancelled or self._closing or generation != self._analytics_generation
        try:
            with closing(Store(self.store.db_path, read_only=True)) as reader:
                reader.conn.set_progress_handler(cancelled, 1000)
                reader.conn.execute("BEGIN")
                snapshot = reader.analytics(filters, pricing=self.config.pricing)
                projects = tuple((r[0], r[1]) for r in reader.conn.execute(
                    "SELECT project_key, MIN(project_label) FROM sessions GROUP BY project_key ORDER BY project_key"))
            if not cancelled():
                self.post_message(AnalyticsReady(generation, snapshot, projects))
        except Exception as err:
            if not cancelled():
                self.post_message(QueryFailed(self.current_generation, str(err)))

    def on_analytics_ready(self, message: AnalyticsReady) -> None:
        if message.generation != self._analytics_generation:
            return
        self._analytics = message.snapshot
        self._project_options = message.projects
        self._render_analytics()
        self._render_projects()

    def _render_analytics(self) -> None:
        snapshot = self._analytics
        summary = snapshot.summary
        peak_day = max(snapshot.days, key=lambda d: d.total_tokens, default=None)
        streak = calculate_streak(snapshot.days)
        top_model = snapshot.models[0] if snapshot.models else None

        tokens_val = compact_tokens(summary.total_tokens) if summary.total_tokens is not None and summary.total_tokens >= 1000 else token_text(summary.total_tokens, language=self.language)
        self.main_screen.query_one("#kpi-tokens-val", Static).update(tokens_val)
        self.main_screen.query_one("#kpi-tokens-sub", Label).update(self.t("Coverage {known}/{total}", known=summary.known_token_records, total=summary.records))

        cost_val = (compact_cost(summary.cost_usd) + (" " + self.t("Estimated") if summary.estimated_records else "")) if summary.cost_usd is not None else self.t("Unknown")
        self.main_screen.query_one("#kpi-cost-val", Static).update(cost_val)
        self.main_screen.query_one("#kpi-cost-sub", Label).update(self.t("Priced {known}/{total}", known=summary.priced_records, total=summary.records))

        self.main_screen.query_one("#kpi-active-days-val", Static).update(f"{len(snapshot.days):,}")
        self.main_screen.query_one("#kpi-active-days-sub", Label).update(self.t("Total active days"))

        self.main_screen.query_one("#kpi-streak-val", Static).update(f"{streak:,}")
        self.main_screen.query_one("#kpi-streak-sub", Label).update(self.t("Consecutive active"))

        self.main_screen.query_one("#kpi-peak-day-val", Static).update(compact_tokens(peak_day.total_tokens) if peak_day else "—")
        self.main_screen.query_one("#kpi-peak-day-sub", Label).update(peak_day.day if peak_day else "—")

        top_model_name = top_model.label.split(" / ")[0] if top_model else "—"
        top_model_share = f"{top_model.summary.total_tokens / summary.total_tokens:.1%}" if top_model and summary.total_tokens else "—"
        self.main_screen.query_one("#kpi-top-model-val", Static).update(top_model_name)
        self.main_screen.query_one("#kpi-top-model-sub", Label).update(top_model_share)

        self.main_screen.query_one("#kpi-messages-val", Static).update(f"{summary.message_count:,}")
        self.main_screen.query_one("#kpi-messages-sub", Label).update(self.t("Total messages"))

        self.main_screen.query_one("#kpi-sessions-val", Static).update(f"{summary.session_count:,}")
        self.main_screen.query_one("#kpi-sessions-sub", Label).update(self.t("Unknown usage: {count}", count=summary.unknown_sessions))

        heatmap_text = render_calendar_heatmap(snapshot.days, width=max(40, self.size.width - 8), language=self.language)
        self.main_screen.query_one("#activity-heatmap", Static).update(heatmap_text)

        table = self.main_screen.query_one("#model-table", DataTable)
        cursor, position = table.cursor_row, table.scroll_offset
        table.clear()
        top_model_tokens = max((g.summary.total_tokens for g in snapshot.models if g.summary.total_tokens), default=1) or 1
        for group in snapshot.models:
            provider, model = json.loads(group.key)
            label = f"{model or self.t('Unknown model')} / {provider or self.t('Unknown provider')}"
            value = group.summary.total_tokens
            share = f"{value / summary.total_tokens:.1%}" if value is not None and summary.total_tokens else "—"
            bar = horizontal_bar(value or 0, top_model_tokens, 10)
            table.add_row(
                Text(clean_display_text(label)),
                Text(bar, style="#5caaa3"),
                compact_tokens(value),
                share,
                cost_text(group.summary, language=self.language),
            )
        table.move_cursor(row=cursor)
        table.scroll_to(x=position.x, y=position.y, animate=False)

        table = self.main_screen.query_one("#tool-table", DataTable)
        cursor, position = table.cursor_row, table.scroll_offset
        table.clear()
        top_tool_tokens = max((g.summary.total_tokens for g in snapshot.tools if g.summary.total_tokens), default=1) or 1
        for group in snapshot.tools:
            label = PLATFORM_LABELS.get(group.key, group.key or self.t("Unknown tool"))
            value = group.summary.total_tokens
            share = f"{value / summary.total_tokens:.1%}" if value is not None and summary.total_tokens else "—"
            bar = horizontal_bar(value or 0, top_tool_tokens, 10)
            table.add_row(
                Text(clean_display_text(label)),
                Text(bar, style="#5caaa3"),
                compact_tokens(value),
                share,
                f"{group.summary.session_count:,}",
            )
        table.move_cursor(row=cursor)
        table.scroll_to(x=position.x, y=position.y, animate=False)

        table = self.main_screen.query_one("#daily-table", DataTable)
        cursor, position = table.cursor_row, table.scroll_offset
        table.clear()
        peak = max((day.total_tokens for day in snapshot.days), default=0) or 1
        for day in reversed(snapshot.days):
            width = int(day.total_tokens / peak * 10)
            share = f"{day.total_tokens / summary.total_tokens:.1%}" if summary.total_tokens else "—"
            table.add_row(
                day.day,
                compact_tokens(day.total_tokens),
                "━" * width,
                f"${day.cost_usd:,.4f}" if day.cost_usd is not None else self.t("Unknown"),
                share,
            )
        table.move_cursor(row=cursor)
        table.scroll_to(x=position.x, y=position.y, animate=False)
        self.main_screen.query_one("#daily-table-status", Label).update(self.t(
            "Usage time · costs may be estimated/partial · undated tokens: {tokens}",
            tokens=token_text(snapshot.unattributed_tokens, language=self.language)))

        self.main_screen.query_one("#usage-trend", Static).update(
            render_vertical_bar_chart(snapshot.days, width=max(20, self.size.width - 8), height=8, language=self.language)
        )

    def _project_display(self, key: str, label: str | None) -> str:
        if not key:
            return self.t("Unknown project")
        clean = (label or Path(key).name or key).strip()
        return clean or "/"

    def _render_projects(self) -> None:
        tree = self.main_screen.query_one("#project-tree", Tree)
        groups = {group.key: group for group in self._analytics.projects}
        self._rendering_projects = True
        try:
            for key in list(self._project_nodes):
                if key not in groups:
                    self._project_nodes.pop(key).remove()
                    self._project_pages.pop(key, None)
            total = self._analytics.summary.total_tokens or 1
            for key, group in groups.items():
                value = group.summary.total_tokens
                share = f"{value / total:.1%}" if value is not None else "—"
                name = (group.label or Path(key).name) if key else self.t("Unknown project")
                node = self._project_nodes.get(key)
                text = _format_project_label(name, value, group.summary.cost_usd, group.summary.session_count, share, key if key else "")
                if node is None:
                    node = tree.root.add(text, data={"type": "project", "project_key": key})
                    self._project_nodes[key] = node
                else:
                    node.set_label(text)
                if key in self._project_pages:
                    self._render_project_children(key)
        finally:
            self._rendering_projects = False
        self.main_screen.query_one("#project-tree-status", Label).update(self.t(
            "{count} projects · session list filtered by activity time", count=len(groups)))
        col_header = "  " + _pad_cell(self.t("Project"), 26, "left") + "  " + \
                     _pad_cell(self.t("Tokens"), 10, "right") + "  " + \
                     _pad_cell(self.t("Cost"), 10, "right") + "  " + \
                     _pad_cell(self.t("Sessions"), 7, "right") + "  " + \
                     _pad_cell(self.t("Share"), 7, "right") + "  " + \
                     self.t("Path")
        self.main_screen.query_one("#project-tree-columns", Label).update(Text(col_header, style="dim"))

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        data = event.node.data or {}
        if data.get("type") == "project" and data["project_key"] not in self._project_pages:
            self._load_project_page(data["project_key"], 0)

    def _load_project_page(self, project_key: str, offset: int) -> None:
        if project_key in self._project_loading:
            return
        self._project_loading.add(project_key)
        self._render_project_children(project_key)
        prior = self._project_pages.get(project_key)
        limit = max(self.page_size, len(prior[0])) if offset == 0 and prior else self.page_size
        self._run_project_worker(self._project_generation, project_key,
                                 replace(self._get_current_filters(), project_key=project_key), offset, limit)

    @work(thread=True, group="projects")
    def _run_project_worker(self, generation: int, key: str, filters: SearchFilters, offset: int, limit: int) -> None:
        worker = get_current_worker()
        def cancelled() -> bool:
            return worker.is_cancelled or self._closing or generation != self._project_generation
        try:
            with closing(Store(self.store.db_path, read_only=True)) as reader:
                reader.conn.set_progress_handler(cancelled, 1000)
                reader.conn.execute("BEGIN")
                page = reader.search("", filters, offset=offset, limit=limit)
                usage = reader.session_usage([r.session_key for r in page.results], pricing=self.config.pricing)
            if not cancelled():
                self.post_message(ProjectPageReady(generation, key, page, usage))
        except Exception as err:
            if not cancelled():
                self.post_message(ProjectPageReady(generation, key, None, {}, str(err)))

    def on_project_page_ready(self, message: ProjectPageReady) -> None:
        if message.generation != self._project_generation or message.project_key not in self._project_nodes:
            return
        key = message.project_key
        self._project_loading.discard(key)
        if message.error:
            self.notify(clean_display_text(message.error), title=self.t("Query failed"), severity="error", markup=False)
            self._render_project_children(key)
            return
        page = message.page
        if page is None:
            return
        if page.offset == 0:
            rows, usage = [], {}
        else:
            previous = self._project_pages.get(key)
            if previous is None or len(previous[0]) != page.offset:
                return
            rows, _, usage = previous
            rows = list(rows)
            usage = dict(usage)
        seen = {row.session_key for row in rows}
        rows.extend(row for row in page.results if row.session_key not in seen)
        usage.update(message.usage)
        self._project_pages[key] = (rows, page.total_count, usage)
        self._render_project_children(key)
        selected = self._selections["projects-view"]
        if selected and any(row.session_key == selected for row in rows):
            self._load_detail(selected, "projects-view")

    def _render_project_children(self, key: str) -> None:
        node = self._project_nodes.get(key)
        if node is None:
            return
        tree = self.main_screen.query_one("#project-tree", Tree)
        cursor_data = tree.cursor_node.data if tree.cursor_node else None
        position = tree.scroll_offset
        node.remove_children()
        page = self._project_pages.get(key)
        if page:
            rows, total, usage = page
            for index, row in enumerate(rows):
                label = f"{format_timestamp(row.updated_ms)[5:]}  {row.client}  {row.title}"
                child = node.add_leaf(Text(clean_display_text(label), no_wrap=True), data={"type": "session", "session_key": row.session_key, "project_key": key})
                if cursor_data and (cursor_data.get("session_key") == row.session_key or (
                    cursor_data.get("type") == "more" and cursor_data.get("project_key") == key and cursor_data.get("offset") == index
                )):
                    self.call_after_refresh(tree.move_cursor, child)
            if len(rows) < total:
                more = node.add_leaf(self.t("Load more… ({loaded}/{total})", loaded=len(rows), total=total),
                                     data={"type": "more", "project_key": key, "offset": len(rows)})
                if cursor_data and cursor_data.get("type") == "more" and cursor_data.get("project_key") == key and cursor_data.get("offset") == len(rows):
                    self.call_after_refresh(tree.move_cursor, more)
            elif not rows:
                node.add_leaf(self.t("No matching conversations."), data={"type": "empty"})
        if key in self._project_loading:
            node.add_leaf(self.t("Loading…"), data={"type": "loading"})
        elif page is None:
            node.add_leaf(self.t("Retry loading"), data={"type": "more", "project_key": key, "offset": 0})
        tree.scroll_to(x=position.x, y=position.y, animate=False)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if self._rendering_projects:
            return
        data = event.node.data or {}
        if data.get("type") == "session":
            key = data["session_key"]
            self._selections["projects-view"] = key
            if self.current_view == "projects-view":
                self.selected_session_key = key
            self._load_detail(key, "projects-view")
        elif self.current_view == "projects-view":
            self.selected_session_key = None

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data or {}
        if data.get("type") == "more":
            self._load_project_page(data["project_key"], data["offset"])
        elif data.get("type") == "session":
            self.on_tree_node_highlighted(Tree.NodeHighlighted(event.node))
            self._read_preview()

    def action_focus_search(self) -> None:
        self._show_view("sessions-view")
        self.main_screen.query_one("#query", Input).focus()

    def action_cancel_or_table(self) -> None:
        self.main_screen.remove_class("reading")
        self.main_screen.query_one({"sessions-view": "#sessions", "projects-view": "#project-tree", "dashboard-view": "#model-table"}[self.current_view]).focus()
        self._render_hints()

    def action_preview_page_up(self) -> None:
        if self.current_view in self._selections:
            self.main_screen.query_one("#project-preview" if self.current_view == "projects-view" else "#preview", TextArea).scroll_page_up()

    def action_preview_page_down(self) -> None:
        if self.current_view in self._selections:
            self.main_screen.query_one("#project-preview" if self.current_view == "projects-view" else "#preview", TextArea).scroll_page_down()

    def action_prev_page(self) -> None:
        if self.current_view == "sessions-view" and self.page_offset:
            self.page_offset = max(0, self.page_offset - self.page_size)
            self._trigger_search()

    def action_next_page(self) -> None:
        if self.current_view == "sessions-view" and self.page_offset + self.page_size < self._search_total:
            self.page_offset += self.page_size
            self._trigger_search()

    def action_quit_app(self) -> None:
        if not isinstance(self.focused, Input):
            self.exit(0)

    def action_force_quit(self) -> None:
        self.exit(0)

    def action_refresh_index(self) -> None:
        if self._is_indexing or self._handoff or self._closing:
            return
        self._is_indexing = True
        self._cancel_scan = False
        self._scan_error = None
        self._index_worker = self._run_indexing_worker()

    @work(thread=True, group="indexing")
    def _run_indexing_worker(self) -> None:
        worker = get_current_worker()
        def cancelled() -> bool:
            return self._cancel_scan or self._closing or worker.is_cancelled
        def progress_cb(progress: IndexProgress) -> None:
            self.post_message(ScanStatusUpdate(replace(progress, errors=list(progress.errors))))
        try:
            with closing(Store(self.store.db_path)) as writer:
                Indexer(writer, self.config.sources).scan_all(cancelled=cancelled, progress_callback=progress_cb)
        except Exception as err:
            self._scan_error = str(err)
        finally:
            self.post_message(ScanFinished())

    def on_scan_finished(self, message: ScanFinished) -> None:
        self._is_indexing = False
        if self._closing:
            return
        self._render_scan_status()
        if self._handoff:
            return
        self._update_platform_options()
        if self._scan_progress.updated_sessions:
            self._project_generation += 1
            self._project_loading.clear()
            for key, node in self._project_nodes.items():
                if node.is_expanded:
                    self._load_project_page(key, 0)
                else:
                    self._project_pages.pop(key, None)
                    node.remove_children()
        self._trigger_search()
        self._trigger_analytics()

    def on_scan_status_update(self, message: ScanStatusUpdate) -> None:
        self._scan_progress = message.progress
        self._render_scan_status()

    def _render_scan_status(self) -> None:
        progress = self._scan_progress
        errors = len(progress.errors) + bool(self._scan_error)
        if self._is_indexing:
            text = self.t("Indexing {tool} · {count} sessions", tool=progress.source_tool or "", count=progress.scanned_sessions)
        elif not self.config.sources:
            text = self.t("No sources configured. Add local source roots in config.toml.")
        else:
            text = self.t("Index ready · {count} sessions scanned", count=progress.scanned_sessions)
        if errors:
            text += " · " + self.t("{count} source errors", count=errors)
        self.main_screen.query_one("#scan-status", Label).update(Text(text, no_wrap=True, overflow="ellipsis"))
        self.main_screen.query_one("#scan-status", Label).tooltip = Text(clean_display_text("\n".join([*progress.errors, self._scan_error or ""])))

    def _unavailable_text(self, unavailable: Unavailable) -> str:
        descriptions = {
            "missing_executable": "Native CLI executable not found. Check installation and PATH.",
            "missing_source": "The native source is missing or has moved.",
            "missing_cwd": "The recorded working directory no longer exists.",
            "unknown_cwd": "No verified working directory. Refusing to guess.",
            "unsupported_profile": "The profile does not match the selected native store.",
            "unsupported_resume": "This session cannot be resumed in a native CLI.",
            "unverified_compatibility": "The native environment is not initialized or verified.",
            "ambiguous_identity": "Multiple sources match this identity. Resume is disabled.",
            "archived": "Unarchive this session in its original tool first.",
            "secondary_session": "Secondary sessions cannot be resumed as top-level conversations.",
            "invalid_identity": "Invalid native session identity.",
            "metadata_only": "Only metadata is available; no native transcript.",
        }
        text = self.t(descriptions.get(unavailable.code, "Cannot resume this session."))
        if unavailable.reason:
            text += f"\n{self.t('Details')}: {clean_display_text(unavailable.reason)}"
        return text

    def action_resume_session(self) -> None:
        if self.current_view not in self._selections or isinstance(self.focused, Input):
            return
        if not self.selected_session_key:
            self.notify(self.t("Select a conversation first."), severity="warning", markup=False)
            return
        snap = self.store.get_session(self.selected_session_key)
        if not snap:
            self.notify(self.t("Conversation unavailable."), severity="warning", markup=False)
            return
        spec = prepare_resume(snap.ref)
        if isinstance(spec, Unavailable):
            self.notify(self._unavailable_text(spec), title=self.t("Cannot resume"), severity="warning", timeout=8, markup=False)
            return
        target = spec.argv[spec.argv.index("--resume") + 1] if "--resume" in spec.argv else spec.argv[-1].removeprefix("--resume=")
        profile = spec.argv[spec.argv.index("--profile") + 1] if "--profile" in spec.argv else snap.ref.source.profile
        if snap.client == "dsh":
            profile = "dsh-tui"
        self.push_screen(ResumeConfirmScreen(snap.client, target, spec.cwd, profile, self.language),
                         lambda confirmed: self._on_resume_confirmed(confirmed, snap, spec))

    @work(exclusive=True, group="handoff")
    async def _on_resume_confirmed(self, confirmed: bool, snap: SessionSnapshot, spec: LaunchSpec) -> None:
        if not confirmed:
            return
        self._handoff = self._cancel_scan = True
        try:
            if self._index_worker:
                try:
                    await self._index_worker.wait()
                except WorkerCancelled:
                    pass
            reverified = prepare_resume(snap.ref)
            if isinstance(reverified, Unavailable):
                self.notify(self._unavailable_text(reverified), severity="error", markup=False)
                return
            if reverified != spec:
                self.notify(self.t("Resume target changed. Please review and confirm again."), severity="warning", markup=False)
                return
            launch_error = None
            code = 0
            try:
                with self.suspend():
                    try:
                        code = run_foreground(reverified)
                    except Exception as err:
                        launch_error = str(err)
            except Exception as err:
                self.notify(clean_display_text(str(err)), title=self.t("Terminal suspension failed"), severity="error", markup=False)
                return
            if launch_error:
                self.notify(clean_display_text(launch_error), title=self.t("Launch failed"), severity="error", markup=False)
            elif code:
                self.notify(self.t("Native CLI exited with status {code}", code=code), severity="warning", markup=False)
        finally:
            self._handoff = False
            self.action_refresh_index()


def run_app(config_path: Path | None = None, data_dir: Path | None = None) -> int:
    config = load_config(config_path=config_path, data_dir=data_dir)
    app = HubApp(config)
    try:
        app.run()
        return 0
    finally:
        app.store.close()
