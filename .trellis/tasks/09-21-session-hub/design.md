# Technical Design: Session Hub

## 1. System Architecture

`ai-session-hub` is a local, keyboard-driven Python 3.13/Textual application for native AI conversation discovery, usage analytics, incremental user/assistant search, and safe original-CLI resume. Native analytics replaces the original Token Monitor archive design.

```
┌─────────────────────────────────────────────────────────────┐
│                       Textual TUI                           │
│  (HubApp: Search, Filter, Session DataTable, Rich Preview)  │
└──────────────────────────────┬──────────────────────────────┘
                               │
               ┌───────────────┴───────────────┐
               ▼                               ▼
     ┌───────────────────┐           ┌───────────────────┐
     │  Resume Engine    │           │  Store & Search   │
     │  (resume.py)      │           │  (store.py)       │
     │  - Validation     │           │  - SQLite WAL     │
     │  - Subprocess     │           │  - FTS5 Trigram   │
     │  - Term Handoff   │           │  - Exact Verify   │
     └─────────┬─────────┘           └─────────▲─────────┘
               │                               │
               ▼                               │ write snapshot
        [Native CLI]                 ┌─────────┴─────────┐
      (codex, claude,                │  Indexer Worker   │
       hermes, omp, dsh)             │  (indexer.py)     │
                                     └─────────▲─────────┘
                                               │ read snapshot
                                     ┌─────────┴─────────┐
                                     │     Adapters      │
                                     │ (codex, claude,   │
                                     │  hermes, omp,     │
                                     │  dsh, catalog)    │
                                     └─────────▲─────────┘
                                               │ read-only IO
                                     ┌─────────┴─────────┐
                                     │     Source IO     │
                                     │ (source_io.py)    │
                                     └───────────────────┘
```

## 2. Core Data Models (`models.py`)

- `SourceSpec`: Frozen definition of an ingestion source (tool name, root path, optional CLI profile, optional OMP sessions_dir).
- `SessionRef`: Stable reference to a discovered session (canonical key, source spec, native ID, locator paths, revision stamp).
- `MessageRecord`: Individual retained message (source_id, parent_id, ordinal, role: "user" | "assistant", text body, timestamp_ms, flags).
- `SessionSnapshot`: Complete session state (ref, client, title, project_id, project_label, cwd, started_ms, updated_ms, kind, archived, text_state, messages, usage, warnings).
- `UsageRecord`: Non-overlapping native event/session fact with nullable token components, timestamp/model/provider and cost provenance. `usage.py` defines typed aggregate results and explicit local pricing rules.
- `LaunchSpec`: Complete verified launch contract (argv, cwd, env_overrides, env_remove).
- `Unavailable`: Explicit diagnostic reason when resume is blocked (code, reason).

## 3. Storage and Indexing (`store.py`, `indexer.py`)

- Current database: application-owned `<data-dir>/index.sqlite3`, schema version 2, WAL, foreign keys, owner-only permissions. Migration from version 1 never opens original histories.
- Tables:
  - `sessions`: Session metadata, locator revisions, status, and exact project identity.
  - `messages`: Normalized message bodies, ordinals, roles, parentage, and flags.
  - `message_fts`: FTS5 trigram external-content table synced via triggers.
  - `usage_records`: Native facts keyed by `(session_key, source_id)` with cascading deletion.
  - `source_status`: Source health and scan diagnostics.
- Search Strategy: Unicode NFKC normalization + casefold. Queries >= 3 characters use FTS5 trigram MATCH candidate generation followed by literal `instr` verification. Queries < 3 characters use parameterized literal `instr`. Fallback literal search when FTS5 is unavailable.
- Indexing: File-granularity change detection using inode/size/mtime_ns; database revisions include both the main file and WAL stamp.

## 4. Adapters

- **Codex**: Ingests JSONL rollout sessions (`sessions/`, `archived_sessions/`), SQLite state metadata (`threads`), and paginated dialogue (`thread_items` in `thread_history_*.sqlite`). Merges projection state without resurrecting deleted turns.
- **Claude Code**: Ingests JSONL transcripts (`projects/`, `transcripts/`) with UUID parent chaining, and historical cache (`.search-index/search.db`).
- **Hermes**: Ingests `state.db` directly, decodes `\x00json:` sentinels, preserves compaction summaries with live user message splits, and follows non-branch compression tips for resume.
- **Oh My Pi (OMP)**: Ingests session JSONL files with fixed title slots, full message histories across reset boundaries, and branch lineage. Resumes with exact JSONL path.
- **DSH**: Ingests streaming zstd compressed JSONL (`session.v*.jsonl.zstd`), extracts user and assistant messages, and preserves seeded parent context.
- **Direct IDE catalog**: Cursor IDE metadata comes directly from its own store. Token Monitor discovery, adapter, registration/configuration and aliases have been removed.

## 5. Resume and Terminal Handoff (`resume.py`)

- Pre-launch validation: Executable presence, directory existence, native identity verification, profile matching, and non-archived checks.
- Execution: Subprocess invocation without shell, passing absolute executable path, verified cwd, and exact CLI flags.
- Terminal handoff: Textual `App.suspend()` context with internal error catching, temporary SIGINT handling, terminal state restoration, and immediate post-resume rescan.

## 6. Native analytics implementation

### Change boundary and data flow

`SessionSnapshot.usage` carries adapter-decoded facts separately from dialogue.
`Store` accepts schema 2 and exposes `analytics()` and `session_usage()` through one
SQL accounting path; analytics panels share a read snapshot. The Textual widget
tree switches between dashboard, sessions and projects. There is no HTTP server,
external analytics database, Token Monitor import or generic plugin layer. Native
sources are read-only inputs; `sidecar` is not read at runtime.

Native records → adapter-owned usage decoding/deduplication → session snapshot →
transactional SQLite facts → typed aggregate results → three Textual views.
Resume remains its existing separately confirmed operation.

### Accounting interface

The frozen `UsageRecord` tuple is part of `SessionSnapshot`.
Usage exists independently of emitted dialogue: tool-only
generations can consume tokens, whereas seeded dialogue must remain searchable
without being charged again. Retain stable source event/request identity, optional
native timestamp/model/provider, normalized input/output tokens, cache-read/write
and reasoning subsets when known, optional total, optional cost and provenance.
Unobserved fields are `None`, not zero. Source-reported zero stays zero.

Each adapter owns format-specific extraction and conversion of cumulative counters
to non-overlapping increments. Do not apply a generic text deduplication or guess
that every `usage` object is incremental. Normalize input to include cache subsets
and output to include reasoning subsets where native semantics are verified; total
does not add those subsets twice. Preserve explicit native totals with a warning
when components cannot be reconciled. Inherited/compacted/replayed usage is not a
new charge; stable event identities govern deduplication, not prose equality.

Keep coarse session-only native counters distinguishable from timestamped events:
never invent per-day/model allocation or add a session total on top of its event
breakdown. Unknown dates/models appear as unattributed coverage. Scope every
aggregate to the same selected platforms/projects and time interval, and make
partial/unknown coverage visible. Costs must distinguish recorded/reported values
from estimates; no implied invoice, missing-price zero, live provider query or
credential access. Implemented policy: prefer native recorded costs (retaining whether
they are actual or estimated); otherwise estimate from explicitly configured local
per-model/provider USD-per-million rates. No model-name guessing or live price fetch;
unpriced components remain unknown and totals show priced coverage.

Token-record coverage and priced-record coverage are explicit independent counters.
Search date filters use session activity; analytics date filters use native event
timestamps. Session totals remain lifetime totals. These scopes are labeled in UI.
Codex context-capacity markers are not consumption: retain their cumulative baseline
for later deltas but emit no charge or state-counter fallback. FTS fallback is allowed
only for an absent optional table; SQLite may report cancellation during virtual-table
setup as `SQLITE_ERROR`, so unknown operational errors must propagate.

### Persistence and cutover

Upgrade app-owned schema 1 transactionally to schema 2. Add a usage-facts table
keyed by `(session_key, source_event_id)`, with FK cascade and indexes supporting
time/model/project aggregation. Replace usage and dialogue with the same snapshot
transaction; reparse native sources by advancing adapter parser revisions so
unchanged files acquire usage. Preserve existing native search rows throughout.
Unknown newer schemas still fail explicitly; the read-only query connection must
accept the migrated version. Migration failure must roll back, not delete/rebuild
the user's index indiscriminately.

Delete legacy Token Monitor-derived rows/status/links inside this migration, not
the original archive database. Remove its source discovery and accepted config
tool, adapter, UI branches, canonical archive key and archive-only reconciliation.
Retire alias structures only after verifying all remaining native/IDE consumers;
do not retain empty compatibility shims. An explicit legacy source configuration
must produce an actionable removed-source error. Old source-only platforms may
disappear: absence of a native reader is not hidden by claiming archive support.

Expose a small Store query interface for dashboard aggregates, project summaries
and per-session usage; SQL performs grouping/paging, not UI-side rescans of entire
histories. Reuse separate worker-owned read connections and stale-request generation
checks. SQLite remains the only application database; no vector/embedding change.

### Views and interaction

- **Dashboard** (initial view): total tokens, known cost and coverage; model ranking
  with shares; local-calendar activity heatmap/active days; daily trend and peak.
  Use Textual/Rich-rendered terminal charts, not external web surfaces or screenshot
  values. Time/platform filters refresh all panels together.
- **Sessions**: retain search, literal previews, pagination, exact resume and source
  diagnostics. Add tokens/model(s)/message-count/known-cost columns or responsive
  detail rows. Detail shows token components and provenance. Search selection
  survives switching views and native handoff.
- **Projects**: aggregate by canonical recorded project/cwd identity, not basename;
  show label plus disambiguating path, sessions, tokens, known cost and share.
  Unknown project is an explicit group. Enter drills into the session view with
  an exact project filter; returning preserves project selection.
- Use visible keyboard navigation among all three views; avoid intercepting typed
  search characters. Narrow terminal layouts stack panels and retain reachable
  controls. Analytics uses native usage-event time; session activity filtering and
  lifetime totals must be labeled separately rather than silently conflated.

### Ownership and non-goals

Main owns `models.py`, native adapters, store migration/aggregates, indexer, UI state,
resume regression safety, and final verification. After contract approval Gemini
may own static layout/styles and fixed-contract formatting/fixture work in disjoint
files. It must not design billing semantics or mutate shared interfaces independently.
No Token Monitor runtime, wholesale source vendoring, Electron rewrite, provider
quota collector, automatic native CLI installation, or prompt submission.

Git initialization is independent and already user-authorized; ignore `sidecar`
and generated/private files, no automatic staging/commit/push. Runtime installation
and operation must also succeed when `sidecar` is absent.

## OMP-style redesign — approved and implemented

Use the existing Textual application and workers, not OMP source or a new renderer.
`ui_layout.py` and `ui.tcss` own compact navigation, minimal dividers, neutral dark
surfaces, contextual status and responsive list/detail presentation. Preserve the
three views and existing F1/F2/F3 assignments; sessions is the initial view.
Plain labels plus `>`, `+`, `-` and line-drawing separators replace decorative icons.

`ui.py` owns focus/overlay transitions, searchable filter selection, explicit reset,
detail disclosure and project child pagination. Filter summaries describe the actual
query scope, including the difference between activity time and usage-event time.
Project page reads move off the UI thread and reject obsolete results. State tracks
stable session/project keys, filters, expanded projects and each preview's position;
language updates change presentation rather than reindexing/remounting the app.

Introduce a small `i18n.py` with keyed zh-CN/en dictionaries and formatting at the
presentation boundary. Do not translate stored facts or use rendered labels as IDs.
Audit dynamic formatting in `ui.py`, `ui_layout.py` and usage presentation helpers;
adapter/launcher error codes retain their meaning and raw diagnostic detail remains
verbatim under localized explanation. English UI means application-owned copy, not
rewriting original Chinese conversation content.

Persist only language in app-owned `ui-preferences.json`, after existing data-root
isolation checks. Use an atomic replacement with owner-only permissions; never
rewrite source/pricing TOML. Saved explicit choice wins, otherwise detect Chinese
locale and use English for others. Invalid preferences have a visible recoverable
warning and safe default. No filesystem changes to source roots.

Preserve dashboard facts and coverage labels; replace card decoration with compact
summary and aligned tables. Keep estimated/partial/unknown distinctions. Long paths
remain available in details; same-named projects must remain distinguishable.

Risks to verify: CJK cell width, wrapped paths, focus after closing overlays, language
updates during worker delivery, text-selection shortcut conflicts, narrow viewport
scroll position and project pagination without truncation/duplicate children.
