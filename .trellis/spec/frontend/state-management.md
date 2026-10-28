# State Management

> How state is managed in this project.

---

## Overview

<!--
Document your project's state management conventions here.

Questions to answer:
- What state management solution do you use?
- How is local vs global state decided?
- How do you handle server state?
- What are the patterns for derived state?
-->

(To be filled by the team)

---

## State Categories

<!-- Local state, global state, server state, URL state -->

(To be filled by the team)

---

## When to Use Global State

<!-- Criteria for promoting state to global -->

(To be filled by the team)

---

## Server State

<!-- How server data is cached and synchronized -->

(To be filled by the team)

---

## Common Mistakes

<!-- State management mistakes your team has made -->

(To be filled by the team)

## Runtime View Lifecycle Contract

### 1. Scope / Trigger

Applies to HubApp background runtime collection, live resizing and application exit.

### 2. Signatures

`RuntimeReady(generation, snapshot, error=None)` delivers one immutable observation;
`on_runtime_ready` owns applying or rejecting it. `on_resize(event: Resize)` uses
the event's dimensions, then defers size-dependent rendering until framework resize.

### 3. Contracts

- Use `_workspace_closing` for application shutdown intent. Textual owns `_closing`;
  setting it before `exit()` rejects the framework's own `ExitApp` message.
- One in-flight collection at a time. Leaving Runtime, handoff or close invalidates
  its generation. Stale completion releases the in-flight slot but cannot replace
  a current snapshot; visible reentry starts a fresh scan once the old one finishes.
- Pause new scans while hidden/handoff/closing. Refresh actions in Runtime never
  trigger history indexing, session resume or history filtering.
- Selection keys include PID/start. If an identity disappears, clear details until
  the user selects a current row; do not silently select a reused PID.
- `App.is_mounted` is a method, not a boolean. Check the mounted screen widget;
  on live resize, `self.size` can still hold the preceding dimensions in the handler.

### 4. Validation & Error Matrix

| Transition | Required result |
| --- | --- |
| Hidden/reentered while collection blocked | No overlapping scan; reject old result |
| Native handoff or close | Stop scheduling, invalidate results, allow framework shutdown |
| Wide to 80 columns to wide | Stacked usable details, then restored split layout/focus |
| PID/start replacement | Old command/ancestor/socket details cleared |
| Collector failure | Preserve last sample as stale and display failure, not an empty success |

### 5. Good / Base / Bad Cases

Good: entering Runtime starts one worker and shows observation time/completeness.
Base: an empty successful observation displays the explicit no-process state.
Bad: a quit action sets Textual `_closing=True`, then cannot post its exit message.

### 6. Tests Required

Assert visible stale-result rejection, no overlapping worker, handoff pause,
PID/start replacement, live wide/narrow/wide geometry and actual stopped message
processing after quit. A headless context exiting by itself is not quit proof;
exercise actual PTY navigation/resize/language/refresh/exit as smoke evidence.

### 7. Wrong vs Correct

Wrong: `_closing = True; self.exit()` or sizing from stale `self.size` in resize.
Correct: `_workspace_closing = True; self.exit()`, and `event.size.width` followed
by deferred rendering after Textual updates its dimensions.

