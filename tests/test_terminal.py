from __future__ import annotations

import os
import pty
import signal
import stat
import tempfile
import termios
import unittest
from pathlib import Path

from ai_session_hub.models import LaunchSpec
from ai_session_hub.resume import run_foreground


class TestTerminalLifecycle(unittest.TestCase):
    """Test foreground CLI execution, PTY allocation, and terminal state restoration."""

    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_pty_child_isatty_and_cooked_mode(self) -> None:
        # Create a Python script that asserts isatty on 0, 1, 2 and writes status
        script_path = self.root / "pty_child.py"
        status_file = self.root / "pty_status.txt"
        script_content = f"""import sys, os, tty
status = f"0:{{sys.stdin.isatty()}}, 1:{{sys.stdout.isatty()}}, 2:{{sys.stderr.isatty()}}"
with open('{status_file}', 'w') as f:
    f.write(status)
tty.setraw(0)
sys.exit(0)
"""
        script_path.write_text(script_content, encoding="utf-8")

        master_fd, slave_fd = pty.openpty()
        try:
            # Save original stdin/stdout/stderr
            old_stdin = os.dup(0)
            old_stdout = os.dup(1)
            old_stderr = os.dup(2)

            # Redirect std fds to slave PTY
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)

            try:
                before = termios.tcgetattr(0)
                spec = LaunchSpec(
                    argv=("/usr/bin/python3", str(script_path)),
                    cwd=self.root,
                    env_overrides={},
                )
                ret = run_foreground(spec)
                self.assertEqual(ret, 0)
                after = termios.tcgetattr(0)
                # macOS sets PENDIN when canonical input is restored; it is a
                # transient retype flag, not a caller-configured terminal mode.
                transient = getattr(termios, "PENDIN", 0)
                before[3] &= ~transient
                after[3] &= ~transient
                self.assertEqual(after, before)
            finally:
                # Restore original std fds
                os.dup2(old_stdin, 0)
                os.dup2(old_stdout, 1)
                os.dup2(old_stderr, 2)
                os.close(old_stdin)
                os.close(old_stdout)
                os.close(old_stderr)
        finally:
            os.close(master_fd)
            os.close(slave_fd)

        self.assertTrue(status_file.exists())
        status = status_file.read_text(encoding="utf-8")
        self.assertEqual(status, "0:True, 1:True, 2:True")

    def test_sigint_restoration_after_foreground_exit(self) -> None:
        # Check that SIGINT handler is restored in parent after run_foreground exits
        def dummy_handler(signum: int, frame: object) -> None:
            pass

        old_sig = signal.signal(signal.SIGINT, dummy_handler)
        try:
            spec = LaunchSpec(
                argv=("/usr/bin/python3", "-c", "import sys; sys.exit(0)"),
                cwd=self.root,
            )
            ret = run_foreground(spec)
            self.assertEqual(ret, 0)

            # Verify current handler is still dummy_handler
            current_sig = signal.getsignal(signal.SIGINT)
            self.assertEqual(current_sig, dummy_handler)
        finally:
            signal.signal(signal.SIGINT, old_sig)

    def test_nonzero_exit_code_propagation(self) -> None:
        spec = LaunchSpec(
            argv=("/usr/bin/python3", "-c", "import sys; sys.exit(42)"),
            cwd=self.root,
        )
        ret = run_foreground(spec)
        self.assertEqual(ret, 42)


if __name__ == "__main__":
    unittest.main()
