from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import TextArea

from ai_session_hub.config import AppConfig
from ai_session_hub.runtime_monitor import ClaudeProcess, ProcessInfo, RuntimeSnapshot
from ai_session_hub.ui import HubApp
from ai_session_hub.ui_runtime import RuntimeView


class TestRuntimeLifecycle(unittest.IsolatedAsyncioTestCase):
    async def _until(self, predicate) -> None:
        async def check() -> None:
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(check(), timeout=5)

    def _snapshot(self, sequence: int, *, pid: int = 5100) -> RuntimeSnapshot:
        process = ProcessInfo(pid, 5000, f"start-{sequence}", "00:01", "/tmp/claude", "claude")
        ancestor = ProcessInfo(5000, 1, "parent-start", "00:02", "/bin/parent", f"parent-evidence-{sequence}")
        return RuntimeSnapshot(float(sequence), (ClaudeProcess(process, ancestors=(ancestor,)),))

    async def test_hidden_inflight_result_cannot_replace_reentered_view(self) -> None:
        entered, release = threading.Event(), threading.Event()
        calls = []

        def collect(cancelled=lambda: False, timeout=10.0):
            calls.append(len(calls) + 1)
            sequence = calls[-1]
            if sequence == 1:
                entered.set()
                release.wait(5)
            return self._snapshot(sequence, pid=5100 + sequence)

        with tempfile.TemporaryDirectory() as directory, patch("ai_session_hub.ui.collect_runtime", side_effect=collect):
            app = HubApp(AppConfig(Path(directory), discover_defaults=False))
            try:
                async with app.run_test(size=(120, 36)) as pilot:
                    await pilot.press("f4")
                    await self._until(entered.is_set)
                    await pilot.press("f2", "f4")
                    self.assertEqual(calls, [1], "reentry must not overlap the still-running scan")
                    release.set()
                    view = app.query_one("#runtime-view", RuntimeView)
                    await self._until(lambda: view.snapshot is not None and view.snapshot.collected_at >= 2)
                    self.assertEqual(view.snapshot.processes[0].process.pid, 5102)
                    details = app.query_one("#runtime-details", TextArea).text
                    self.assertNotIn("parent-evidence-1", details)
                    self.assertIn("parent-evidence-2", details)
            finally:
                release.set()

    async def test_handoff_pauses_collection_and_manual_refresh_resumes_it(self) -> None:
        calls = []

        def collect(cancelled=lambda: False, timeout=10.0):
            calls.append(len(calls) + 1)
            return self._snapshot(calls[-1])

        with tempfile.TemporaryDirectory() as directory, patch("ai_session_hub.ui.collect_runtime", side_effect=collect):
            app = HubApp(AppConfig(Path(directory), discover_defaults=False))
            async with app.run_test(size=(120, 36)) as pilot:
                await pilot.press("f4")
                view = app.query_one("#runtime-view", RuntimeView)
                await self._until(lambda: view.snapshot is not None and view.snapshot.collected_at == 1)
                app._handoff = True
                try:
                    await pilot.press("ctrl+r")
                    await pilot.pause(0.1)
                    self.assertEqual(calls, [1])
                    self.assertEqual(view.snapshot.collected_at, 1)
                finally:
                    app._handoff = False
                await pilot.press("ctrl+r")
                await self._until(lambda: view.snapshot.collected_at == 2)
                self.assertEqual(calls, [1, 2])

    async def test_reused_pid_replaces_selected_ancestry_instead_of_retaining_old_details(self) -> None:
        calls = []

        def collect(cancelled=lambda: False, timeout=10.0):
            calls.append(len(calls) + 1)
            return self._snapshot(calls[-1])

        with tempfile.TemporaryDirectory() as directory, patch("ai_session_hub.ui.collect_runtime", side_effect=collect):
            app = HubApp(AppConfig(Path(directory), discover_defaults=False))
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("f4")
                view = app.query_one("#runtime-view", RuntimeView)
                await self._until(lambda: view.snapshot is not None and view.snapshot.collected_at == 1)
                self.assertIn("parent-evidence-1", app.query_one("#runtime-details", TextArea).text)
                await pilot.press("ctrl+r")
                await self._until(lambda: view.snapshot.collected_at == 2)
                self.assertIsNone(view.selected_identity)
                details = app.query_one("#runtime-details", TextArea).text
                self.assertNotIn("parent-evidence-1", details)
                self.assertNotIn("parent-evidence-2", details)
                await pilot.press("enter")
                self.assertEqual(view.selected_identity, (5100, "start-2"))
                self.assertIn("parent-evidence-2", app.query_one("#runtime-details", TextArea).text)
