# Implementation Plan: Session Hub

## Phase 1: Trellis Task Setup & Scaffolding
- Update `prd.md`, write `design.md`, `implement.md`, populate `implement.jsonl`, `check.jsonl`, append research to `research.md`.
- Run `python3 .trellis/scripts/task.py start .trellis/tasks/09-21-session-hub`.
- Create `pyproject.toml` and package directory structure `src/ai_session_hub/`.
- Initialize `.venv` with Python >=3.13, install `textual==8.2.8`, `zstandard==0.25.0`, and editable package.

## Phase 2: Core Contracts & Storage
- `src/ai_session_hub/models.py`: Implement frozen dataclasses `SourceSpec`, `SessionRef`, `MessageRecord`, `SessionSnapshot`, `LaunchSpec`, `Unavailable`.
- `src/ai_session_hub/adapters/__init__.py`: Define adapter Protocol (`discover`, `read`, `prepare_resume`) and adapter registry.
- `src/ai_session_hub/source_io.py`: Read-only SQLite helper (`?mode=ro`, `PRAGMA query_only=ON`), JSONL stream reader with line-boundary and partial-line safety, rich text sanitizer.
- `src/ai_session_hub/store.py`: Implement SQLite store (`index.sqlite3`) with tables `sessions`, `messages`, `message_fts` (trigram), `session_aliases`, `source_status`, triggers, and basic CRUD.

## Phase 3: Adapters Implementation
- `src/ai_session_hub/adapters/codex.py`: Rollout JSONL, state SQLite threads, and paginated SQLite `thread_items`.
- `src/ai_session_hub/adapters/claude.py`: Project transcripts JSONL, UUID parent chaining, search cache fallback.
- `src/ai_session_hub/adapters/hermes.py`: SQLite `state.db`, `\x00json:` sentinels, compaction summary live user splits, non-branch compression tips.
- `src/ai_session_hub/adapters/omp.py`: JSONL sessions, title slot parsing, full message history across reset boundaries, branch ancestry.
- `src/ai_session_hub/adapters/dsh.py`: Streaming zstandard JSONL, message extraction, seeded context retention.
- `src/ai_session_hub/adapters/catalog.py`: Token Monitor usage archive reader, Cursor IDE conversation search reader, unambiguous alias reconciliation.

## Phase 4: Indexing & Search Engine
- `src/ai_session_hub/indexer.py`: Incremental scanner for file and SQLite sources, change detection via (inode, size, mtime_ns) and `PRAGMA data_version`.
- `src/ai_session_hub/store.py`: Implement `search(query, filters, offset, limit)` and `context(session_key, message_id)`.
- `src/ai_session_hub/config.py`: Source discovery, environment root resolution, TOML config parsing, and path validation.

## Phase 5: Resume Engine & Terminal Handoff
- `src/ai_session_hub/resume.py`: Launch validation (executable, cwd, profile, identity), subprocess execution, and Textual `suspend()` terminal handoff with SIGINT handling.

## Phase 6: UI & Terminal Interface
- `src/ai_session_hub/ui_layout.py`: Static layout with widget IDs `query`, `tool-filter`, `project-filter`, `time-filter`, `kind-filter`, `sessions`, `preview`, `scan-status`.
- `src/ai_session_hub/ui.tcss`: Responsive styling and layout rules (stacking below 100 cols).
- `src/ai_session_hub/ui.py`: `HubApp(App)` implementation, debounced search, thread workers for store queries and indexing, keyboard bindings (`/`, Up/Down, Enter, Tab, Escape, Ctrl+R, `r`, `q`, Ctrl+Q, PageUp/PageDown).

## Phase 7: Automated Behavior Testing
- `tests/test_ingestion.py`: Ingestion correctness across Codex, Claude, Hermes, OMP, DSH, Catalog.
- `tests/test_indexing.py`: Incremental updates, file changes, WAL changes, alias reconciliation, isolation.
- `tests/test_search.py`: Assistant search (Chinese/English), literal matching, Unicode NFKC normalization, FTS fallback.
- `tests/test_resume.py`: LaunchSpec generation, shell metacharacter safety, profile resolution, unavailable reasons.
- `tests/test_ui.py`: Textual pilot tests for search, selection, preview, resume confirmation.
- `tests/test_terminal.py`: PTY child fixture, cooked mode restoration, signal handling.

## Phase 8: Verification & Acceptance
- Run full automated test suite with `.venv/bin/python -m unittest discover -s tests -v`.
- Launch supervised PTY session (`hub start`) and verify live TUI behavior against fixture data.
- Verify acceptance criteria AC1–AC6 and prepare final report.

## Compatibility repair

1. Compare downloaded Token Monitor source and installed native CLI help/source against existing adapters; diagnose with read-only metadata and minimal fixtures.
2. Correct OMP/Claude/Hermes/DSH native identity, classification, environment and launch arguments where evidence shows a mismatch. Version parser revisions so unchanged transcripts are reindexed.
3. Remove the aggregator from platform options; display originating platform identities without removing catalog coverage.
4. Benchmark SQLite search before/after; avoid zero-hit fallback scans and fetching all matching bodies before pagination. Isolate UI read connections from the indexing writer; cancel stale queries.
5. Run integrated regressions and supervised terminal scenarios, including actual native CLI loading without prompts where feasible. Record blockers and measured outcomes in research/README.

Ownership: adapter/resume changes and search/store changes are independent; main integrates UI, catalog, parser invalidation and documentation. No vector dependency, cloud API, or new platform parser is introduced in this repair.

Verification completed: 45 regression tests pass with ResourceWarning treated as
an error; actual isolated OMP/Hermes/DSH launches exercised without model prompts;
browser keyboard confirmation, native handoff, return, and terminal restoration
checked. Search measurements and unavailable-source limits are recorded in
`research.md` and usage is documented in `README.md`. At that point Git was absent;
the subsequent explicit user request authorized initialization, now completed on
`main` with `sidecar`/private/generated artifacts ignored and no staging or commit.

## Native analytics expansion — implemented and verified

This section supersedes the original Token Monitor catalog implementation steps.
The approved final summary was implemented; accounting and terminal evidence is in `research.md`.

1. **Independent sources and migration** — main owns config, registry, catalog,
   models, store and indexer. Remove Token Monitor discovery/adapter/config support
   and archive-only reconciliation; keep direct Cursor metadata. Upgrade only the
   app-owned schema, remove old archive-derived rows/status/links, preserve native
   records and source files. Advance parser revisions for usage backfill.
2. **Shared native accounting** — main defines immutable usage/provenance/filter
   contracts and integrates native parsers for Codex/Claude/Hermes/OMP/DSH. Usage
   records are independent of emitted prose. Handle cumulative snapshots, stable
   request IDs, replay/seed/compaction, overlapping cache/reasoning, partial data
   and coarse session/model counters explicitly. Add local configurable pricing
   only under the reviewed policy; never borrow Token Monitor's pricing cache.
3. **One aggregation path** — persist facts in the snapshot transaction; implement
   SQL dashboard/model/day, session and project aggregates with consistent scopes
   and coverage. Keep read connections separate from the indexing writer. Preserve
   existing literal search performance and exact resume behavior.
4. **Three TUI views** — main owns navigation/state/workers and handlers. After
   fixing the widget/data contract, dispatch static layout/TCSS/formatting or
   synthetic fixture work to Gemini 3.8 on disjoint files, while main integrates
   dynamic data. Implement dashboard totals/model shares/heatmap/trend, session
   usage detail and project drilldown. Verify usable narrow terminal layout.
5. **Verification** — synthetic fixture checks for migration, repeated scan and
   replacement, cumulative/replayed/inherited usage, cache/reasoning overlaps,
   source cost/estimate/unknown handling, project identity and aggregate parity.
   Run `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -q`
   once after integrating concurrent writes. Smoke the actual application in a
   supervised PTY with `discover_defaults=false` before aggregate-only native
   discovery. Verify all views, filters, project drilldown and unchanged resume.
   Verify runtime never reads Token Monitor/sidecar; no model prompts or credentials.
6. **Cleanup after proof** — update README/research/acceptance evidence and remove
   owned throwaway fixtures/processes. No automatic commit or push; user authorized
   Git initialization, not a commit. Keep remaining provider/format limitations
   explicit rather than counting unavailable data as zero.

Already completed independently: Git bootstrap by Gemini 3.8 and read-only reference
mapping by a concurrent Gemini 3.8 scout. Main verified ignore rules and checked
native Hermes/OMP source to correct unsupported reference-only cost assumptions.

## OMP-style redesign — implemented and verified

The user approved this design before implementation. Evidence is in `research.md`.

1. Establish keyed zh-CN/en presentation and isolated preference persistence;
   inspect symbol references before changing shared configuration/formatting APIs.
2. Recompose compact navigation, search/scope summary, restrained dashboard and
   responsive sessions/project detail surfaces in existing layout/style modules.
3. Integrate settings, searchable filters/reset, contextual focus/shortcuts,
   stable selection/scroll restoration and asynchronous paged project expansion.
4. Update existing behavioral UI tests; add only uncertain language persistence,
   state-preservation and beyond-200-project accessibility regression boundaries.
   Preserve exact-resume/terminal-lifecycle checks. Run the full suite once after
   integration: `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -q`.
5. Run synthetic-data actual supervised PTY scenarios at 80x24 and 140x45 in both
   languages; inspect the rendered terminal surface, exercise settings/restart,
   filter search/reset, list/detail/back, project loading and fixture handoff.
   Do not publish private history or automatically launch a live native session.
6. After proof, update README and verification evidence, stop owned processes and
   remove throwaway artifacts. Report clipboard/live-native verification separately.

Existing R1–R13 behavior remains required. No adapter/accounting rewrite, database
migration, new UI framework, global OMP modification or font installation is planned.
