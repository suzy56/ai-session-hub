from __future__ import annotations

import ctypes
import errno
import io
import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_session_hub import runtime_monitor as monitor


START = "Tue Oct 20 12:30:00 2026"
REUSED = "Tue Oct 20 12:31:00 2026"


def row(pid: int, ppid: int, executable: str, command: str | list[str] | None = None, started: str = START) -> tuple:
    argv = shlex.split(command) if isinstance(command, str) else command if command is not None else [executable]
    return pid, ppid, executable, argv, started


def fields(*items: str) -> bytes:
    return b"\0".join(item.encode() for item in items) + b"\0\n"


class NativeFixture:
    """Only native observations are replaced; consumers receive real snapshots."""

    def __init__(self, root: Path, rows: list[tuple] | None = None):
        self.root = root
        self.rows = rows if rows is not None else [row(1, 0, "/sbin/launchd"), row(30, 1, "/bin/zsh"), row(40, 30, "/opt/bin/claude")]
        self.refreshed: list[tuple] | None = None
        self.metadata_calls = 0
        self.network = b""
        self.network_stderr = b""
        self.network_code = 1
        self.network_timeout = False
        self.malformed_ps = b""
        self.cwd_missing = False
        self.cwd_stderr = b""
        self.cwd_path = str(root)
        self.panes = b"ccb-session|%7|30\n"
        self.sessions = b"ccb-session|0\n"
        self.missing: set[str] = set()
        self.timeout_ps = False
        self.argument_errors: dict[int, Exception] = {}

    def arguments(self, pid: int) -> list[str]:
        if pid in self.argument_errors:
            raise self.argument_errors[pid]
        for item_pid, _, _, argv, _ in self.rows:
            if item_pid == pid:
                return list(argv)
        raise FileNotFoundError(errno.ENOENT, "fixture process exited")

    def which(self, tool: str) -> str | None:
        return None if tool in self.missing else f"/fixture/bin/{tool}"

    def run(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        # Native commands are never shell-expanded, elevated or allowed an unbounded wait.
        if kwargs["shell"] or not 0 < kwargs["timeout"] <= 2 or kwargs["env"]["LC_ALL"] != "C":
            raise AssertionError("Unsafe native invocation")
        tool = Path(argv[0]).name
        if tool == "ps":
            if self.timeout_ps:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            if "pid=,args=" in argv:
                # Deliberately flattened, like real ps: never an argv authority.
                output = "".join(f"{pid} {' '.join(command)}\n" for pid, _, _, command, _ in self.rows).encode()
            else:
                self.metadata_calls += 1
                rows = self.refreshed if self.metadata_calls > 1 and self.refreshed is not None else self.rows
                output = "".join(f"{pid} {ppid} {started} 01:02 {executable}\n" for pid, ppid, executable, _, started in rows).encode() + self.malformed_ps
            return subprocess.CompletedProcess(argv, 0, output, b"")
        if tool == "lsof":
            if "-i" in argv:
                if self.network_timeout:
                    raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
                return subprocess.CompletedProcess(argv, self.network_code, self.network, self.network_stderr)
            pids = argv[argv.index("-p") + 1].split(",")
            output = b"" if self.cwd_missing else b"".join(fields(f"p{pid}", "fcwd", f"n{self.cwd_path}") for pid in pids)
            return subprocess.CompletedProcess(argv, 1 if self.cwd_missing else 0, output, self.cwd_stderr)
        if tool == "tmux":
            return subprocess.CompletedProcess(argv, 0, self.panes if "list-panes" in argv else self.sessions, b"")
        raise AssertionError(f"Unexpected executable: {tool}")


class TestRuntimeMonitor(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.fixture = NativeFixture(self.root)
        original_read = monitor._read_json

        def read_metadata(path: Path, scan):
            # Never inspect the host's /tmp or home metadata in deterministic tests.
            return original_read(path, scan) if path.is_relative_to(self.root) else {}

        self.read_patch = patch.object(monitor, "_read_json", side_effect=read_metadata)
        self.read_patch.start()
        self.addCleanup(self.read_patch.stop)
        self.runner = patch.object(monitor.subprocess, "run", side_effect=self.fixture.run)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        resolver = patch.object(monitor.shutil, "which", side_effect=self.fixture.which)
        resolver.start()
        self.addCleanup(resolver.stop)
        arguments = patch.object(monitor, "_native_argv", side_effect=self.fixture.arguments)
        arguments.start()
        self.addCleanup(arguments.stop)

    def collect(self, **kwargs) -> monitor.RuntimeSnapshot:
        return monitor.collect_runtime(**kwargs)

    def metadata(self, *, lease: dict | None = None, lifecycle: dict | None = None, state: dict | None = None) -> None:
        directory = self.root / ".ccb" / "ccbd"
        directory.mkdir(parents=True)
        state = state if state is not None else {"record_type": "ccbd_project_namespace_state", "schema_version": 2, "tmux_session_name": "ccb-session", "tmux_socket_path": str(self.root / "existing.sock")}
        for name, data in (("state", state), ("lease", lease or {}), ("lifecycle", lifecycle or {})):
            (directory / f"{name}.json").write_text(json.dumps(data))

    def test_native_and_node_entrypoints_exclude_argument_mentions(self) -> None:
        self.fixture.rows = [
            row(1, 0, "/sbin/init"),
            row(10, 1, "/opt/Claude Tools/claude", "'/opt/Claude Tools/claude' --print 'private prompt'"),
            row(11, 1, "claude.exe"),
            row(12, 1, "/usr/bin/node", "node /opt/node_modules/@anthropic-ai/claude-code/cli.js --print prompt"),
            row(13, 1, "/usr/bin/node", "node --no-warnings /opt/node_modules/@anthropic-ai/claude-code/cli.js"),
            row(20, 1, "/bin/bash", "bash -c 'claude --print prompt'"),
            row(21, 1, "/usr/bin/python3", "python3 inspect.py claude"),
            row(22, 1, "/usr/bin/node", "node application.js /opt/@anthropic-ai/claude-code/cli.js"),
            row(23, 1, "/usr/bin/node", "node -e 'require(\"@anthropic-ai/claude-code/cli.js\")'"),
            row(24, 1, "/usr/bin/claude-helper"),
            row(25, 1, "/usr/bin/node", "node /opt/@anthropic-ai/claude-code/cli.js.backup"),
        ]
        snapshot = self.collect()
        self.assertEqual({p.process.pid for p in snapshot.processes}, {10, 11, 12, 13})
        self.assertTrue(snapshot.process_complete)
        self.assertEqual(snapshot.processes[0].process.executable, "/opt/Claude Tools/claude")
        self.assertNotIn("private prompt", repr(snapshot))

    def test_actual_node_entrypoint_with_spaces_and_quotes_is_recognized(self) -> None:
        self.fixture.rows = [
            row(1, 0, "/sbin/init"),
            row(40, 1, "node", ["node", "/opt/Claude's Tools/@anthropic-ai/claude-code/cli.js", "--print", "private prompt"]),
            row(41, 1, "node", ["node", "/opt/@anthropic-ai/claude-code/cli.js backup"]),
        ]
        snapshot = self.collect()
        self.assertEqual({process.process.pid for process in snapshot.processes}, {40})
        self.assertTrue(snapshot.process_complete)
        self.assertNotIn("private prompt", repr(snapshot))

    def test_native_claude_does_not_need_to_read_its_prompt_arguments(self) -> None:
        self.fixture.argument_errors[40] = PermissionError(errno.EACCES, "prompt arguments unavailable")
        snapshot = self.collect()
        self.assertTrue(snapshot.process_complete)
        self.assertEqual(snapshot.processes[0].process.command, "/opt/bin/claude [arguments hidden]")

    def test_verified_empty_census_is_not_a_tool_failure(self) -> None:
        self.fixture.rows = [row(1, 0, "/sbin/init")]
        snapshot = self.collect()
        self.assertEqual(snapshot.processes, ())
        self.assertTrue(snapshot.process_complete)
        self.assertEqual(snapshot.issues, ())

    def test_ordered_parent_chain_and_process_identity(self) -> None:
        observed = self.collect().processes[0]
        self.assertEqual(observed.process.ppid, 30)
        self.assertEqual([p.pid for p in observed.ancestors], [30, 1])
        self.assertEqual(observed.process.identity, (40, START))
        self.assertEqual(observed.process.cwd, str(self.root))
        self.assertTrue(observed.ancestry_complete)

    def test_missing_parent_is_explicit_and_not_reconstructed(self) -> None:
        self.fixture.rows = [row(40, 999, "claude")]
        observed = self.collect().processes[0]
        self.assertEqual(observed.ancestors, ())
        self.assertFalse(observed.ancestry_complete)
        self.assertTrue(any(issue.code == "incomplete" for issue in observed.issues))

    def test_cycles_terminate_with_partial_parent_evidence(self) -> None:
        self.fixture.rows = [row(40, 30, "claude"), row(30, 40, "/bin/zsh")]
        observed = self.collect().processes[0]
        self.assertEqual([p.pid for p in observed.ancestors], [30])
        self.assertFalse(observed.ancestry_complete)

    def test_reused_claude_pid_discards_its_connections_and_details(self) -> None:
        self.fixture.network = fields("p40", "f7", "PTCP", "n127.0.0.1:9000", "TST=LISTEN")
        self.fixture.refreshed = [row(40, 30, "/usr/bin/other", started=REUSED)]
        snapshot = self.collect()
        self.assertEqual(snapshot.processes, ())
        self.assertFalse(snapshot.process_complete)
        self.assertTrue(any(i.code == "identity_changed" for i in snapshot.issues))

    def test_reused_ancestor_pid_cannot_show_replacement_program(self) -> None:
        self.fixture.refreshed = [self.fixture.rows[0], row(30, 1, "/usr/bin/replacement", started=REUSED), self.fixture.rows[2]]
        observed = self.collect().processes[0]
        self.assertEqual(observed.ancestors, ())
        self.assertFalse(observed.ancestry_complete)
        self.assertNotIn("replacement", repr(observed))

    def test_exit_and_reparenting_invalidate_old_pid_details(self) -> None:
        for replacement in ([], [row(40, 1, "claude")]):
            with self.subTest(replacement=replacement):
                self.fixture.metadata_calls = 0
                self.fixture.refreshed = replacement
                snapshot = self.collect()
                self.assertFalse(snapshot.processes)
                self.assertTrue(any(i.code in {"identity_changed", "exited"} for i in snapshot.issues))

    def test_pid_scoped_tcp_udp_ipv6_and_listener_semantics(self) -> None:
        self.fixture.network_code = 0
        self.fixture.network = (
            fields("p40", "f7", "PTCP", "n[::1]:9000", "TST=LISTEN")
            + fields("p40", "f8", "PTCP", "n127.0.0.1:5000->127.0.0.1:9000", "TST=ESTABLISHED")
            + fields("p40", "f9", "PUDP", "n[::1]:5001")
            + fields("p40", "f10", "PUDP", "n[::1]:5002->[::1]:5003")
            + fields("p30", "f11", "PTCP", "n127.0.0.1:7777", "TST=LISTEN")
        )
        observed = self.collect().processes[0]
        self.assertTrue(observed.network_complete)
        self.assertEqual(len(observed.connections), 4)
        listener, tcp, udp, connected_udp = observed.connections
        self.assertEqual((listener.local_address, listener.remote_address, listener.state), ("[::1]:9000", None, "LISTEN"))
        self.assertEqual((tcp.remote_address, tcp.state), ("127.0.0.1:9000", "ESTABLISHED"))
        self.assertEqual((udp.protocol, udp.local_address, udp.remote_address, udp.state), ("UDP", "[::1]:5001", None, None))
        self.assertEqual((connected_udp.remote_address, connected_udp.state), ("[::1]:5003", None))
        self.assertTrue(all(socket.pid == 40 for socket in observed.connections))

    def test_successful_no_match_is_no_observed_sockets(self) -> None:
        observed = self.collect().processes[0]
        self.assertEqual(observed.connections, ())
        self.assertTrue(observed.network_complete)

    def test_partial_batched_pid_coverage_is_not_silent_zero_activity(self) -> None:
        self.fixture.rows.append(row(41, 30, "claude"))
        self.fixture.network = fields("p40", "f7", "PTCP", "n*:9000", "TST=LISTEN")
        snapshot = self.collect()
        by_pid = {p.process.pid: p for p in snapshot.processes}
        self.assertTrue(by_pid[40].network_complete)
        self.assertFalse(by_pid[41].network_complete)
        self.assertEqual(by_pid[41].connections, ())
        self.assertTrue(any(i.source == "network" and i.pid == 41 and i.code == "incomplete" for i in snapshot.issues))

    def test_permission_diagnostics_preserve_partial_sockets(self) -> None:
        self.fixture.network = fields("p40", "f7", "PTCP", "n*:9000", "TST=LISTEN")
        self.fixture.network_stderr = b"lsof: permission denied: secret-value-never-displayed"
        snapshot = self.collect()
        self.assertEqual(len(snapshot.processes[0].connections), 1)
        self.assertFalse(snapshot.processes[0].network_complete)
        self.assertTrue(any(i.source == "network" and i.code == "denied" for i in snapshot.issues))
        self.assertNotIn("secret-value", repr(snapshot))

    def test_missing_lsof_is_unavailable_not_zero_activity(self) -> None:
        self.fixture.missing.add("lsof")
        snapshot = self.collect()
        self.assertTrue(snapshot.processes)
        self.assertFalse(snapshot.processes[0].network_complete)
        self.assertIsNone(snapshot.processes[0].process.cwd)
        self.assertTrue(any(i.source == "network" and i.code == "missing_tool" for i in snapshot.issues))

    def test_timeout_preserves_process_but_marks_network_incomplete(self) -> None:
        self.fixture.network_timeout = True
        snapshot = self.collect()
        self.assertTrue(snapshot.processes)
        self.assertFalse(snapshot.processes[0].network_complete)
        self.assertTrue(any(i.source == "network" and i.code == "timeout" for i in snapshot.issues))

    def test_missing_ps_and_timeout_do_not_claim_successful_empty_scan(self) -> None:
        self.fixture.missing.add("ps")
        snapshot = self.collect()
        self.assertFalse(snapshot.process_complete)
        self.assertEqual(snapshot.issues[0].code, "missing_tool")
        self.fixture.missing.clear()
        self.fixture.timeout_ps = True
        snapshot = self.collect()
        self.assertFalse(snapshot.process_complete)
        self.assertEqual(snapshot.issues[0].code, "timeout")

    def test_cancelled_and_expired_scans_are_explicit(self) -> None:
        for kwargs, code in (({"cancelled": lambda: True}, "cancelled"), ({"timeout": 0}, "timeout")):
            with self.subTest(code=code):
                snapshot = self.collect(**kwargs)
                self.assertFalse(snapshot.process_complete)
                self.assertEqual(snapshot.processes, ())
                self.assertTrue(any(i.code == code for i in snapshot.issues))

    def test_malformed_native_rows_remain_partial_not_empty_success(self) -> None:
        self.fixture.malformed_ps = b"not a process census row\n"
        self.fixture.network = fields("p40", "f7", "PTCP")
        snapshot = self.collect()
        self.assertEqual(len(snapshot.processes), 1)
        self.assertFalse(snapshot.process_complete)
        self.assertFalse(snapshot.processes[0].network_complete)
        self.assertTrue(any(i.source == "ps" and i.code == "malformed" for i in snapshot.issues))

    def test_unavailable_native_arguments_never_use_flattened_ps_fallback(self) -> None:
        self.fixture.rows = [row(1, 0, "/sbin/init"), row(40, 1, "node", ["node", "/opt/@anthropic-ai/claude-code/cli.js"])]
        self.fixture.argument_errors[40] = PermissionError(errno.EACCES, "denied")
        snapshot = self.collect()
        self.assertEqual(snapshot.processes, ())
        self.assertFalse(snapshot.process_complete)
        self.assertTrue(any(i.source == "argv" and i.code == "denied" for i in snapshot.issues))

    def test_nul_delimited_cwd_does_not_create_a_forged_record(self) -> None:
        self.fixture.cwd_path = str(self.root / "line\np999\nf7")
        observed = self.collect().processes[0]
        self.assertEqual(observed.process.cwd, str(self.root / "line p999 f7"))
        self.assertEqual(observed.connections, ())

    def test_missing_cwd_is_an_explicit_limitation(self) -> None:
        self.fixture.cwd_missing = True
        observed = self.collect().processes[0]
        self.assertIsNone(observed.process.cwd)
        self.assertTrue(any(i.source == "cwd" for i in observed.issues))

    def test_secrets_and_all_claude_prompt_forms_are_hidden(self) -> None:
        self.fixture.rows[1] = row(30, 1, "/usr/bin/launcher", "launcher --api-key=topsecret --token othersecret --password 'space secret' https://user:pass@example.test sk-abcdefghijk")
        self.fixture.rows[2] = row(40, 30, "claude", "claude --print 'private customer prompt' --system-prompt 'private system' --api-key=claudesecret")
        snapshot = self.collect()
        for secret in ("topsecret", "othersecret", "space secret", "user:pass", "sk-abcdefghijk", "private customer", "private system", "claudesecret"):
            self.assertNotIn(secret, repr(snapshot))
        self.assertIn("arguments hidden", snapshot.processes[0].process.command)
        self.assertIn("REDACTED", snapshot.processes[0].ancestors[0].command)

    def test_whole_multiword_secret_value_is_redacted_not_just_first_word(self) -> None:
        self.fixture.rows[1] = row(30, 1, "/usr/bin/launcher", ["launcher", "--password", "firstSensitive middleSensitive lastSensitive", "--safe", "visible"])
        snapshot = self.collect()
        for fragment in ("firstSensitive", "middleSensitive", "lastSensitive"):
            self.assertNotIn(fragment, repr(snapshot))
        self.assertIn("visible", snapshot.processes[0].ancestors[0].command)

    def test_unavailable_ancestor_arguments_hide_flattened_secrets(self) -> None:
        self.fixture.rows[1] = row(30, 1, "/usr/bin/launcher", ["launcher", "--password", "firstSensitive middleSensitive lastSensitive"])
        self.fixture.argument_errors[30] = PermissionError(errno.EPERM, "denied")
        snapshot = self.collect()
        process = snapshot.processes[0]
        self.assertFalse(process.ancestry_complete)
        self.assertIn("arguments unavailable", process.ancestors[0].command)
        for fragment in ("firstSensitive", "middleSensitive", "lastSensitive"):
            self.assertNotIn(fragment, repr(snapshot))

    def test_credential_headers_and_split_bearer_values_are_redacted(self) -> None:
        self.fixture.rows[1] = row(30, 1, "/usr/bin/launcher", "launcher -H 'Authorization: Basic privatebase64' --authorization Bearer privatebearer --credentials 'user:privatepassword'")
        snapshot = self.collect()
        for secret in ("privatebase64", "privatebearer", "privatepassword"):
            self.assertNotIn(secret, repr(snapshot))

    def test_shell_parent_does_not_leak_embedded_prompt(self) -> None:
        self.fixture.rows[1] = row(30, 1, "/bin/bash", "bash -c 'claude --print private-embedded-prompt'")
        snapshot = self.collect()
        self.assertNotIn("private-embedded-prompt", repr(snapshot))

    def test_detached_ccb_is_verified_from_existing_pane_not_attachment(self) -> None:
        self.metadata(lease={"ccbd_pid": 90, "keeper_pid": 91, "mount_state": "mounted"}, lifecycle={"desired_state": "running", "phase": "active"})
        self.fixture.rows += [row(90, 1, "/usr/bin/python3", f"python3 /opt/ccbd.py --project {self.root}"), row(91, 1, "/opt/keeper", f"keeper --project-dir={self.root}")]
        with patch.object(monitor, "_socket_exists", return_value=True):
            snapshot = self.collect()
        ccb = snapshot.processes[0].ccb[0]
        self.assertTrue(ccb.verified)
        self.assertEqual((ccb.project, ccb.session, ccb.pane, ccb.attached_clients), (str(self.root), "ccb-session", "%7", 0))
        self.assertTrue(all(controller.verified for controller in ccb.controllers))
        self.assertEqual(ccb.phase, "active")

    def test_controller_reused_during_authoritative_argv_read_is_not_verified(self) -> None:
        self.metadata(lease={"ccbd_pid": 90})
        self.fixture.rows.append(row(90, 1, "ccbd", ["ccbd", "--project", str(self.root)]))

        def arguments(pid: int) -> list[str]:
            result = self.fixture.arguments(pid)
            if pid == 90:
                self.fixture.refreshed = [item for item in self.fixture.rows if item[0] != 90] + [row(90, 1, "other", started=REUSED)]
            return result

        with patch.object(monitor, "_native_argv", side_effect=arguments):
            snapshot = self.collect()
        ccb = snapshot.processes[0].ccb[0]
        self.assertFalse(ccb.controllers[0].verified)
        self.assertTrue(any(issue.source == "argv" and issue.code == "identity_changed" for issue in ccb.issues))

    def test_stale_stopped_metadata_cannot_prove_live_controllers(self) -> None:
        self.metadata(lease={"ccbd_pid": 90, "keeper_pid": 91}, lifecycle={"desired_state": "stopped", "phase": "unmounted", "shutdown_intent": "user"})
        self.fixture.rows += [row(90, 1, "/usr/bin/unrelated", f"unrelated --project {self.root}")]
        snapshot = self.collect()
        self.assertEqual(len(snapshot.processes), 1)
        ccb = snapshot.processes[0].ccb[0]
        self.assertFalse(ccb.verified)
        self.assertIsNone(ccb.attached_clients)
        self.assertEqual(ccb.desired_state, "stopped")
        self.assertEqual([c.verified for c in ccb.controllers], [False, False])
        self.assertTrue(ccb.issues)

    def test_program_name_without_project_identity_is_not_controller_verification(self) -> None:
        self.metadata(lease={"ccbd_pid": 90, "keeper_pid": 91})
        self.fixture.rows += [row(90, 1, "ccbd", "ccbd --project /different/project"), row(91, 1, "python3", f"python3 unrelated.py keeper --project {self.root}")]
        ccb = self.collect().processes[0].ccb[0]
        self.assertEqual([c.verified for c in ccb.controllers], [False, False])

    def test_unrelated_pane_or_session_cannot_establish_ccb_ownership(self) -> None:
        self.metadata()
        with patch.object(monitor, "_socket_exists", return_value=True):
            for panes in (b"ccb-session|%7|999\n", b"other-session|%7|30\n"):
                with self.subTest(panes=panes):
                    self.fixture.panes = panes
                    ccb = self.collect().processes[0].ccb[0]
                    self.assertFalse(ccb.verified)
                    self.assertIsNone(ccb.pane)

    def test_live_ccb_pane_and_stopped_lifecycle_preserve_conflict(self) -> None:
        self.metadata(lifecycle={"desired_state": "stopped", "phase": "stopped"})
        with patch.object(monitor, "_socket_exists", return_value=True):
            ccb = self.collect().processes[0].ccb[0]
        self.assertTrue(ccb.verified)
        self.assertTrue(any(i.code == "conflict" for i in ccb.issues))

    def test_unknown_ccb_schema_is_not_verified(self) -> None:
        self.metadata(state={"record_type": "future_format", "schema_version": 500, "tmux_session_name": "ccb-session", "tmux_socket_path": str(self.root / "existing.sock")})
        with patch.object(monitor, "_socket_exists", return_value=True):
            ccb = self.collect().processes[0].ccb[0]
        self.assertFalse(ccb.verified)
        self.assertTrue(any(i.code == "incomplete" for i in ccb.issues))

    def test_wrongly_typed_ccb_schema_preserves_process_evidence(self) -> None:
        self.metadata()
        state_path = self.root / ".ccb" / "ccbd" / "state.json"
        for invalid in ({"schema_version": []}, {"record_type": {}}, {"schema_version": True}):
            with self.subTest(invalid=invalid):
                state_path.write_text(json.dumps(invalid))
                snapshot = self.collect()
                self.assertEqual(snapshot.processes[0].process.pid, 40)
                self.assertFalse(snapshot.processes[0].ccb[0].verified)
                self.assertTrue(any(issue.code == "incomplete" for issue in snapshot.processes[0].ccb[0].issues))

    def test_malformed_and_oversized_ccb_files_do_not_break_process_discovery(self) -> None:
        self.metadata()
        directory = self.root / ".ccb" / "ccbd"
        (directory / "state.json").write_text("{malformed")
        (directory / "lease.json").write_text("x" * 65537)
        snapshot = self.collect()
        self.assertEqual(len(snapshot.processes), 1)
        self.assertTrue(any(i.source == "ccb" and i.code == "malformed" for i in snapshot.issues))

    def test_metadata_symlinks_do_not_read_unrelated_content(self) -> None:
        directory = self.root / ".ccb" / "ccbd"
        directory.mkdir(parents=True)
        unrelated = self.root / "unrelated.json"
        unrelated.write_text(json.dumps({"tmux_session_name": "private-credential-content"}))
        (directory / "state.json").symlink_to(unrelated)
        snapshot = self.collect()
        self.assertEqual(snapshot.processes[0].ccb, ())
        self.assertNotIn("private-credential-content", repr(snapshot))
        self.assertTrue(any(i.source == "ccb" and i.code == "malformed" for i in snapshot.issues))

    def test_missing_tmux_reports_limitation_and_does_not_imply_detached(self) -> None:
        self.metadata()
        self.fixture.missing.add("tmux")
        with patch.object(monitor, "_socket_exists", return_value=True):
            snapshot = self.collect()
        ccb = snapshot.processes[0].ccb[0]
        self.assertFalse(ccb.verified)
        self.assertIsNone(ccb.attached_clients)
        self.assertTrue(any(i.source == "tmux" and i.code == "missing_tool" for i in snapshot.issues))


class TestNativeArgumentBoundaries(unittest.TestCase):
    def test_darwin_argc_boundary_never_inspects_environment_tail(self) -> None:
        arguments = [b"node", b"/opt/Claude's Tools/@anthropic-ai/claude-code/cli.js", b"", b"multiword value"]
        count = ctypes.c_int(len(arguments))
        prefix = bytes(count) + b"/usr/bin/node\0\0\0" + b"\0".join(arguments) + b"\0"
        boundary = len(prefix)
        payload = prefix + b"CREDENTIAL=private-environment-tail\xff"

        class GuardedBuffer(ctypes.Array):
            _type_ = ctypes.c_char
            _length_ = len(payload)

            def __getitem__(self, index):
                end = index.stop if isinstance(index, slice) else index + 1
                if end > boundary:
                    raise AssertionError("Environment tail was inspected")
                return super().__getitem__(index)

        buffer = GuardedBuffer()
        ctypes.memmove(buffer, payload, len(payload))
        self.assertEqual(monitor._decode_darwin_argv(buffer, len(payload)), [arg.decode() for arg in arguments])

    def test_darwin_truncated_or_invalid_argc_is_rejected(self) -> None:
        for payload in (b"\0", bytes(ctypes.c_int(-1)) + b"/bin/node\0node\0", bytes(ctypes.c_int(2)) + b"/bin/node\0node\0unterminated"):
            with self.subTest(payload=payload):
                buffer = ctypes.create_string_buffer(payload)
                with self.assertRaises(ValueError):
                    monitor._decode_darwin_argv(buffer, len(payload))

    def test_linux_cmdline_preserves_true_argument_boundaries(self) -> None:
        argv = ["launcher", "--password", "private multiword value", "", "literal'quote"]
        payload = b"\0".join(arg.encode() for arg in argv) + b"\0"
        with patch.object(monitor.sys, "platform", "linux"), patch("builtins.open", return_value=io.BytesIO(payload)):
            self.assertEqual(monitor._native_argv(40), argv)

    def test_linux_truncated_and_oversized_cmdline_are_rejected(self) -> None:
        for payload in (b"launcher\0unterminated", b"x" * (monitor._ARGV_LIMIT + 1)):
            with self.subTest(length=len(payload)):
                with patch.object(monitor.sys, "platform", "linux"), patch("builtins.open", return_value=io.BytesIO(payload)):
                    with self.assertRaises(ValueError):
                        monitor._native_argv(40)


if __name__ == "__main__":
    unittest.main()
