from __future__ import annotations

import json
import datetime
import tempfile
import unittest
from pathlib import Path

from textual.widgets import Button, ContentSwitcher, DataTable, Input, Label, OptionList, Select, Static, TextArea, Tree

from ai_session_hub.config import AppConfig
from ai_session_hub.models import SourceSpec
from ai_session_hub.ui import HubApp, ResumeConfirmScreen, SearchResultsReady
from ai_session_hub.i18n import load_language, save_language
from ai_session_hub.ui_dialogs import FilterScreen, SettingsScreen


class TestUI(unittest.IsolatedAsyncioTestCase):
    """Test Textual UI interaction, keyboard navigation, and responsive layouts."""

    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()

        # Create source JSONL files so indexer keeps them
        sess_dir = self.root / "sessions"
        sess_dir.mkdir(parents=True)
        (sess_dir / "apple.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"id": "1", "cwd": str(self.root)}}) + "\n" +
            json.dumps({"type": "response_item", "payload": {"id": "m1", "role": "user", "content": [{"text": "I like red apples."}]}}) + "\n",
            encoding="utf-8",
        )
        (sess_dir / "banana.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"id": "2", "cwd": str(self.root)}}) + "\n" +
            json.dumps({"type": "response_item", "payload": {"id": "m2", "role": "user", "content": [{"text": "I like yellow bananas."}]}}) + "\n",
            encoding="utf-8",
        )

        src = SourceSpec(tool="codex", root=self.root)
        self.config = AppConfig(
            data_dir=self.data_dir,
            discover_defaults=False,
            sources=[src],
        )

    def tearDown(self) -> None:
        self.td.cleanup()

    async def test_search_and_preview_interaction(self) -> None:
        app = HubApp(self.config)
        async with app.run_test(size=(120, 40)) as pilot:
            # Let initial scan/search settle
            await pilot.pause(0.2)

            table = app.query_one("#sessions", DataTable)
            self.assertEqual(table.row_count, 2)

            # Focus search and type 'apple'
            await pilot.press("/")
            input_widget = app.query_one("#query", Input)
            self.assertTrue(input_widget.has_focus)

            input_widget.value = "apple"
            # Allow debounce timer (0.15s) and worker to process
            await pilot.pause(0.3)

            # Verify table filtered to 1 result
            self.assertEqual(table.row_count, 1)

            preview = app.query_one("#preview", TextArea)
            self.assertIn("I like red apples.", preview.text)
            self.assertIn("codex", str(app.query_one("#preview-meta", Label).content))

    async def test_responsive_layout_narrow_mode(self) -> None:
        app = HubApp(self.config)
        # Run at narrow terminal size (80x24)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.1)
            await pilot.press("f2")
            await pilot.pause()

            table = app.query_one("#sessions", DataTable)
            preview = app.query_one("#preview", TextArea)
            self.assertGreater(table.region.width, 60)
            self.assertFalse(app.query_one("#preview-container").display)
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(preview.has_focus)
            self.assertIn("apples", preview.text)
            self.assertGreater(preview.region.width, 60)
            self.assertFalse(table.display)
            await pilot.press("escape")
            await pilot.pause()
            self.assertTrue(table.has_focus)
            self.assertTrue(table.display)

    async def test_stale_query_generation_discarded(self) -> None:
        app = HubApp(self.config)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.1)

            old_generation = app.current_generation
            old_page = app.store.search("apple", app._get_current_filters())
            app.query_one("#query", Input).value = "banana"
            await pilot.pause(0.3)
            app.post_message(SearchResultsReady(generation=old_generation, page=old_page))
            await pilot.pause()
            table = app.query_one("#sessions", DataTable)
            self.assertEqual(table.row_count, 1)
            self.assertIn("bananas", str(table.get_row_at(0)[2]))

    async def test_new_query_resets_page(self) -> None:
        app = HubApp(self.config)
        async with app.run_test(size=(120, 40)) as pilot:
            app.page_size = 1
            await pilot.press("f2")
            app._trigger_search()
            await pilot.pause(0.2)
            app.action_next_page()
            await pilot.pause(0.2)
            self.assertEqual(app.query_one("#sessions", DataTable).row_count, 1)
            app.query_one("#query", Input).value = "apple"
            await pilot.pause(0.3)
            table = app.query_one("#sessions", DataTable)
            self.assertEqual(table.row_count, 1)
            self.assertIn("apples", str(table.get_row_at(0)[2]))

    async def test_usage_project_drilldown_preserves_native_session(self) -> None:
        source = self.root / "omp"
        sessions = source / "sessions"
        sessions.mkdir(parents=True)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for index, scale in enumerate((1, 2)):
            project = self.root / f"workspace-{index}" / "same-name"
            project.mkdir(parents=True)
            records = [
                {"type": "session", "id": f"usage-{index}", "cwd": str(project), "timestamp": now},
                {"type": "message", "id": f"answer-{index}", "timestamp": now, "message": {
                    "role": "assistant", "model": "fixture-model", "provider": "fixture", "timestamp": now,
                    "content": [{"type": "text", "text": f"Native answer {index}"}],
                    "usage": {"input": 100 * scale, "output": 10 * scale, "cacheRead": 20 * scale,
                              "cacheWrite": 0, "totalTokens": 130 * scale, "cost": {"total": 0.013 * scale}},
                }},
            ]
            (sessions / f"{index}.jsonl").write_text("\n".join(map(json.dumps, records)) + "\n")
        self.config.sources = [SourceSpec("omp", source)]
        app = HubApp(self.config)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.4)
            self.assertIn("390", str(app.query_one("#kpi-tokens-val", Static).content))
            await pilot.press("f3")
            await pilot.pause(0.2)
            tree = app.query_one("#project-tree", Tree)
            self.assertEqual(len(tree.root.children), 2)
            # Expand first project folder
            first_folder = tree.root.children[0]
            first_folder.expand()
            await pilot.pause(0.3)
            self.assertEqual(len(first_folder.children), 1)
            # Highlight the session leaf
            leaf = first_folder.children[0]
            tree.select_node(leaf)
            await pilot.pause(0.3)
            preview = app.query_one("#project-preview", TextArea).text
            self.assertIn("Native answer", preview)
            self.assertIn("fixture-model", str(app.query_one("#project-preview-meta", Label).content))
            selected = app.selected_session_key
            snapshot = app.store.get_session(selected)
            native_path = next(path for path in snapshot.ref.locators if path.suffix == ".jsonl")
            with native_path.open("a") as stream:
                stream.write(json.dumps({"type": "message", "id": "new-answer", "message": {
                    "role": "assistant", "content": [{"type": "text", "text": "New reply after refresh"}]
                }}) + "\n")
            app.action_refresh_index()
            await pilot.pause(.5)
            self.assertEqual(app.selected_session_key, selected)
            self.assertIn("New reply after refresh", app.query_one("#project-preview", TextArea).text)
            await pilot.press("escape")
            tree.move_cursor(first_folder)
            await pilot.pause()
            await pilot.press("f2", "f3")
            self.assertIsNone(app.selected_session_key)

    async def test_language_switch_preserves_search_reader_and_background_updates(self) -> None:
        save_language(self.data_dir, "en")
        app = HubApp(self.config)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(.3)
            await pilot.press("/")
            app.query_one("#query", Input).value = "apple"
            await pilot.pause(.3)
            await pilot.press("escape", "enter")
            preview = app.query_one("#preview", TextArea)
            key = app.selected_session_key
            preview.move_cursor((1, 5))
            selection = preview.selection
            await pilot.press("comma")
            self.assertIsInstance(app.screen, SettingsScreen)
            # A real worker result must target the workspace behind the modal.
            app._trigger_search()
            app._trigger_analytics()
            await pilot.pause(.3)
            app.screen.query_one("#settings-language", Select).value = "zh-CN"
            await pilot.click("#settings-apply")
            await pilot.pause(.3)
            self.assertEqual(app.language, "zh-CN")
            self.assertEqual(app.selected_session_key, key)
            self.assertEqual(app.query_one("#query", Input).value, "apple")
            self.assertTrue(preview.has_focus)
            self.assertEqual(preview.selection, selection)
            self.assertIn("用户", preview.text)
            self.assertIn("I like red apples.", preview.text)
            self.assertEqual(str(app.query_one("#nav-sessions", Button).label), "会话")
            await pilot.press("comma")
            app.screen.query_one("#settings-language", Select).value = "en"
            await pilot.click("#settings-apply")
            await pilot.pause()
            self.assertIn("User", preview.text)
            self.assertEqual(app.selected_session_key, key)
        restarted = HubApp(self.config)
        async with restarted.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(str(restarted.query_one("#nav-sessions", Button).label), "Sessions")

    async def test_filter_draft_cancel_apply_reset_and_typing_safety(self) -> None:
        app = HubApp(self.config)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(.3)
            await pilot.press("f")
            self.assertIsInstance(app.screen, FilterScreen)
            app.screen.query_one("#filter-query", Input).value = "claude"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.press("escape")
            self.assertEqual(app.query_one("#sessions", DataTable).row_count, 2)
            await pilot.press("f")
            app.screen.query_one("#filter-query", Input).value = "claude"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.click("#filter-apply")
            await pilot.pause(.3)
            self.assertEqual(app.query_one("#sessions", DataTable).row_count, 0)
            await pilot.press("f")
            await pilot.click("#filter-reset")
            await pilot.click("#filter-apply")
            await pilot.pause(.3)
            self.assertEqual(app.query_one("#sessions", DataTable).row_count, 2)
            await pilot.press("/", "f", "r", "q", "comma")
            self.assertEqual(app.query_one("#query", Input).value, "frq,")
            self.assertEqual(len(app.screen_stack), 1)

    async def test_project_paging_reaches_last_session_and_keeps_selection(self) -> None:
        source = self.root / "omp"
        sessions = source / "sessions"
        sessions.mkdir(parents=True)
        project = self.root / "workspace-with-a-long-name" / "same-name"
        project.mkdir(parents=True)
        for index in range(205):
            records = [
                {"type": "session", "id": f"paged-{index:03}", "title": f"Session {index:03}",
                 "cwd": str(project), "timestamp": "2026-09-01T12:00:00Z"},
                {"type": "message", "id": "answer", "message": {"role": "assistant",
                 "content": [{"type": "text", "text": f"Unique answer {index:03}"}]}},
            ]
            (sessions / f"{index}.jsonl").write_text("\n".join(map(json.dumps, records)) + "\n")
        self.config.sources = [SourceSpec("omp", source)]
        app = HubApp(self.config)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(.7)
            await pilot.press("f3")
            tree = app.query_one("#project-tree", Tree)
            folder = tree.root.children[0]
            folder.expand()
            await pilot.pause(.3)
            for _ in range(2):
                more = next(n for n in folder.children if n.data.get("type") == "more")
                tree.select_node(more)
                await pilot.pause(.3)
            leaves = [n for n in folder.children if n.data.get("type") == "session"]
            self.assertEqual(len({n.data["session_key"] for n in leaves}), 205)
            self.assertFalse(any(n.data.get("type") == "more" for n in folder.children))
            tree.select_node(leaves[-1])
            await pilot.pause(.3)
            key = app.selected_session_key
            native_id = app.store.get_session(key).ref.native_id
            self.assertIn(f"Unique answer {native_id.removeprefix('paged-')}", app.query_one("#project-preview", TextArea).text)
            await pilot.press("comma")
            app.screen.query_one("#settings-language", Select).value = "zh-CN" if app.language == "en" else "en"
            await pilot.click("#settings-apply")
            await pilot.pause(.3)
            self.assertEqual(app.selected_session_key, key)
            self.assertEqual(app.query_one("#views", ContentSwitcher).current, "projects-view")
            self.assertTrue(folder.is_expanded)
            await pilot.press("escape", "f")
            await pilot.click("#filter-tab-project_key")
            app.screen.query_one("#filter-query", Input).value = "workspace-with-a-long-name"
            await pilot.pause()
            self.assertEqual(app.screen.query_one("#filter-results", OptionList).option_count, 1)
            await pilot.press("enter")
            await pilot.click("#filter-apply")
            await pilot.pause(.3)
            self.assertEqual(app._get_current_filters().project_key, str(project.resolve()))

    async def test_confirmation_dialog_cancel(self) -> None:
        app = HubApp(self.config)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)

            # Select first session
            app.selected_session_key = "key-apple"

            # Push confirmation screen
            confirm_screen = ResumeConfirmScreen(
                tool="codex",
                native_id="1",
                cwd=self.root,
                profile=None,
            )
            app.push_screen(confirm_screen)
            await pilot.pause(0.1)

            self.assertIsInstance(app.screen, ResumeConfirmScreen)

            # Press Escape to cancel
            await pilot.press("escape")
            await pilot.pause(0.1)

            self.assertNotIsInstance(app.screen, ResumeConfirmScreen)

    async def test_confirmation_enter_honors_focused_action(self) -> None:
        app = HubApp(self.config)
        decisions = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.push_screen(ResumeConfirmScreen("omp", "exact-id", self.root, None), callback=decisions.append)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(decisions, [False])
            app.push_screen(ResumeConfirmScreen("omp", "exact-id", self.root, None), callback=decisions.append)
            await pilot.pause()
            await pilot.press("tab", "enter")
            await pilot.pause()
            self.assertEqual(decisions, [False, True])

    async def test_token_monitor_dashboard_subtabs_and_kpis(self) -> None:
        app = HubApp(self.config)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.5)
            # Switch to dashboard view
            await pilot.press("f1")
            await pilot.pause(0.2)
            self.assertEqual(app.query_one("#dash-switcher", ContentSwitcher).current, "dash-overview-pane")
            # Verify 8 KPI cards
            for kpi_id in ("#kpi-tokens-val", "#kpi-cost-val", "#kpi-active-days-val", "#kpi-streak-val",
                           "#kpi-peak-day-val", "#kpi-top-model-val", "#kpi-messages-val", "#kpi-sessions-val"):
                self.assertTrue(app.query_one(kpi_id, Static).content)
            # Verify activity heatmap
            heatmap = app.query_one("#activity-heatmap", Static)
            self.assertIsNotNone(heatmap.content)
            # Verify model table and tool table
            model_table = app.query_one("#model-table", DataTable)
            tool_table = app.query_one("#tool-table", DataTable)
            self.assertGreaterEqual(model_table.row_count, 0)
            self.assertGreaterEqual(tool_table.row_count, 0)
            # Switch to Trend tab
            await pilot.click("#dash-tab-trend")
            await pilot.pause(0.2)
            self.assertEqual(app.query_one("#dash-switcher", ContentSwitcher).current, "dash-trend-pane")
            trend_chart = app.query_one("#usage-trend", Static)
            self.assertIsNotNone(trend_chart.content)
            daily_table = app.query_one("#daily-table", DataTable)
            self.assertGreaterEqual(daily_table.row_count, 0)
            # Switch back to Overview tab
            await pilot.click("#dash-tab-overview")
            await pilot.pause(0.2)
            self.assertEqual(app.query_one("#dash-switcher", ContentSwitcher).current, "dash-overview-pane")

    async def test_project_tree_aligned_columns_and_header(self) -> None:
        app = HubApp(self.config)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.5)
            await pilot.press("f3")
            await pilot.pause(0.2)
            col_header = app.query_one("#project-tree-columns", Label)
            self.assertIn("Project", str(col_header.content))
            self.assertIn("Tokens", str(col_header.content))
            self.assertIn("Cost", str(col_header.content))
            tree = app.query_one("#project-tree", Tree)
            self.assertGreaterEqual(len(tree.root.children), 1)

    async def test_header_navigation_and_filters_unwrapped(self) -> None:
        app = HubApp(self.config)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.2)
            # Verify all expected buttons exist in header
            self.assertIsNotNone(app.query_one("#nav-sessions", Button))
            self.assertIsNotNone(app.query_one("#nav-projects", Button))
            self.assertIsNotNone(app.query_one("#nav-dashboard", Button))
            self.assertIsNotNone(app.query_one("#open-filters", Button))
            self.assertIsNotNone(app.query_one("#nav-settings", Button))
            # Click open filters
            await pilot.click("#open-filters")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, FilterScreen)
            await pilot.press("escape")
            await pilot.pause(0.1)
            self.assertNotIsInstance(app.screen, FilterScreen)
    async def test_project_filter_aligned_formatting(self) -> None:
        from ai_session_hub.ui_dialogs import format_project_option, shorten_path
        home = str(Path.home())
        self.assertEqual(shorten_path(home + "/Desktop/test"), "~/Desktop/test")
        self.assertEqual(shorten_path(home), "~")
        formatted = format_project_option("long-project-name-12345", home + "/Desktop/test", True, width=80)
        self.assertTrue(formatted.plain.startswith("> long-project-name-12345"))
        self.assertIn("~/Desktop/test", formatted.plain)
        self.assertNotIn("\n", formatted.plain)

if __name__ == "__main__":
    unittest.main()
