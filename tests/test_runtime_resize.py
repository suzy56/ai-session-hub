from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import DataTable, TextArea

from ai_session_hub.config import AppConfig
from ai_session_hub.runtime_monitor import RuntimeSnapshot
from ai_session_hub.ui import HubApp


class TestRuntimeSurfaceLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_live_resize_stacks_details_then_restores_split_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "ai_session_hub.ui.collect_runtime", return_value=RuntimeSnapshot(time.time())
        ):
            app = HubApp(AppConfig(Path(directory), discover_defaults=False))
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.press("f4")
                processes = app.query_one("#runtime-processes", DataTable)
                details = app.query_one("#runtime-details", TextArea)
                await pilot.pause()
                self.assertLess(processes.region.x, details.region.x)
                self.assertEqual(processes.region.y, details.region.y)

                await pilot.resize_terminal(80, 24)
                await pilot.pause()
                self.assertTrue(app.main_screen.has_class("narrow"))
                self.assertGreaterEqual(details.region.y, processes.region.bottom)
                self.assertGreater(details.region.height, 2)
                self.assertEqual(details.region.x, processes.region.x)
                await pilot.press("tab")
                self.assertIs(app.focused, details)

                await pilot.resize_terminal(140, 40)
                await pilot.pause()
                self.assertFalse(app.main_screen.has_class("narrow"))
                self.assertEqual(processes.region.y, details.region.y)
                self.assertGreater(details.region.x, processes.region.x)

    async def test_force_quit_actually_stops_message_processing(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "ai_session_hub.ui.collect_runtime", return_value=RuntimeSnapshot(time.time())
        ):
            app = HubApp(AppConfig(Path(directory), discover_defaults=False))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("f4")
                self.assertTrue(app.is_running)
                # Schedule the real action, as native-terminal automation does.
                # Checking only _exit would miss a rejected ExitApp message.
                app.call_later(app.action_force_quit)

                async def stopped() -> None:
                    while app.is_running:
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(stopped(), timeout=3)
                self.assertFalse(app.is_running)
            self.assertEqual(app.return_value, 0)
