from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Select, TextArea
from textual.widgets.option_list import Option

from ai_session_hub.i18n import tr
from ai_session_hub.source_io import clean_display_text


@dataclass(frozen=True)
class FilterSelection:
    tool: str | None = None
    project_key: str | None = None
    time: str | None = None
    kind: str | None = None

def shorten_path(path_str: str) -> str:
    if not path_str:
        return ""
    try:
        home = str(Path.home())
        if path_str == home:
            return "~"
        if path_str.startswith(home + "/"):
            return "~" + path_str[len(home):]
    except Exception:
        pass
    return path_str


def format_project_option(name: str, path: str | None, selected: bool, width: int = 75) -> Text:
    prefix = "> " if selected else "  "
    if not path:
        return Text(prefix + name, style="bold" if selected else "")

    clean_name = name.strip() or "/"
    short_p = shorten_path(path) if path != "/" else "/"

    name_width = 28
    clen = cell_len(clean_name)
    if clen > name_width:
        cur = ""
        for ch in clean_name:
            if cell_len(cur + ch) > name_width - 1:
                break
            cur += ch
        cur += "…"
        name_str = cur
    else:
        name_str = clean_name + " " * (name_width - clen)

    res = Text(prefix)
    res.append(name_str, style="bold" if selected else "#d6d8da")
    res.append("  ")

    path_width = max(10, width - name_width - len(prefix) - 4)
    plen = cell_len(short_p)
    if plen > path_width:
        p_cur = ""
        for ch in short_p:
            if cell_len(p_cur + ch) > path_width - 1:
                break
            p_cur += ch
        p_cur += "…"
        short_p = p_cur

    res.append(short_p, style="dim")
    return res


class FilterScreen(ModalScreen[FilterSelection | None]):
    """Edit a filter draft; cancellation never changes the underlying query."""

    BINDINGS = [Binding("escape", "cancel", show=False), Binding("ctrl+enter", "apply", show=False)]
    FIELDS = (("tool", "Tool"), ("project_key", "Project"), ("time", "Time"), ("kind", "Kind"))

    def __init__(self, language: str, selection: FilterSelection,
                 platforms: tuple[tuple[str, str], ...], projects: tuple[tuple[str, str], ...]):
        super().__init__()
        self.language = language
        self.draft = selection
        self.field = "tool"
        self.choices = {
            "tool": ((tr(language, "All tools"), None), *platforms),
            "project_key": ((tr(language, "All projects"), None), *projects),
            "time": tuple((tr(language, label), value) for label, value in (
                ("All time", None), ("Last 24 hours", "24h"), ("Last 7 days", "7d"), ("Last 30 days", "30d"))),
            "kind": tuple((tr(language, label), value) for label, value in (
                ("All kinds", None), ("Conversation", "conversation"), ("Subagent", "subagent"),
                ("Advisor", "advisor"), ("Background review", "background-review"),
                ("IDE", "ide"), ("Unknown", "unknown"), ("Metadata only", "metadata-only"))),
        }
        self._visible_choices: list[tuple[str, str | None]] = []

    def compose(self) -> ComposeResult:
        t = lambda key: tr(self.language, key)
        with Vertical(classes="hub-dialog", id="filter-dialog"):
            yield Label(t("Filters"), classes="dialog-title")
            with Horizontal(id="filter-tabs"):
                for field, label in self.FIELDS:
                    yield Button(t(label), id=f"filter-tab-{field}")
            yield Input(placeholder=t("Find a name or path…"), id="filter-query", classes="dialog-input")
            yield OptionList(id="filter-results")
            yield Label("", id="filter-detail", markup=False)
            yield Label("", id="filter-draft", markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button(t("Reset"), id="filter-reset")
                yield Button(t("Cancel"), id="filter-cancel")
                yield Button(t("Apply"), id="filter-apply", variant="primary")

    def on_mount(self) -> None:
        self._render_choices()
        self.query_one("#filter-query", Input).focus()

    def _render_choices(self) -> None:
        query = self.query_one("#filter-query", Input).value.casefold().strip()
        self._visible_choices = [(label, value) for label, value in self.choices[self.field]
                        if query in label.casefold() or query in (value or "").casefold()]
        options = self.query_one("#filter-results", OptionList)
        options.clear_options()
        selected = getattr(self.draft, self.field)
        dialog_width = options.size.width if options.is_mounted and options.size.width > 0 else 75
        option_items = []
        for index, (label, value) in enumerate(self._visible_choices):
            if self.field == "project_key" and value is not None:
                opt_text = format_project_option(label, value, value == selected, width=dialog_width)
            else:
                prefix = "> " if value == selected else "  "
                opt_text = Text(
                    prefix + clean_display_text(label),
                    no_wrap=True,
                    overflow="ellipsis",
                )
            option_items.append(Option(opt_text, id=str(index)))
        options.add_options(option_items)
        options.highlighted = next((i for i, (_, value) in enumerate(self._visible_choices) if value == selected), 0) if self._visible_choices else None
        for field, _ in self.FIELDS:
            self.query_one(f"#filter-tab-{field}", Button).set_class(field == self.field, "active")
        parts = []
        for field, label in self.FIELDS:
            value = getattr(self.draft, field)
            chosen = next((name for name, key in self.choices[field] if key == value), value or "—")
            parts.append(f"{tr(self.language, label)}: {chosen}")
        self.query_one("#filter-draft", Label).update(Text(clean_display_text(" · ".join(parts))))
        self.query_one("#filter-detail", Label).update(tr(self.language, "No matches") if not self._visible_choices else "")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter-query":
            self._render_choices()

    def on_key(self, event: Key) -> None:
        if event.key == "down" and self.query_one("#filter-query", Input).has_focus:
            self.query_one("#filter-results", OptionList).focus()
            event.stop()
            event.prevent_default()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        options = self.query_one("#filter-results", OptionList)
        if options.highlighted is not None:
            self._choose(options.highlighted)

    def _choose(self, index: int) -> None:
        if 0 <= index < len(self._visible_choices):
            self.draft = replace(self.draft, **{self.field: self._visible_choices[index][1]})
            self._render_choices()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._choose(event.option_index)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if 0 <= event.option_index < len(self._visible_choices):
            label, value = self._visible_choices[event.option_index]
            self.query_one("#filter-detail", Label).update(Text(clean_display_text(value or label)))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button.id or ""
        if button.startswith("filter-tab-"):
            self.field = button.removeprefix("filter-tab-")
            self.query_one("#filter-query", Input).value = ""
            self._render_choices()
            self.query_one("#filter-query", Input).focus()
        elif button == "filter-reset":
            self.draft = FilterSelection()
            self.query_one("#filter-query", Input).value = ""
            self._render_choices()
        elif button == "filter-apply":
            self.action_apply()
        elif button == "filter-cancel":
            self.action_cancel()

    def action_apply(self) -> None:
        self.dismiss(self.draft)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SettingsScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, language: str):
        super().__init__()
        self.language = language

    def compose(self) -> ComposeResult:
        t = lambda key: tr(self.language, key)
        with Vertical(classes="hub-dialog", id="settings-dialog"):
            yield Label(t("Settings"), classes="dialog-title")
            yield Label(t("Interface language"), id="settings-title")
            yield Select([("简体中文", "zh-CN"), ("English", "en")], value=self.language,
                         allow_blank=False, id="settings-language")
            yield Label(t("Applies immediately and is remembered on restart."), id="settings-note")
            with Horizontal(classes="dialog-actions"):
                yield Button(t("Cancel"), id="settings-cancel")
                yield Button(t("Apply"), id="settings-apply", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings-apply":
            self.dismiss(str(self.query_one("#settings-language", Select).value))
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)


class InfoScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", show=False)]

    def __init__(self, language: str, text: str, title: str = "Details"):
        super().__init__()
        self.language, self.text, self.title_key = language, text, title

    def compose(self) -> ComposeResult:
        with Vertical(classes="hub-dialog", id="info-dialog"):
            yield Label(tr(self.language, self.title_key), classes="dialog-title")
            yield TextArea(self.text, read_only=True, show_line_numbers=False, id="info-text")
            yield Button(tr(self.language, "Close"), id="info-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_close()

    def action_close(self) -> None:
        self.dismiss(None)
