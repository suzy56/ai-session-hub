# Claude Runtime Monitor — Design

## Change Boundary

The behavior gap is runtime visibility, not session ingestion: existing history
and token accounting cannot establish process liveness, OS parentage or sockets.
Add one collector module and one monitoring view; keep the indexed-history and
native-resume contracts unchanged. No database migration, monitoring daemon,
plugin framework, CCB implementation change or lifecycle control.

## Evidence and Feasibility

- `src/ai_session_hub/ui.py:117-158,455-474,635-650` already separates threaded
  collection and UI delivery with typed messages and generation checks.
- `src/ai_session_hub/ui.py:1032-1088` owns native handoff. Monitoring must respect
  `_handoff` and `_closing` rather than keeping a separate lifecycle flag.
- `src/ai_session_hub/resume.py:47-51,82-87` uses executable resolution and argv
  execution without a shell; reuse that execution convention, not its process
  control/cleanup behavior.
- `pyproject.toml:10-14` has only Textual and zstandard runtime dependencies.
  Use standard-library subprocesses and native `ps`/`lsof`, avoiding a new native
  Python dependency and standalone-binary packaging changes.
- A read-only local probe on 2026-10-20 successfully collected this Python
  process's PID, PPID, start time, elapsed time and executable using
  `ps -ww -p <pid> -o pid=,ppid=,lstart=,etime=,comm=` with `LC_ALL=C`.
  `lsof -a -p <pid> -d cwd -Fn` returned its working directory.
- A temporary loopback listener/client in the same probe yielded LISTEN and
  ESTABLISHED TCP records, numeric endpoints and owning PID through
  `lsof -nP -a -p <pid> -i -FpcfPnT`; all temporary sockets were closed. This is
  collector-command feasibility evidence, not a product smoke run or a claim of
  current Claude network activity. Prefer NUL-delimited lsof fields in production.
- Current inspected CCB metadata under the incident project's `.ccb/ccbd` has
  `state.json` record type `ccbd_project_namespace_state`, schema version 2,
  `tmux_session_name`, `tmux_socket_path` and namespace fields. `lease.json` has
  `ccbd_pid`, `keeper_pid` and `mount_state`; `lifecycle.json` has `desired_state`,
  `phase`, `owner_pid` and `shutdown_intent`. Only selected non-secret fields were
  read. Observed lifecycle is stopped/unmounted but lease retains old PID numbers:
  a direct stale-metadata acceptance example, not a running CCB fixture.
- The supplied incident documents the live detached/recovery scenario. No Linux
  runtime, currently running CCB-backed Claude, UDP or IPv6 was exercised during
  planning. Cover data semantics deterministically and report live-platform limits.

## Module Interface and Data Flow

`runtime_monitor.py` owns discovery, native output decoding, typed immutable
snapshots and CCB attribution behind one collection entrypoint. The caller supplies
cancellation/deadline context; callers never parse `ps`, `lsof` or CCB JSON.

OS process census → Claude identification → process identity/ancestor validation
→ PID-scoped socket/cwd collection → relevant CCB metadata/tmux observations
→ immutable `RuntimeSnapshot` → Textual worker message → monitoring view.

Use typed records for process identity (PID + start time), process details,
ancestor entries, sockets, CCB ownership and collection issues. A snapshot contains
observation time and source-specific completeness, not a single success boolean.
Each connection retains its PID. Missing endpoints/states are optional values,
not empty strings meaning success. Issues distinguish unsupported/missing-tool,
denied, timeout, exited, malformed and incomplete evidence.

Keep one census per refresh and batch `lsof` requests for relevant PIDs rather
than launching a command for every ancestor. Enrich only Claude rows/needed
ancestors and deduplicate CCB project/socket reads. Parse external output once at
this seam. Do not add a database/store dependency or read session history to find
processes.

## Process Identity and Parentage

Use wide, locale-stable `ps` output only for PID, PPID, start/elapsed time and
executable. Argument boundaries come from bounded macOS `KERN_PROCARGS2` argc
decoding or Linux `/proc/<pid>/cmdline`, never flattened `ps args` text. macOS
transport buffers are cleared without inspecting the environment tail. Native
Claude prompt arguments are not queried. Native `lsof` supplies working directories.
Identify native executable basenames and recognized Claude Code Node entrypoints,
including paths containing spaces; ambiguous/unavailable argv is partial evidence.
Retain only sanitized ancestor commands, masking whole credential argument values.

Construct ancestry from the census with cycle detection. Stop at PID 0/1 or a
missing node, explicitly describing truncation where appropriate. Recheck observed
process identity before accepting PID-specific enrichment; if start identity
changes or the process exits, discard its details. A reparented process can only
show the observed current chain, never a reconstructed original launcher.
Selection keys include PID and start time; a replacement Claude receives a new
identity even if the OS reuses its PID.

## Socket Semantics

Use numeric, machine-readable `lsof` internet socket records. Preserve protocol,
IPv4/IPv6 endpoint structure, listener versus connected sockets and TCP states;
UDP does not acquire an invented ESTABLISHED state. No DNS lookup, proxy-log
integration or inference that an IP endpoint is api.anthropic.com.

Treat lsof's no-match result separately from invocation/permission failures;
stderr, malformed output and incomplete PID coverage must remain visible.
No socket records means no sockets observed in that successful sample, not no
network requests between samples. Permission-limited snapshots cannot claim full
coverage. If Linux lacks lsof, explain how to enable socket collection while
retaining process/ancestor visibility; do not elevate privileges automatically.

## CCB Attribution

Discover candidate project roots only from observed cwd ancestors and relevant
process/tmux ownership evidence. Read a bounded set of `.ccb/ccbd` state, lease
and lifecycle fields. Parse in one place and tolerate absent/unknown schemas
without inventing meanings. Do not traverse the home directory or source trees.

For an existing referenced tmux socket, use inspection-only list/display commands
with explicit `-S` targeting; do not invoke CCB startup/attach helpers. Match pane
PID or its observed descendants to Claude and corroborate the project socket/
session. A tmux server can be an ancestor while ccbd is a separate supervisor;
show these relationships separately. Read attached-client count from actual tmux
session observations. Zero is detached; unavailable is not zero.

Show lifecycle intent and lease references with their provenance even when
current process or tmux confirmation is absent. PID existence alone cannot verify
that an old ccbd/keeper reference still belongs to this runtime. Verify program/
project identity before labeling a live controller; otherwise keep it unverified.
State conflicts remain visible rather than choosing metadata over OS observations.
Do not read arbitrary control-plane endpoints, logs, account files or restore
secrets, and never make a network call to a recorded endpoint.

Use printable `|` tmux format separators and split numeric suffixes from the
right: actual tmux under `LC_ALL=C` replaces literal tabs with underscores.

## UI Integration

Add Runtime / 运行监控 navigation and F4. Keep process list selection stable across
refreshes and show the selected process's ancestry, sockets and CCB evidence in
read-only details. At narrow widths, provide usable stacked/inspectable details
instead of compressing all columns. Translate labels/statuses using existing i18n;
PID values, commands, paths and connection endpoints remain literal.

Use a background worker and generation-tagged message, following existing app
patterns. Start on entry; refresh about every 3 seconds only while visible.
Manual refresh joins/skips an in-flight scan instead of creating overlapping
commands. A refresh has a bounded total deadline (10 seconds maximum), each native
command a smaller timeout (at most 2 seconds). On timeout show partial evidence
and collection/staleness status; do not block or queue refreshes indefinitely.

Leaving the view, native handoff and close invalidate results and prevent new
collection; in-flight calls finish under their deadlines and cannot update stale
selection/view state. Resume refresh on return. Hidden-view refresh is not a
persistent background-monitoring promise. Global history filters do not constrain
runtime results; scope/help/refresh hints must describe the active runtime view.

HubApp shutdown intent uses `_workspace_closing`, not Textual's internal `_closing`.
Live resize reads `Resize.size.width` and defers size-dependent rendering; reading
`self.size` inside the handler can observe the previous terminal width.

## Expected Files

- New `src/ai_session_hub/runtime_monitor.py`: collector, typed snapshots and
  native/CCB decoding; keep OS evidence handling out of UI code.
- New `src/ai_session_hub/ui_runtime.py`: monitoring widgets and rendering scoped
  to this view; owns no subprocesses or history data.
- `ui.py`, `ui_layout.py`, `ui.tcss`, `i18n.py`: navigation, worker lifecycle,
  language, help, responsive layout and active-view action integration.
- New `tests/test_runtime_monitor.py`: consumer-visible identity, attribution,
  connection semantics and failure cases using isolated native-output fixtures.
- New `tests/test_runtime_ui.py` and `tests/test_runtime_resize.py`: regression
  coverage for hidden/reentered scans, handoff pause, PID reuse, resize and quit.
  Existing UI tests remain unchanged; actual PTY smoke is independently required.
- `README.md`, `README.zh-CN.md`: monitoring usage, required native tools, explicit
  evidence/privilege limits and read-only scope, after implementation proof.

## Compatibility and Risk Controls

No existing exports need to change; new types are additive private-to-package
contracts. No persisted schema or preferences change. Existing history sources,
search, pricing, resume and language preferences must retain their behavior.

Native command formats and CCB schemas vary: isolate decoding, preserve partial
status and include realistic malformed/missing/unknown-schema cases. Commands
may contain secrets: sanitize before retaining display snapshots and do not log
raw command output. Observation races cannot be eliminated with command snapshots;
identity rechecks and explicit timestamps limit the claim to observed evidence.

Rollback is removal of the new view/module and navigation integration; it requires
no data migration or changes to live Claude/CCB runtimes. Do not alter the user's
pre-existing `.serena/project.yml` change. Do not commit or push without request.
