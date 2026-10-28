# Local Claude Runtime Monitoring

## Goal

Let users see which local Claude Code processes are actually running, which parent
program owns them, and which network sockets they currently hold, including Claude
left running inside a detached CCB project runtime.

## Background and Evidence

- The existing Textual app provides sessions, projects and dashboard navigation,
  but no runtime monitor (`src/ai_session_hub/ui.py:165-183`,
  `src/ai_session_hub/ui_layout.py:28-39,206-213`).
- The supplied incident establishes detached tmux ownership, automatic agent
  recovery and the difference between historical PID records and live processes
  (`ccb-claude-background-process-investigation-2026-10-20.md:40-69,125-168,185-208`).
- The user approved task creation/design, then explicitly approved the final
  implementation plan in a subsequent message.

## Requirements

### R1 — Actual Claude Processes

Add a dedicated runtime-monitoring view. Discover native `claude` / `claude.exe`
and identifiable Node-hosted Claude Code entrypoints, not arbitrary commands whose
arguments merely mention Claude. Show PID, executable/program, start time, elapsed
runtime and working directory. Runtime discovery must work without indexed session
history or configured sources. Show an explicit empty state after a successful
scan with no matches.

### R2 — Parent Program Attribution

Show direct PPID and the ordered ancestor chain, with each available ancestor's
PID, program and command line. Display missing, exited, inaccessible or reparented
ancestry as incomplete; never invent the original launcher. Retain process start
identity alongside PID so reuse cannot silently associate another process with
previous details. Distinguish OS parentage from separately observed supervision.

### R3 — PID-Scoped Network Connections

For each selected Claude process, display its own local address, remote address
when present, protocol and observed connection state. Support IPv4/IPv6 TCP and
UDP sockets where exposed by the operating system. Label listeners and unconnected
sockets accurately. Do not attribute an ancestor's or another process's sockets
to Claude. Distinguish no observed sockets from unavailable/partial collection.

### R4 — CCB Runtime Ownership

Where accessible, associate a live Claude process with its CCB project and tmux
session/pane using process and runtime evidence. Show project directory, tmux
session, attached-client count, lifecycle intent/phase and independently identified
ccbd/keeper references when available. An attached-client count of zero means
detached, not stopped. Persisted PID numbers and lifecycle intent alone do not
prove a process is alive or supervised; label stale, conflicting or unverified
information. Use the incident as a regression scenario, never hard-code its PIDs,
project path, installation path or session name.

### R5 — Responsive, Fresh and Bilingual UI

Add a navigation button and F4 shortcut consistent with existing views, with a
process list and inspectable parent/network/CCB details. Refresh automatically
while visible and permit manual refresh. Preserve selection by process identity;
clear exited-process details, skip overlapping scans and discard obsolete results.
Show collection time, progress/staleness and source-specific errors. Keep collection
off the UI thread and pause new collection when hidden, during native terminal
handoff or when closing. Support existing English/Chinese language switching and
wide/narrow terminal layouts without regressing other views or shortcuts.

### R6 — Read-Only and Compatibility Constraints

Remain local and read-only on the existing macOS/Linux product surface. Do not
elevate privileges, launch Claude/CCB, create or attach tmux sessions, signal
processes, decrypt TLS, capture request bodies or read credentials/process
environments. Restrict CCB reads to relevant runtime metadata and runtime discovery
to observed process directories/ancestors, not a filesystem-wide scan. Missing
tools, permission denial, disappearing processes, malformed records and command
timeouts must produce explicit limitations without breaking session browsing.
Render external text literally and redact recognizable credential-bearing command
arguments before they enter display snapshots; do not persist monitoring data.

## Acceptance Criteria

- AC1 (R1, R6): Native and Node-hosted Claude processes are recognized; unrelated
  commands mentioning Claude are excluded. A monitor with no configured history
  still discovers live processes, or displays a verified empty state.
- AC2 (R2): Selecting a process shows its immediate PPID and ordered ancestors.
  Missing ancestors and PID reuse cannot display another process's details as the
  original launch chain.
- AC3 (R3): A controlled loopback TCP connection is attributed to its actual PID;
  IPv6/UDP/listener cases retain correct endpoint/state semantics. Collection
  failure is not displayed as zero activity.
- AC4 (R4): A detached CCB/tmux scenario can show a running Claude process with
  zero attached clients and verified ownership. Stopped-runtime metadata with old
  PIDs cannot create live processes or claim verified supervision.
- AC5 (R5): Navigation, selection, language switching, manual/automatic refresh,
  hidden-view/handoff pause, process disappearance and stale-result rejection work
  without blocking the UI or changing session search/resume behavior.
- AC6 (R6): Missing commands, denied access, malformed metadata and timeouts leave
  the app usable and explain which evidence is missing. Monitoring never performs
  a lifecycle-changing operation or exposes credential-bearing command arguments.
- AC7 (R1-R6): Exercise the collector with real local process/socket observations
  and visually inspect the running TUI in wide and narrow layouts. Separately
  report simulated CCB/OS cases versus live-provider evidence and untested platforms.

## Out of Scope

- Process termination, automatic cleanup, `ccb kill` or modification of CCB policy.
- HTTP request paths, TLS interception, packet capture, model-call counts, request
  purpose classification, token/cost inference from sockets or guaranteed hostname
  attribution through proxies.
- CPU/memory profiling, bandwidth accounting, persistent process/network history,
  background daemon monitoring while the app is closed, remote hosts, Windows,
  or monitoring other AI tools.

## Planning Status

No blocking product decisions remain within the confirmed scope. Technical design
and execution/verification order are recorded in `design.md` and `implement.md`.
Implementation and verification are complete. All acceptance criteria have
collector/UI regression or actual native/PTY evidence recorded in `implement.md`;
live Linux and real CCB supervisor verification are explicitly unperformed.
Changes remain uncommitted; task archival is intentionally deferred to avoid
automatic commit behavior without a user request.
