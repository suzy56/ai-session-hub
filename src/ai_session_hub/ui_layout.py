from __future__ import annotations

from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Input,
    Label,
    Static,
    TextArea,
    Tree,
)

from ai_session_hub.i18n import tr

PLATFORM_LABELS = {
    "codex": "Codex",
    "claude": "Claude Code",
    "hermes": "Hermes",
    "omp": "OMP",
    "dsh": "DSH",
    "cursor-ide": "Cursor IDE",
}


def compose_hub_widgets(language: str) -> tuple[Widget, ...]:
    """Compose the session hub without storing language or navigation state."""
    view_nav = Horizontal(
        Label(tr(language, "Session Hub"), id="app-title"),
        Button(tr(language, "Sessions"), id="nav-sessions", variant="primary"),
        Button(tr(language, "Projects"), id="nav-projects"),
        Button(tr(language, "Dashboard"), id="nav-dashboard"),
        Static("", id="nav-spacer"),
        Button(tr(language, "Filters"), id="open-filters"),
        Button(tr(language, "Settings"), id="nav-settings"),
        id="view-nav",
    )
    scope_bar = Horizontal(
        Label("", id="scope-summary"),
        id="scope-bar",
    )

    kpi_grid = Horizontal(
        Vertical(
            Label(tr(language, "Total tokens"), id="kpi-tokens-title", classes="kpi-title"),
            Static("—", id="kpi-tokens-val", classes="kpi-value"),
            Label(tr(language, "Usage coverage"), id="kpi-tokens-sub", classes="kpi-sub"),
            classes="kpi-card",
            id="kpi-tokens",
        ),
        Vertical(
            Label(tr(language, "Known cost"), id="kpi-cost-title", classes="kpi-title"),
            Static("—", id="kpi-cost-val", classes="kpi-value"),
            Label(tr(language, "Pricing coverage"), id="kpi-cost-sub", classes="kpi-sub"),
            classes="kpi-card",
            id="kpi-cost",
        ),
        Vertical(
            Label(tr(language, "Active days"), id="kpi-active-days-title", classes="kpi-title"),
            Static("—", id="kpi-active-days-val", classes="kpi-value"),
            Label(tr(language, "Total active days"), id="kpi-active-days-sub", classes="kpi-sub"),
            classes="kpi-card",
            id="kpi-active-days",
        ),
        Vertical(
            Label(tr(language, "Consecutive days"), id="kpi-streak-title", classes="kpi-title"),
            Static("—", id="kpi-streak-val", classes="kpi-value"),
            Label(tr(language, "Consecutive active"), id="kpi-streak-sub", classes="kpi-sub"),
            classes="kpi-card",
            id="kpi-streak",
        ),
        Vertical(
            Label(tr(language, "Peak single day"), id="kpi-peak-day-title", classes="kpi-title"),
            Static("—", id="kpi-peak-day-val", classes="kpi-value"),
            Label("—", id="kpi-peak-day-sub", classes="kpi-sub"),
            classes="kpi-card",
            id="kpi-peak-day",
        ),
        Vertical(
            Label(tr(language, "Top model"), id="kpi-top-model-title", classes="kpi-title"),
            Static("—", id="kpi-top-model-val", classes="kpi-value"),
            Label("—", id="kpi-top-model-sub", classes="kpi-sub"),
            classes="kpi-card",
            id="kpi-top-model",
        ),
        Vertical(
            Label(tr(language, "Total messages"), id="kpi-messages-title", classes="kpi-title"),
            Static("—", id="kpi-messages-val", classes="kpi-value"),
            Label(tr(language, "Active days"), id="kpi-messages-sub", classes="kpi-sub"),
            classes="kpi-card",
            id="kpi-messages",
        ),
        Vertical(
            Label(tr(language, "Total sessions"), id="kpi-sessions-title", classes="kpi-title"),
            Static("—", id="kpi-sessions-val", classes="kpi-value"),
            Label(tr(language, "Unknown usage"), id="kpi-sessions-sub", classes="kpi-sub"),
            classes="kpi-card",
            id="kpi-sessions",
        ),
        id="kpi-row",
    )

    dash_subnav = Horizontal(
        Button(tr(language, "Overview"), id="dash-tab-overview", variant="primary"),
        Button(tr(language, "Trend"), id="dash-tab-trend"),
        id="dash-subnav",
    )

    heatmap_card = Vertical(
        Label(tr(language, "Token Activity"), id="heatmap-title", classes="section-title"),
        Static(id="activity-heatmap"),
        classes="dashboard-card",
        id="heatmap-card",
    )

    breakdown_row = Horizontal(
        Vertical(
            Label(tr(language, "Model breakdown"), id="model-title", classes="section-title"),
            DataTable(id="model-table", cursor_type="row"),
            classes="dashboard-card",
            id="model-card",
        ),
        Vertical(
            Label(tr(language, "Tool breakdown"), id="tool-title", classes="section-title"),
            DataTable(id="tool-table", cursor_type="row"),
            classes="dashboard-card",
            id="tool-card",
        ),
        id="dash-breakdown-row",
    )

    dash_overview_pane = VerticalScroll(
        kpi_grid,
        heatmap_card,
        breakdown_row,
        id="dash-overview-pane",
    )

    dash_trend_pane = VerticalScroll(
        Vertical(
            Label(tr(language, "Usage trend"), id="trend-title", classes="section-title"),
            Static(id="usage-trend"),
            classes="dashboard-card",
            id="trend-chart-card",
        ),
        Vertical(
            Label(tr(language, "Daily activity"), id="daily-title", classes="section-title"),
            DataTable(id="daily-table", cursor_type="row"),
            Label("", id="daily-table-status"),
            classes="dashboard-card",
            id="daily-card",
        ),
        id="dash-trend-pane",
    )

    dash_switcher = ContentSwitcher(
        dash_overview_pane,
        dash_trend_pane,
        id="dash-switcher",
        initial="dash-overview-pane",
    )

    dashboard_view = Vertical(
        dash_subnav,
        dash_switcher,
        id="dashboard-view",
    )

    main_container = Horizontal(
        DataTable(id="sessions", cursor_type="row"),
        Vertical(
            Label(tr(language, "Conversation"), id="preview-header"),
            Label("", id="preview-meta"),
            TextArea(id="preview", read_only=True, show_line_numbers=False, soft_wrap=True),
            Button(tr(language, "Details"), id="preview-info"),
            id="preview-container",
        ),
        id="main-container",
    )
    sessions_view = Vertical(
        Input(placeholder=tr(language, "Search messages or title (/ to focus)"), id="query"),
        main_container,
        Label("", id="session-page-status"),
        id="sessions-view",
    )

    projects_container = Horizontal(
        Vertical(
            Label(tr(language, "Projects"), id="project-tree-header"),
            Label("", id="project-tree-columns"),
            Tree(tr(language, "Projects"), id="project-tree"),
            Label("", id="project-tree-status"),
            id="project-tree-container",
        ),
        Vertical(
            Label(tr(language, "Conversation"), id="project-preview-header"),
            Label("", id="project-preview-meta"),
            TextArea(id="project-preview", read_only=True, show_line_numbers=False, soft_wrap=True),
            Button(tr(language, "Details"), id="project-preview-info"),
            id="project-preview-container",
        ),
        id="projects-main-container",
    )
    projects_view = Vertical(projects_container, id="projects-view")
    views = ContentSwitcher(
        sessions_view,
        projects_view,
        dashboard_view,
        id="views",
        initial="sessions-view",
    )
    return (
        view_nav,
        scope_bar,
        views,
        Label("", id="scan-status"),
        Label("", id="key-hints"),
    )
