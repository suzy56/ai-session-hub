from __future__ import annotations

from datetime import date, timedelta

from rich.text import Text

from ai_session_hub.i18n import tr
from ai_session_hub.usage import DailyUsage, UsageSummary


def token_text(value: int | None, language: str = "zh-CN") -> str:
    return tr(language, "Unknown") if value is None else f"{value:,}"


def compact_tokens(value: int | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def compact_cost(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:,.2f}"


def cost_text(summary: UsageSummary, language: str = "zh-CN") -> str:
    if summary.cost_usd is None:
        return tr(language, "Unknown")
    label = tr(language, "Estimated" if summary.estimated_records else "Recorded")
    coverage = " · " + tr(language, "Partial") if summary.priced_records < summary.records or summary.unknown_sessions else ""
    return f"${summary.cost_usd:,.4f} {label}{coverage}"


def calculate_streak(days: tuple[DailyUsage, ...]) -> int:
    """Calculate consecutive days of activity leading up to today or the latest active date."""
    if not days:
        return 0
    dates = set()
    for d in days:
        try:
            dates.add(date.fromisoformat(d.day))
        except ValueError:
            pass
    if not dates:
        return 0
    today = date.today()
    check_date = today if today in dates else today - timedelta(days=1)
    if check_date not in dates:
        check_date = max(dates)
    streak = 0
    while check_date in dates:
        streak += 1
        check_date -= timedelta(days=1)
    return streak


def horizontal_bar(val: int, max_val: int, length: int = 12) -> str:
    """Render a horizontal progress bar for model/tool share."""
    if not max_val or not val or length <= 0:
        return "·" * length
    ratio = min(1.0, max(0.0, val / max_val))
    filled = int(ratio * length)
    return "█" * filled + "░" * (length - filled)


def render_calendar_heatmap(days: tuple[DailyUsage, ...], width: int = 120, language: str = "zh-CN") -> Text:
    """Render a GitHub-style 7-row contribution calendar heatmap."""
    day_data = {item.day: item.total_tokens for item in days}
    col_width = 2 if (width - 4) >= 52 * 2 else 1
    num_weeks = min(52, max(12, (width - 4) // col_width))

    today = date.today()
    end_date = today + timedelta(days=(6 - today.weekday()))
    start_date = end_date - timedelta(weeks=num_weeks) + timedelta(days=1)

    max_val = max(day_data.values(), default=1) or 1
    row_labels = ["一", "", "三", "", "五", "", "日"] if language == "zh-CN" else ["M", "", "W", "", "F", "", "S"]

    res = Text()
    for row_idx in range(7):
        line = Text(f"{row_labels[row_idx]:<2} ", style="dim")
        for week_idx in range(num_weeks):
            cur_day = start_date + timedelta(weeks=week_idx, days=row_idx)
            if cur_day > today:
                char = "  " if col_width == 2 else " "
                line.append(char, style="dim")
                continue
            val = day_data.get(cur_day.isoformat(), 0)
            if val == 0:
                char = "· " if col_width == 2 else "·"
                style = "#2d3136"
            else:
                ratio = val / max_val
                if ratio < 0.15:
                    char = "■ " if col_width == 2 else "■"
                    style = "#23534d"
                elif ratio < 0.40:
                    char = "■ " if col_width == 2 else "■"
                    style = "#357a72"
                elif ratio < 0.70:
                    char = "■ " if col_width == 2 else "■"
                    style = "#5caaa3"
                else:
                    char = "■ " if col_width == 2 else "■"
                    style = "#80b6af"
            line.append(char, style=style)
        res.append_text(line)
        res.append("\n")

    month_line = Text("   ")
    cur_month = -1
    week_chars: list[tuple[int, str]] = []
    for week_idx in range(num_weeks):
        cur_day = start_date + timedelta(weeks=week_idx)
        if cur_day.month != cur_month and cur_day <= today:
            cur_month = cur_day.month
            m_label = f"{cur_month}月" if language == "zh-CN" else cur_day.strftime("%b")
            week_chars.append((week_idx * col_width, m_label))

    rendered_months = [" "] * (num_weeks * col_width + 4)
    for pos, label in week_chars:
        if pos + len(label) < len(rendered_months):
            for i, c in enumerate(label):
                rendered_months[pos + i] = c
    month_line.append("".join(rendered_months).rstrip(), style="dim")
    res.append_text(month_line)
    return res


def render_vertical_bar_chart(days: tuple[DailyUsage, ...], width: int = 80, height: int = 8, language: str = "zh-CN") -> Text:
    """Render a multi-line vertical bar chart with Y-axis tokens and X-axis dates."""
    day_data = {item.day: item.total_tokens for item in days}
    if not day_data:
        return Text("No usage data" if language == "en" else "无用量数据", style="dim")

    sorted_days = sorted(day_data.keys())
    y_axis_width = 9
    avail_cols = max(14, width - y_axis_width - 2)

    latest_date = date.fromisoformat(sorted_days[-1])
    chart_days = [latest_date - timedelta(days=avail_cols - 1 - i) for i in range(avail_cols)]
    series = [day_data.get(d.isoformat(), 0) for d in chart_days]

    peak = max(series, default=0)
    if peak == 0:
        return Text("0 tokens", style="dim")

    def fmt_num(v: float) -> str:
        if v >= 1_000_000_000:
            return f"{v / 1_000_000_000:.1f}B"
        if v >= 1_000_000:
            return f"{v / 1_000_000:.1f}M"
        if v >= 1_000:
            return f"{v / 1_000:.1f}K"
        return f"{int(v)}"

    sub_blocks = " ▂▃▄▅▆▇█"
    res = Text()

    for r in range(height - 1, -1, -1):
        if r == height - 1:
            y_str = f"{fmt_num(peak):>7} ┤ "
        elif r == int((height - 1) * 0.75):
            y_str = f"{fmt_num(peak * 0.75):>7} ┤ "
        elif r == int((height - 1) * 0.5):
            y_str = f"{fmt_num(peak * 0.5):>7} ┤ "
        elif r == int((height - 1) * 0.25):
            y_str = f"{fmt_num(peak * 0.25):>7} ┤ "
        elif r == 0:
            y_str = f"{'0':>7} ┤ "
        else:
            y_str = f"{'':>7} │ "

        row_text = Text(y_str, style="dim")
        for val in series:
            total_sub = (val / peak) * (height * 8)
            row_sub = int(total_sub - r * 8)
            if row_sub <= 0:
                row_text.append(" ")
            elif row_sub >= 8:
                row_text.append("█", style="#80b6af")
            else:
                row_text.append(sub_blocks[row_sub], style="#5caaa3")
        res.append_text(row_text)
        res.append("\n")

    x_axis = Text(f"{'':>7} ┴" + "─" * (len(series) + 1), style="dim")
    res.append_text(x_axis)
    res.append("\n")

    date_line = [" "] * (len(series) + 4)
    step = max(7, len(series) // 6)
    for i in range(0, len(series), step):
        d_str = chart_days[i].strftime("%m/%d")
        if i + len(d_str) <= len(date_line):
            for j, c in enumerate(d_str):
                date_line[i + j] = c
    res.append_text(Text(f"{'':>9}" + "".join(date_line).rstrip(), style="dim"))
    return res


def trend_text(days: tuple[DailyUsage, ...], language: str = "zh-CN", *, width: int = 80) -> Text:
    """Backwards-compatible wrapper that renders the vertical bar chart."""
    return render_vertical_bar_chart(days, width=width, height=6, language=language)
