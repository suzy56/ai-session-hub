# Error Handling

> How errors are handled in this project.

---

## Overview

<!--
Document your project's error handling conventions here.

Questions to answer:
- What error types do you define?
- How are errors propagated?
- How are errors logged?
- How are errors returned to clients?
-->

(To be filled by the team)

---

## Error Types

<!-- Custom error classes/types -->

(To be filled by the team)

---

## Error Handling Patterns

<!-- Try-catch patterns, error propagation -->

(To be filled by the team)

---

## API Error Responses

<!-- Standard error response format -->

(To be filled by the team)

---

## Common Mistakes

<!-- Error handling mistakes your team has made -->

(To be filled by the team)

## Runtime Monitoring Evidence Contract

### 1. Scope / Trigger

Applies to `runtime_monitor.py` and any consumer of native process/socket or CCB
observations. Failure to observe is not evidence that a process or socket is absent.

### 2. Signatures

`collect_runtime(cancelled: Callable[[], bool], timeout: float = 10.0) -> RuntimeSnapshot`.
Returns immutable process, ancestor, socket, CCB and `CollectionIssue` records.

### 3. Contracts

- A process identity is `(pid, started)`, revalidated with PPID before retaining
  enrichment. Socket records retain their actual owning PID.
- Keep `process_complete`, `network_complete`, `ancestry_complete` and source issues
  separate. Commands are argv-only, `LC_ALL=C`, at most two seconds per invocation,
  with cancellation/deadline checks throughout the scan.
- `ps args` is flattened text, not quoted argv. Use native macOS argc-bounded
  argument queries or Linux `/proc/<pid>/cmdline`; do not guess boundaries with
  `shlex.split`. Never inspect the macOS transport's environment tail.
- Sanitize command values before creating snapshots. Native Claude arguments are
  not queried; ancestor multiword credential values must be redacted as one value.
- tmux replaces literal tabs under the C locale. Use printable format separators
  and split numeric suffixes from the right, preserving the session-name prefix.
- Persisted CCB PIDs/lifecycle fields do not prove liveness or supervision. Live
  pane ancestry establishes ownership; controller program/project identity is
  verified independently. Zero attached clients means detached, not stopped.

### 4. Validation & Error Matrix

| Observation | Required result |
| --- | --- |
| Successful lsof no-match with no diagnostics | No sockets observed in sample |
| Missing command, denied access, timeout or diagnostics | Source-specific limitation; never zero-activity success |
| Missing/reused/reparented process | Discard raced PID details; mark incomplete/exited |
| Unknown or wrongly typed CCB schema | Preserve OS evidence; ownership unverified, no exception |
| Stopped metadata with old PID | Historical/unverified reference, not a live process |

### 5. Good / Base / Bad Cases

Good: live detached tmux pane matches Claude ancestry and reports zero clients.
Base: no CCB metadata; a normal Claude process still has OS/socket evidence.
Bad: inaccessible argv or malformed metadata is silently shown as verified absence.

### 6. Tests Required

Keep consumer regressions for PID reuse/reparenting, missing ancestors, true argv
boundaries (spaces, quotes and multiword secrets), credential-free snapshots,
TCP/UDP/IPv6/listener semantics, unknown schema types and stale CCB references.
Also use an owned native process/socket smoke; mocked native outputs cannot prove
actual locale or tool-format behavior. Never start/stop a user's provider to test.

### 7. Wrong vs Correct

Wrong: `shlex.split(ps_args)` followed by redacting only the next whitespace token.
Correct: authoritative NUL/argc argv decoding, then whole-argument redaction.

