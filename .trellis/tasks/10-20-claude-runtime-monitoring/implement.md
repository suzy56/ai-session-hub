# Claude Runtime Monitor — Implementation Plan

## Approval Gate

The user explicitly approved the final scope/design summary in a subsequent
message. The task is now in progress; implementation and verification are
authorized within the documented read-only scope.

## Ordered Execution

1. Load task artifacts and relevant backend/frontend guidelines. Read current
   affected files and use language-server references before changing any existing
   exported interface. Keep the unrelated `.serena/project.yml` edit untouched.
2. Implement `runtime_monitor.py` with one typed snapshot interface: OS census,
   Claude identification, PID/start identity checks, ancestry, working directory,
   PID-scoped IPv4/IPv6 TCP/UDP sockets and source-specific collection issues.
   Keep commands argv-only, bounded and locale-stable; no elevation or process
   control. Enforce sanitized display data and no persistent raw monitoring output.
3. Add CCB ownership enrichment from relevant project runtime metadata and existing
   tmux inspection. Keep OS parent chain, controller references, lifecycle intent
   and observed pane/session ownership distinct. Recognize detached-but-running
   and stale/stopped metadata without inferring process liveness from files.
4. Integrate the Runtime view and F4, selected-process details, English/Chinese
   labels, narrow layout, collection status/time, manual/automatic refresh and
   lifecycle guards. Use one in-flight scan and generation rejection, and ensure
   history filters/refresh/resume shortcuts do not act on a phantom session in
   Runtime view. Do not refactor unrelated dashboard/history behavior.
5. Add deterministic behavior tests only for plausible identity, attribution,
   endpoint, error and lifecycle regressions. Use existing unittest conventions;
   do not install/run real Claude or stop/start a real CCB project in tests.
6. Run one final affected-suite/full-suite verification, then exercise the real
   collector and actual TUI. Fix observed regressions before delivery. Report
   evidence separately for native observations and simulated provider cases.

## Verification Matrix

| Requirement | Proof |
| --- | --- |
| R1 | Native/Node entrypoint positives, unrelated-argument negatives, no-history discovery; real local collector scan |
| R2 | Ordered ancestry, missing ancestor, cycle/reparenting, PID/start mismatch and selection replacement |
| R3 | Correct PID endpoints/states for TCP listener/connected, IPv6 and UDP fixtures; real temporary loopback socket observation |
| R4 | Detached live pane with zero attached clients; absent socket, stopped lifecycle with stale PIDs, mismatched controller identity and conflicting metadata |
| R5 | Actual F4/button navigation, selection, manual refresh, disappearance, language switch, wide/narrow rendering, stale-result rejection and hidden/handoff pause |
| R6 | Missing executable, permission denial, malformed native/CCB data, deadline, partial collection and credential-bearing command display; no mutation commands |

Run tests using the repository's Python/source layout, for example:

```text
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_runtime_monitor.py'
PYTHONPATH=src python3 -m unittest discover -s tests
```

For smoke proof, use a throwaway isolated AppConfig/data directory and the actual
source-tree HubApp, not the possibly older Homebrew-installed binary. Launch in a
PTY, inspect wide and 80-column Runtime views, switch English/Chinese, select a
process and inspect parent/network/CCB details. Use owned temporary loopback
sockets/processes only where needed; never send traffic to Anthropic or start
real Claude for verification. For a controlled process recognition smoke, an owned
native helper may be named like a supported executable; label it as synthetic,
not a real provider invocation. If no Claude is currently running, prove the empty
state separately and do not fabricate a live provider observation.

## Risk and Completion Gates

- Native output parsing must not interpret permission failure as an empty sample.
- Current environment proves macOS command feasibility only. Record Linux runtime
  verification as unperformed unless a real Linux environment is available; keep
  portable ps/lsof support and Linux fixture coverage within scope.
- No currently running CCB-backed Claude was established during planning. Use the
  supplied incident and isolated metadata/tmux-output fixtures for the required
  detached/recovery semantics; do not restart a stopped user project to get proof.
- No TLS/request/billing/hostname guarantees, persistent history, process control,
  CPU/memory/traffic accounting or other-provider scope expansion.
- After smoke proof, update both READMEs with capability/tool/evidence limits,
  remove owned throwaway artifacts and stop only owned verification resources.
- Read `trellis-check` and `requesting-code-review` at verification time. Review
  affected callsites, docs and acceptance evidence before reporting completion.
- No automatic commit, push or modifications to the user's unrelated files.

## Verification Evidence (2026-10-20)

- Final `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`: **133
  tests passed**. The runtime-targeted suite passed **47 tests** before the final
  printable tmux separator correction; the final full suite includes all of them.
- Real macOS synthetic native `claude` helper: observed PID/direct PPID, ordered
  ancestors, cwd, TCP LISTEN/ESTABLISHED loopback sockets and bound UDP socket.
  Helpers are owned verification processes, not real provider invocations.
- Real native ancestor launched with a multiword credential argument: entire
  value absent from the collected snapshot; parent PID corroborated. Real Node
  helper under a spaced Claude Code entrypoint path was correctly identified.
- After stopping the owned native helper, actual collection returned zero Claude
  processes, `process_complete=True` and no issues: successful empty discovery is
  distinct from access/tool failure and no exited helper retained stale details.
- Actual PTY TUI smoke exited with status 0 after F4/button navigation, selected
  PID details, loopback evidence, manual refresh, English/Chinese settings change,
  wide to 80x24 resize, hidden-view pause and reentry. Visual SVG inspection
  confirmed stacked narrow details and visible PID/program/elapsed columns.
- Actual owned detached tmux session plus synthetic CCB schema-v2 metadata:
  collector matched Claude pane ancestry, verified session/pane ownership, showed
  attached_clients=0 with desired_state=running, and retained its TCP sockets.
  This is not a live CCB daemon/provider test. The owned tmux server was stopped.
- Live tmux smoke exposed C-locale tab replacement; printable field separators
  fixed the observed failure and the same ownership scenario passed afterward.
- Review found native flattened-argv redaction/identity flaws and malformed schema
  type handling; fixed with authoritative argv and typed schema checks. UI smoke
  found Textual `_closing` collision and stale resize width; fixed and covered by
  message-processing/geometry regressions. A claimed Runtime resume leak was
  retracted after checking the pre-existing current-view guard.
- README usage, shortcuts, tool/permission limits and read-only evidence semantics
  updated in both languages; reusable backend/frontend contracts captured in specs.
- No Linux runtime or real CCB supervisor/recovery invocation was exercised; those
  formats/attribution boundaries have deterministic regressions. No credentials,
  provider traffic, user's CCB configuration or user runtime lifecycle was changed.
- No commit/push or auto-committing archive/journal command was run.

## Local Installation Update (2026-10-20)

- User explicitly requested updating the installed local command. Built the
  current source with the existing PyInstaller spec, then backed up and atomically
  replaced `/opt/homebrew/Cellar/ai-session-hub/0.1.0/bin/ai-session-hub` without
  changing the `/opt/homebrew/bin/ai-session-hub` entrypoint.
- Previous binary retained at
  `~/Library/Caches/ai-session-hub/local-install-20261020-203339/ai-session-hub.previous`.
- Installed binary SHA256:
  `6c0078b2a5de19cdd3c15e98744148b71ecfcff1e9f55d4e7c83126b4eebe0b9`;
  it matches the newly built executable and differs from the backup.
- Ran the installed command in an actual PTY with isolated config/data: F4 opened
  Runtime and rendered successful empty discovery; Ctrl+R triggered collection;
  Ctrl+Q exited with status 0. `--help` and `--version` also ran successfully.
- This is a local replacement, not a published Homebrew release; the application
  still reports 0.1.0, and a later Homebrew upgrade may overwrite this installation.
- Removed the owned temporary build and smoke workspace; retained only the
  intentional rollback binary. Existing running user sessions were not stopped.

