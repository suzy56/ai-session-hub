"""Read-only, bounded local process observations; no history or lifecycle control."""
from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class CollectionIssue:
    source: str
    code: str
    message: str
    pid: int | None = None


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    started: str
    elapsed: str
    executable: str
    command: str
    cwd: str | None = None

    @property
    def identity(self) -> tuple[int, str]:
        return self.pid, self.started


@dataclass(frozen=True)
class ConnectionInfo:
    pid: int
    protocol: str
    local_address: str
    remote_address: str | None = None
    state: str | None = None


@dataclass(frozen=True)
class ControllerInfo:
    role: str
    pid: int
    verified: bool


@dataclass(frozen=True)
class CCBRuntime:
    project: str
    session: str | None = None
    pane: str | None = None
    attached_clients: int | None = None
    desired_state: str | None = None
    phase: str | None = None
    shutdown_intent: str | None = None
    mount_state: str | None = None
    verified: bool = False
    controllers: tuple[ControllerInfo, ...] = ()
    issues: tuple[CollectionIssue, ...] = ()


@dataclass(frozen=True)
class ClaudeProcess:
    process: ProcessInfo
    ancestors: tuple[ProcessInfo, ...] = ()
    connections: tuple[ConnectionInfo, ...] = ()
    ccb: tuple[CCBRuntime, ...] = ()
    issues: tuple[CollectionIssue, ...] = ()
    network_complete: bool = True
    ancestry_complete: bool = True


@dataclass(frozen=True)
class RuntimeSnapshot:
    collected_at: float
    processes: tuple[ClaudeProcess, ...] = ()
    issues: tuple[CollectionIssue, ...] = ()
    process_complete: bool = True


_SECRET = re.compile(r"(?i)(?:api[-_]?key|token|secret|password|passwd|authorization|credential)")
_TOKEN = re.compile(r"(?i)(?:sk-[a-z0-9_-]{8,}|bearer\s+\S+|https?://[^\s/@]+:[^\s/@]+@)")
_PS_ROW = re.compile(r"^\s*(\d+)\s+(\d+)\s+((?:\S+\s+){4}\S+)\s+(\S+)\s+(.+?)\s*$")


def _literal(value: str) -> str:
    return _TOKEN.sub("[REDACTED]", "".join(c if c.isprintable() else " " for c in value))


_ARGV_LIMIT = 1024 * 1024


def _decode_darwin_argv(buffer: ctypes.Array, size: int) -> list[str]:
    """Decode argc entries only; the environment tail is never inspected/copied."""
    header = ctypes.sizeof(ctypes.c_int)
    if size < header or size > ctypes.sizeof(buffer):
        raise ValueError("Invalid process argument buffer length")
    argc = ctypes.c_int.from_buffer(buffer).value
    if argc < 0 or argc > size - header:
        raise ValueError("Invalid process argument count")
    if argc == 0:
        return []
    offset = header
    # The executable path precedes padding and argv[0]. Do not decode this path.
    while offset < size and buffer[offset] != b"\0":
        offset += 1
    if offset == size:
        raise ValueError("Missing executable path terminator")
    while offset < size and buffer[offset] == b"\0":
        offset += 1
    argv: list[str] = []
    for _ in range(argc):
        start = offset
        while offset < size and buffer[offset] != b"\0":
            offset += 1
        if offset == size:
            raise ValueError("Truncated process argument")
        argv.append(buffer[start:offset].decode("utf-8", "replace"))
        offset += 1
    return argv


def _native_argv(pid: int) -> list[str]:
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        query = libc.sysctl
        query.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                          ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        query.restype = ctypes.c_int
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, PID
        buffer = ctypes.create_string_buffer(_ARGV_LIMIT)
        size = ctypes.c_size_t(_ARGV_LIMIT)
        try:
            if query(mib, 3, buffer, ctypes.byref(size), None, 0):
                raise OSError(ctypes.get_errno(), "Process arguments unavailable")
            return _decode_darwin_argv(buffer, size.value)
        finally:
            # sysctl's transport includes an environment tail. Never retain it,
            # convert it to bytes, decode it, or inspect anything after argc argv.
            ctypes.memset(buffer, 0, ctypes.sizeof(buffer))
    if sys.platform.startswith("linux"):
        with open(f"/proc/{pid}/cmdline", "rb") as stream:
            data = stream.read(_ARGV_LIMIT + 1)
        if len(data) > _ARGV_LIMIT or (data and not data.endswith(b"\0")):
            raise ValueError("Oversized or truncated process arguments")
        return [arg.decode("utf-8", "replace") for arg in data[:-1].split(b"\0")] if data else []
    raise OSError(errno.ENOSYS, "Native process arguments unsupported")


def _claude(executable: str, argv: list[str]) -> bool:
    name = Path(executable).name.lower()
    if name in {"claude", "claude.exe"}:
        return True
    if name not in {"node", "nodejs", "node.exe"} or len(argv) < 2:
        return False
    # Only the actual entrypoint, never a later prompt/argument, establishes identity.
    index = 1
    while index < len(argv) and argv[index] in {"--no-warnings", "--enable-source-maps"}:
        index += 1
    if index >= len(argv) or argv[index].startswith("-"):
        return False
    return bool(re.search(r"(?:^|/)@anthropic-ai/claude-code/(?:cli\.js|bin/claude(?:\.js)?)$", argv[index]))


def _command(argv: list[str], claude: bool = False) -> str:
    if not argv:
        return ""
    if claude:
        # Prompts can be positional, --print, --system-prompt, or arbitrary flags.
        # Keep only the proven program/entrypoint, not any Claude arguments.
        kept = argv[:1]
        if Path(argv[0]).name.lower() in {"node", "nodejs", "node.exe"}:
            kept = argv[:2]
        return _literal(shlex.join(kept)) + " [arguments hidden]"
    program = Path(argv[0]).name
    inline_flags = {"-c", "-lc", "-ic", "-ec", "-e", "--eval", "-p", "--print"}
    if (program in {"sh", "bash", "zsh", "fish", "node", "nodejs"} or program.startswith("python")) and any(arg in inline_flags for arg in argv[1:]):
        index = next(i for i, arg in enumerate(argv) if arg in inline_flags)
        return _literal(shlex.join(argv[:index + 1])) + " [command body hidden]"
    kept: list[str] = []
    hide_next = False
    for arg in argv:
        if hide_next:
            kept.append("[REDACTED]")
            hide_next = arg.lower() in {"bearer", "basic"}
        elif _SECRET.search(arg.split("=", 1)[0]):
            key = arg.split("=", 1)[0]
            if "=" in arg and re.fullmatch(r"[-\w]+", key):
                kept.append(key + "=[REDACTED]")
            elif re.fullmatch(r"[-\w]+:?", arg):
                kept.append(arg)
                hide_next = True
            else:
                kept.append("[REDACTED]")
        else:
            kept.append(_literal(arg))
    return _literal(shlex.join(kept))


class _Scan:
    def __init__(self, cancelled: Callable[[], bool], timeout: float):
        self.cancelled = cancelled
        self.deadline = time.monotonic() + max(0.0, min(timeout, 10.0))
        self.issues: list[CollectionIssue] = []
        self.argument_attempts: set[int] = set()

    def available(self, source: str) -> bool:
        code = "cancelled" if self.cancelled() else "timeout" if time.monotonic() >= self.deadline else None
        if code:
            issue = CollectionIssue(source, code, "Collection cancelled." if code == "cancelled" else "Collection deadline reached.")
            if issue not in self.issues:
                self.issues.append(issue)
            return False
        return True

    def run(self, tool: str, args: list[str], source: str, no_match: bool = False) -> bytes | None:
        if not self.available(source):
            return None
        executable = shutil.which(tool)
        if not executable:
            self.issues.append(CollectionIssue(source, "missing_tool", f"{tool} is not installed or not on PATH."))
            return None
        try:
            result = subprocess.run(
                [executable, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, shell=False,
                env={**os.environ, "LC_ALL": "C", "LANG": "C"},
                timeout=min(2.0, max(0.001, self.deadline - time.monotonic())),
            )
        except subprocess.TimeoutExpired:
            self.issues.append(CollectionIssue(source, "timeout", f"{tool} exceeded its collection deadline."))
            return None
        except OSError as exc:
            self.issues.append(CollectionIssue(source, "denied" if isinstance(exc, PermissionError) else "unavailable", f"{tool} could not be executed."))
            return None
        # Native stderr can contain raw commands or paths: never retain it.
        if result.stderr:
            denied = b"permission" in result.stderr.lower() or b"denied" in result.stderr.lower()
            self.issues.append(CollectionIssue(source, "denied" if denied else "incomplete", f"{tool} reported diagnostics; coverage may be incomplete."))
        if result.returncode and not (no_match and result.returncode == 1 and not result.stderr):
            self.issues.append(CollectionIssue(source, "unavailable", f"{tool} returned status {result.returncode}."))
            return result.stdout or None
        return result.stdout

    def arguments(self, pid: int, commands: dict[int, list[str]]) -> None:
        if pid in self.argument_attempts:
            return
        self.argument_attempts.add(pid)
        if not self.available("argv"):
            return
        try:
            argv = _native_argv(pid)
        except OSError as exc:
            code = "denied" if exc.errno in {errno.EACCES, errno.EPERM} else "exited" if exc.errno in {errno.ESRCH, errno.ENOENT} else "unavailable"
            self.issues.append(CollectionIssue("argv", code, "Authoritative process arguments are unavailable; flattened ps text is not used.", pid))
            return
        except ValueError:
            self.issues.append(CollectionIssue("argv", "malformed", "Authoritative process arguments are malformed or truncated.", pid))
            return
        if not argv:
            self.issues.append(CollectionIssue("argv", "incomplete", "The process exposed no authoritative arguments.", pid))
            return
        if self.available("argv"):
            commands[pid] = argv

    def census(self, pids: list[int] | None = None) -> dict[int, ProcessInfo] | None:
        selector = ["-p", ",".join(map(str, pids))] if pids else ["-A"]
        data = self.run("ps", ["-ww", *selector, "-o", "pid=,ppid=,lstart=,etime=,comm="], "ps")
        if data is None:
            return None
        rows: dict[int, ProcessInfo] = {}
        for line in data.decode("utf-8", "replace").splitlines():
            match = _PS_ROW.match(line)
            if not match:
                self.issues.append(CollectionIssue("ps", "malformed", "A process census row could not be decoded."))
                continue
            pid, ppid, started, elapsed, executable = match.groups()
            rows[int(pid)] = ProcessInfo(int(pid), int(ppid), " ".join(started.split()), elapsed, _literal(executable), "")
        return rows


def _ancestors(process: ProcessInfo, rows: dict[int, ProcessInfo]) -> tuple[list[ProcessInfo], list[CollectionIssue]]:
    chain: list[ProcessInfo] = []
    issues: list[CollectionIssue] = []
    seen = {process.pid}
    pid = process.ppid
    while pid:
        if pid in seen:
            issues.append(CollectionIssue("ps", "incomplete", "Ancestry contains a cycle.", process.pid))
            break
        seen.add(pid)
        parent = rows.get(pid)
        if parent is None:
            issues.append(CollectionIssue("ps", "incomplete", "An ancestor exited or is inaccessible.", pid))
            break
        chain.append(parent)
        if pid == 1:
            break
        if len(chain) >= 128:
            issues.append(CollectionIssue("ps", "incomplete", "Ancestry depth limit reached.", process.pid))
            break
        pid = parent.ppid
    return chain, issues


def _lsof(data: bytes, network: bool, scan: _Scan) -> tuple[dict[int, list[ConnectionInfo]], dict[int, str]]:
    sockets: dict[int, list[ConnectionInfo]] = {}
    cwd: dict[int, str] = {}
    pid: int | None = None
    fields: dict[str, str] = {}

    def flush() -> None:
        if pid is None or not fields:
            return
        if not network:
            if fields.get("f") == "cwd" and "n" in fields:
                cwd[pid] = _literal(fields["n"])
            return
        protocol, endpoint = fields.get("P"), fields.get("n")
        if protocol not in {"TCP", "UDP"} or not endpoint:
            scan.issues.append(CollectionIssue("network", "malformed", "An internet socket record lacks protocol or endpoint.", pid))
            return
        local, separator, remote = endpoint.partition("->")
        if not local or (separator and not remote):
            scan.issues.append(CollectionIssue("network", "malformed", "A socket endpoint is incomplete.", pid))
            return
        sockets.setdefault(pid, []).append(ConnectionInfo(pid, protocol, _literal(local), _literal(remote) if separator else None, _literal(fields["T"]) if "T" in fields else None))

    # -F0 terminates each field with NUL; newline separates process/file sets.
    # Split only at NUL: paths containing newlines remain a single n field.
    for token in data.split(b"\0"):
        token = token.lstrip(b"\n")
        if not token:
            continue
        key, value = chr(token[0]), token[1:].decode("utf-8", "replace")
        if key == "p":
            flush()
            fields = {}
            try:
                pid = int(value)
            except ValueError:
                pid = None
                scan.issues.append(CollectionIssue("network" if network else "cwd", "malformed", "Invalid lsof process identity."))
        elif key == "f":
            flush()
            fields = {"f": value}
        elif key == "T":
            if value.startswith("ST="):
                fields["T"] = value[3:]
        elif key in {"P", "n"}:
            fields[key] = value
        elif key not in {"c", "t"}:
            scan.issues.append(CollectionIssue("network" if network else "cwd", "malformed", "An lsof field could not be decoded.", pid))
    flush()
    return sockets, cwd


def _read_json(path: Path, scan: _Scan) -> dict:
    if not scan.available("ccb"):
        return {}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("not regular")
            content = stream.read(65537)
        if len(content) > 65536:
            raise ValueError("oversized")
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError("not object")
        return value
    except FileNotFoundError:
        return {}
    except PermissionError:
        scan.issues.append(CollectionIssue("ccb", "denied", "CCB runtime metadata is inaccessible."))
    except (OSError, ValueError, UnicodeError):
        scan.issues.append(CollectionIssue("ccb", "malformed", "CCB runtime metadata is unreadable, oversized or malformed."))
    return {}


def _text(data: dict, key: str) -> str | None:
    value = data.get(key)
    return _literal(value) if isinstance(value, str) else None


def _socket_exists(path: str) -> bool:
    try:
        return Path(path).is_absolute() and stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _ccb(process: ProcessInfo, chain: list[ProcessInfo], rows: dict[int, ProcessInfo], commands: dict[int, list[str]], scan: _Scan, cache: dict[Path, tuple[dict, dict, dict]], tmux_cache: dict[str, tuple[list[tuple[str, str, int]], dict[str, int]]]) -> tuple[CCBRuntime, ...]:
    candidates: set[Path] = set()
    for item in [process, *chain]:
        if item.cwd:
            path = Path(item.cwd)
            candidates.update([path, *list(path.parents)[:32]])
        # Relevant tmux/control-plane argv may explicitly identify its project.
        args = commands.get(item.pid, [])
        if Path(item.executable).name in {"tmux", "ccbd", "keeper"} or (args and Path(args[0]).name == "tmux"):
            for index, arg in enumerate(args[:-1]):
                if arg in {"-c", "--project", "--project-dir", "--cwd"} and Path(args[index + 1]).is_absolute():
                    candidates.add(Path(args[index + 1]))
                if arg == "-S":
                    socket = Path(args[index + 1])
                    for parent in list(socket.parents)[:8]:
                        if parent.name == ".ccb":
                            candidates.add(parent.parent)
    result: list[CCBRuntime] = []
    for project in sorted(candidates, key=str)[:64]:
        if not scan.available("ccb"):
            break
        directory = project / ".ccb" / "ccbd"
        if project not in cache:
            before = len(scan.issues)
            state, lease, lifecycle = (_read_json(directory / name, scan) for name in ("state.json", "lease.json", "lifecycle.json"))
            cache[project] = state, lease, lifecycle
            if not any((state, lease, lifecycle)) and len(scan.issues) == before:
                continue
        state, lease, lifecycle = cache[project]
        if not any((state, lease, lifecycle)):
            continue
        issues: list[CollectionIssue] = []
        record_type = state.get("record_type", state.get("type"))
        schema_version = state.get("schema_version")
        if state and (record_type not in (None, "ccbd_project_namespace_state") or
                      (schema_version is not None and (type(schema_version) is not int or schema_version not in (1, 2)))):
            issues.append(CollectionIssue("ccb", "incomplete", "Unknown CCB state schema; ownership is unverified."))
        session = _text(state, "tmux_session_name")
        socket = _text(state, "tmux_socket_path")
        pane: str | None = None
        attached: int | None = None
        verified = False
        if socket and _socket_exists(socket):
            if socket not in tmux_cache:
                # tmux replaces control characters (including tabs) under LC_ALL=C.
                # Split printable numeric suffixes from the right; session names may contain '|'.
                panes_data = scan.run("tmux", ["-S", socket, "list-panes", "-a", "-F", "#{session_name}|#{pane_id}|#{pane_pid}"], "tmux")
                sessions_data = scan.run("tmux", ["-S", socket, "list-sessions", "-F", "#{session_name}|#{session_attached}"], "tmux")
                panes: list[tuple[str, str, int]] = []
                sessions: dict[str, int] = {}
                for line in (panes_data or b"").decode("utf-8", "replace").splitlines():
                    parts = line.rsplit("|", 2)
                    if len(parts) == 3 and parts[2].isdigit():
                        panes.append((_literal(parts[0]), _literal(parts[1]), int(parts[2])))
                    else:
                        scan.issues.append(CollectionIssue("tmux", "malformed", "Malformed tmux pane observation."))
                for line in (sessions_data or b"").decode("utf-8", "replace").splitlines():
                    parts = line.rsplit("|", 1)
                    if len(parts) == 2 and parts[1].isdigit():
                        sessions[_literal(parts[0])] = int(parts[1])
                    else:
                        scan.issues.append(CollectionIssue("tmux", "malformed", "Malformed tmux session observation."))
                tmux_cache[socket] = panes, sessions
            panes, sessions = tmux_cache[socket]
            attached = sessions.get(session) if session else None
            for name, pane_id, pane_pid in panes:
                if name == session and pane_pid in {item.pid for item in [process, *chain]}:
                    pane = pane_id
                    verified = not issues
                    break
        else:
            issues.append(CollectionIssue("ccb", "incomplete", "No accessible existing CCB tmux socket; ownership is unverified."))
        if not verified:
            issues.append(CollectionIssue("ccb", "unverified", "Metadata alone does not establish live CCB ownership."))
        controllers: list[ControllerInfo] = []
        for role, key in (("ccbd", "ccbd_pid"), ("keeper", "keeper_pid")):
            pid = lease.get(key)
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                continue
            row = rows.get(pid)
            if row is not None and pid not in scan.argument_attempts and pid not in commands:
                scan.arguments(pid, commands)
                checked = scan.census([pid]) if pid in commands else None
                if checked is None or pid not in checked or checked[pid].identity != row.identity or checked[pid].ppid != row.ppid:
                    commands.pop(pid, None)
                    issues.append(CollectionIssue("argv", "identity_changed", "Controller identity changed while reading its arguments.", pid))
            args = commands.get(pid, [])
            # Exact program/module identity and an explicit project argument are both required.
            entrypoint = Path(args[0]).name if args else ""
            if entrypoint in {"python", "python3", "python3.13", "node", "nodejs"} and len(args) > 1:
                entrypoint = args[2] if args[1] == "-m" and len(args) > 2 else Path(args[1]).name
            program_matches = entrypoint in {role, f"{role}.py", f"{role}.js"}
            project_matches = any(
                (arg in {"--project", "--project-dir", "--cwd"} and index + 1 < len(args) and args[index + 1] == str(project))
                or arg in {f"--project={project}", f"--project-dir={project}", f"--cwd={project}"}
                for index, arg in enumerate(args)
            )
            controller_verified = row is not None and program_matches and project_matches
            controllers.append(ControllerInfo(role, pid, controller_verified))
            if not controller_verified:
                issues.append(CollectionIssue("ccb", "unverified", f"Recorded {role} PID is stale or lacks matching program/project evidence.", pid))
        desired = _text(lifecycle, "desired_state")
        phase = _text(lifecycle, "phase")
        if verified and (desired in {"stopped", "stop"} or phase in {"stopped", "unmounted"}):
            issues.append(CollectionIssue("ccb", "conflict", "Live pane ownership conflicts with stopped lifecycle metadata."))
        result.append(CCBRuntime(_literal(str(project)), session, pane, attached, desired, phase, _text(lifecycle, "shutdown_intent"), _text(lease, "mount_state"), verified, tuple(controllers), tuple(issues)))
    return tuple(result)


def collect_runtime(cancelled: Callable[[], bool] = lambda: False, timeout: float = 10.0) -> RuntimeSnapshot:
    """Collect one local observation, bounded to ten seconds and two seconds/call."""
    scan = _Scan(cancelled, timeout)
    rows = scan.census()
    if rows is None:
        return RuntimeSnapshot(time.time(), issues=tuple(scan.issues), process_complete=False)
    commands: dict[int, list[str]] = {}
    for pid, row in rows.items():
        name = Path(row.executable).name.lower()
        if name in {"node", "nodejs", "node.exe"}:
            scan.arguments(pid, commands)
        elif name in {"claude", "claude.exe"}:
            # Native identity needs no access to potentially sensitive prompts.
            commands[pid] = [row.executable]
    claude_pids = [pid for pid, row in rows.items() if _claude(row.executable, commands.get(pid, []))]
    if not claude_pids:
        return RuntimeSnapshot(time.time(), issues=tuple(scan.issues), process_complete=not any(i.source in {"ps", "argv"} for i in scan.issues))
    chains = {pid: _ancestors(rows[pid], rows) for pid in claude_pids}
    relevant = sorted({pid for pid in claude_pids} | {item.pid for chain, _ in chains.values() for item in chain})
    for pid in relevant:
        if pid not in commands:
            scan.arguments(pid, commands)
    rows = {
        pid: replace(row, command=_command(commands[pid], pid in claude_pids) if pid in commands
                     else _literal(row.executable) + " [arguments unavailable]")
        for pid, row in rows.items()
    }
    cwd: dict[int, str] = {}
    connections: dict[int, list[ConnectionInfo]] = {}
    for offset in range(0, len(relevant), 128):
        batch = relevant[offset:offset + 128]
        data = scan.run("lsof", ["-nP", "-a", "-p", ",".join(map(str, batch)), "-d", "cwd", "-F0pfn"], "cwd", no_match=True)
        if data is not None:
            _, found = _lsof(data, False, scan)
            cwd.update({pid: value for pid, value in found.items() if pid in batch})
    for offset in range(0, len(claude_pids), 128):
        batch = claude_pids[offset:offset + 128]
        data = scan.run("lsof", ["-nP", "-a", "-p", ",".join(map(str, batch)), "-i", "-F0pfPnT"], "network", no_match=True)
        if data is not None:
            found, _ = _lsof(data, True, scan)
            connections.update({pid: value for pid, value in found.items() if pid in batch})
            if data.strip(b"\0\n"):
                for missing_pid in set(batch) - found.keys():
                    scan.issues.append(CollectionIssue("network", "incomplete", "Batched socket output did not cover this PID; no sockets were observed, but coverage is unverified.", missing_pid))
    # Validate all details, including controllers, against a fresh identity census.
    refreshed = scan.census()
    valid = {pid for pid, row in rows.items() if refreshed is not None and pid in refreshed and row.identity == refreshed[pid].identity and row.ppid == refreshed[pid].ppid}
    rows = {pid: replace(row, cwd=cwd.get(pid)) for pid, row in rows.items() if pid in valid}
    result: list[ClaudeProcess] = []
    cache: dict[Path, tuple[dict, dict, dict]] = {}
    tmux_cache: dict[str, tuple[list[tuple[str, str, int]], dict[str, int]]] = {}
    for pid in claude_pids:
        if pid not in valid:
            scan.issues.append(CollectionIssue("ps", "identity_changed" if refreshed and pid in refreshed else "exited", "Process identity or parentage changed; PID-specific details discarded.", pid))
            continue
        chain, ancestry_issues = _ancestors(rows[pid], rows)
        if any(ancestor.pid not in commands or not commands[ancestor.pid] for ancestor in chain):
            ancestry_issues.append(CollectionIssue("argv", "incomplete", "An ancestor command line is unavailable.", pid))
        process_issues = list(ancestry_issues)
        if pid not in cwd:
            process_issues.append(CollectionIssue("cwd", "incomplete", "Working directory was not observable.", pid))
        ccb = _ccb(rows[pid], chain, rows, commands, scan, cache, tmux_cache)
        result.append(ClaudeProcess(rows[pid], tuple(chain), tuple(connections.get(pid, [])), ccb, tuple(process_issues), not any(i.source == "network" and i.pid in {None, pid} for i in scan.issues), not ancestry_issues))
    return RuntimeSnapshot(time.time(), tuple(result), tuple(scan.issues), not any(i.source in {"ps", "argv"} for i in scan.issues))
