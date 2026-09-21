# Local discovery findings

Inspected on 2026-09-21, on the user's macOS machine. Only application code, directory presence, schema/field names, aggregate counts, and CLI help were inspected for these findings. Conversation bodies and credentials are not included here.

## Token Monitor

- Installed app: `/Applications/Token Monitor.app`, version `0.58.0`, bundle ID `com.javis.tokenmonitor` (Info.plist).
- Data directory: `~/Library/Application Support/Token Monitor`.
- Archive: `session-usage-archive.sqlite`, inspected with SQLite read-only mode. It uses WAL; readers must account for active WAL state rather than copying only the main database or assuming an immutable file.
- Tables: `metadata(key, value)` and `sessions(session_key, entry_json, revision)`.
- Entry fields: `capturedAt`, `client`, `day`, `month`, `periodWindows`, `periods`, `sessionId`.
- `periods.allTime` includes title, project identity/label, timestamps, session kind, model/provider aggregates, message count, token counts, and costs. It contains no full conversation message array.

Observed archive rows:

| Client | Rows |
| --- | ---: |
| codex | 265 |
| cursor | 18 |
| antigravity | 17 |
| pi | 16 |
| hermes | 13 |
| dsh | 5 |
| workbuddy | 3 |
| copilot | 1 |
| Total | 338 |

These are archive rows, not a verified complete count of native conversations. For example, a local Claude projects directory exists despite no Claude rows in this snapshot.

## Application implementation evidence

The application bundles readable JavaScript in `Contents/Resources/app.asar`. Relevant source was extracted only into temporary research storage. Do not vendor application source into this project; implement independent adapters against observed formats and verified tool interfaces.

- `src/shared/clientCatalog.js:43`: catalog lists many clients, including Codex, Claude, Hermes, Cursor, Antigravity, Pi, WorkBuddy, Copilot, and DSH. Catalog presence alone establishes neither full transcript availability nor resumability.
- `src/shared/sessionFiles.js:31`: Codex root honors `CODEX_HOME`; default is `~/.codex`.
- `src/shared/sessionFiles.js:37`: archive identifiers can be `rollout-...` file stems. They must not be assumed to be the UUID accepted by the CLI; read canonical native metadata.
- `src/shared/sessionFiles.js:46`: Claude source discovery checks project and transcript roots. Codex source lookup here covers sessions; the new project must also account for locally present archived sessions.
- `src/shared/sessionDetail.js:93`: Claude parsing builds prompt and usage events; assistant events carry usage/tool names rather than complete assistant response text. This output is insufficient for full dialogue search.
- `src/shared/sessionDetail.js:393`: detail handling branches to OpenCode and Reasonix readers, with generic file readers for other supported paths.
- `src/shared/sessionDetailResolver.js:22`: DSH has a distinct detail reader; comments identify zstd transcript storage. Plan for a specific decoder rather than assuming JSONL everywhere.

Temporary source root during this investigation: `/private/tmp/ai-session-hub-research.SPAfPQ/token-monitor`. Evidence anchors refer to paths inside the installed archive and can be recovered by re-extraction if temporary storage is cleaned.

## Native source and resume evidence

| Tool | Local observation | Verified help / limitation |
| --- | --- | --- |
| Codex | `~/.codex/sessions` and `~/.codex/archived_sessions` exist; `codex` on PATH | `codex resume [OPTIONS] [SESSION_ID]`; `-C/--cd` sets work root. No live session resumed. |
| Claude Code | `~/.claude/projects` exists; `claude` on PATH | `claude --resume [value]` accepts a session ID. No live session resumed. |
| Hermes | `~/.hermes/sessions` exists; `hermes` on PATH | `hermes --resume <session_id>`; `--in DIR` overrides recorded cwd. The separate `hermes resume` subcommand lifts an emergency stop and MUST NOT be used for conversation resume. |
| DSH | `~/.dsh/sessions` exists; `dsh` on PATH | Launcher help shows `dsh --profile tui --resume <session>` forwarding to the app; app-level semantics still need verification. |
| Cursor | `agent` on PATH | `agent --resume [chatId]` and `--workspace <path>` exist. This does not prove Cursor IDE archive IDs are CLI chat IDs. |
| Pi | Archive rows present | `pi` absent from PATH and default `~/.pi/agent/sessions` absent. Custom roots or historical installations still need investigation. |
| Antigravity, WorkBuddy, Copilot | Archive rows present | Native transcript format and exact resume interface not yet verified. |

Gemini and OpenCode executables were not found on the current PATH; that does not prove their historical data is absent.

## Design consequences

1. Use Token Monitor as an optional catalog/enrichment source, not the sole transcript source or required running service.
2. Separate discovery, transcript reading, and resume capabilities per adapter. Represent metadata-only and unavailable records explicitly.
3. Native conversation identity must include tool, source/profile root, and native ID; retain archive identity as an alias.
4. Build a separate local index; preserve raw history. Search must include assistant replies, Chinese text, and accurate message boundaries.
5. Launch exact argument arrays with cwd and relevant profile environment. Never generate a shell command from conversation titles or substitute a latest-session operation.

## Remaining investigation

- Scope-dependent native formats, custom scan roots, profiles, archive/subagent behavior, and source availability.
- DSH TUI resume semantics and Cursor IDE vs CLI identity compatibility.
- UTF-8/CJK indexing strategy, terminal framework selection, dependency APIs, and terminal handoff validation.
- Real end-to-end resume checks after implementation, without sending unsolicited prompts.

## Verified research evidence from planning

- Environment: Python 3.13.9 and SQLite 3.51.0 available; Textual not installed. PyPI metadata confirms chosen Textual 8.2.8 and zstandard 0.25.0 versions.
- Search capability: In-memory SQLite FTS5 trigram experiment matched `中文检索` and an assistant-only English phrase; literal fallback matched `中文`, `%`, and `"`. No project or source data was changed.
- Format verification: Read-only schema queries verified Token Monitor archive, Codex state and paginated history, Hermes state, and Claude historical cache. A zstd-to-memory decode verified DSH v3 field shapes, notably `session/title.data.title`. CLI help verified Codex's UUID resume and `--cd`/`--profile` options. No live native session was resumed.

## Compatibility repair research (2026-09-21)

Downloaded upstream source to temporary storage, pinned at `8ad5769cb0636a59df4e92b7797c955ada833594`. Source was inspected, not vendored or executed.

### Upstream compatibility is not resume compatibility

- [Client catalog](https://github.com/Javis603/token-monitor/blob/8ad5769cb0636a59df4e92b7797c955ada833594/src/shared/clientCatalog.js#L42-L77) enumerates 30 usage clients. Token Monitor itself is not a client. Discovery/usage support alone does not prove complete dialogue extraction or native resume.
- [Pi watcher roots](https://github.com/Javis603/token-monitor/blob/8ad5769cb0636a59df4e92b7797c955ada833594/src/shared/collector.js#L1865) include both `.pi/agent/sessions` and `.omp/agent/sessions`; [Tokscale mapping](https://github.com/Javis603/token-monitor/blob/8ad5769cb0636a59df4e92b7797c955ada833594/src/shared/tokscaleClientMapping.js#L7-L16) explicitly groups OMP under Pi. Preserve archive client identities and reconcile only unique native identities; never blindly launch `omp` for every `pi` archive row.
- [Claude detail parser](https://github.com/Javis603/token-monitor/blob/8ad5769cb0636a59df4e92b7797c955ada833594/src/shared/sessionDetail.js#L94-L146) emits user prompts and assistant usage/tool events, not complete assistant reply text. Reusing those output events would lose searchable dialogue.
- [DSH detail routing](https://github.com/Javis603/token-monitor/blob/8ad5769cb0636a59df4e92b7797c955ada833594/src/shared/sessionDetailResolver.js#L18-L35) uses a dedicated zstd reader rather than the generic JSONL path. Per-platform decoding is still required before writing one local normalized search index.
- The upstream research found no native CLI conversation-resume implementation to reuse. Native executable/version/help/source must establish each launch contract independently.

### Local findings

- Before repair, the application index contained 74,554 messages and occupied 619,638,784 bytes (main database file). These are point-in-time aggregates, not completeness claims.
- All 35 native OMP records were classified as subagents because the adapter rejected any parent directory below `sessions/`. Native OMP uses project buckets for primary conversations; child artifacts have their own structure.
- Claude's 607 indexed sessions were search-cache entries. Read-only inspection found no project/transcript JSONL files and no existing cache `file_path` targets. Restoring native history is a prerequisite for those records to resume; cached text is not a safe replacement for the original transcript.
- DSH's installed TUI entrypoint is inside `profiles/dsh-tui/node_modules/@deepseek-harness-tui/dsh-tui/bin/`, not directly inside `profiles/dsh-tui/bin/`. Checking the latter incorrectly rejects an initialized installation.

### Search architecture decision

Keep SQLite as the local canonical index. Fix FTS zero-hit handling, SQL-side filtering/counting/pagination, page-only body retrieval, and independent reader/writer connections before adding another engine. Original storage formats belong in adapters, not in query execution.

A vector index would add semantic similarity (different wording with similar meaning), not replace exact IDs, literal phrases, Chinese substrings, or `%`/quote matching. If semantic search is requested later, make it an optional local embedding/hybrid layer keyed by canonical session/message IDs, versioned with the embedding model and updated/deleted with the same ingestion revision. Preserve the existing literal path as the exact-search authority. No embeddings or vector dependency are added in this repair.

### Search measurements

Synthetic, unchanged corpus: 600 sessions, 36,000 messages, 39,515,466 body characters, 196,833,280-byte SQLite file; five samples per query, page size 20. Before/after result counts and SHA-256 signatures of returned identities/match types/excerpts agreed for all six cases.

| Query case | Before median ms | After median ms |
| --- | ---: | ---: |
| No match | 69.411 | 0.266 |
| Rare term | 0.227 | 0.472 |
| Common term | 212.573 | 69.676 |
| Common term with platform filter | 236.142 | 47.052 |
| Two-character term | 212.508 | 58.354 |
| Later page | 205.911 | 67.653 |

Post-fix read-only measurements on the user's existing 74,554-message index (three samples, 20 results/page): absent synthetic term 5.427 ms; `session` 65.156 ms (436 sessions); `中文` 174.516 ms (294 sessions). These measure SQLite search only, not debounce or terminal rendering. Two-character queries remain literal scans; vectors would not preserve their exact substring semantics.

### Native resume and terminal verification

- Final regression run: `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -q` passed 45 tests in 7.459 seconds. Trellis JSONL context validation also passed. No project lint/type-check command is configured.
- OMP loaded the exact isolated synthetic history and displayed `SMOKE_RESTORED_HISTORY`; Ctrl+D exited with code 0. Full browser → confirmation → actual OMP → browser → quit was also exercised through a supervised PTY, with exit 0 and terminal restoration reported true.
- Hermes loaded the isolated native-schema session `session-hub-smoke-hermes`; its exit summary reported the same session and two stored messages. Provider setup was declined and no prompt was submitted.
- DSH loaded an isolated v3 session through the actual installed `dsh-tui` profile and `/exit` returned code 0 with terminal restoration true. The synthetic header needed integer `createdAt` and a project bucket matching native cwd encoding; failed preliminary launches were fixture errors, not production fixes. This fixture had no conversation events, so it verifies exact-session loading/startup rather than populated-history rendering.
- DSH TUI 0.10.1 warns that engine 0.1.5-rc.2 is newer than its validated 0.1.5-rc.1. The installed versions were left unchanged.
- PTY testing found that a child can leave raw terminal modes behind. `run_foreground` now snapshots and restores terminal attributes in `finally`; a child that deliberately leaves raw mode fails the regression before the fix and passes afterward. macOS may set the transient `PENDIN` retype flag on canonical-mode restoration, so mode comparisons exclude only that flag.
- Live keyboard testing found that the initially focused Cancel button consumed Enter before the modal Confirm binding. A priority Confirm binding now matches the advertised shortcut; the regression observed `[False]` before the fix and `[True]` afterward, and an actual OMP launch via Enter was verified.
- Importing one concrete adapter previously suppressed registration of all other built-ins. Cached unconditional built-in imports remove the import-order dependency; standalone resume/UI tests and the full suite pass.
- Static peer review of native compatibility changes reported no actionable findings. Live native CLI testing was performed by the integration owner, not the reviewer. Missing Claude transcripts and unverified IDE resume identities remain explicit availability limits, not fabricated successful resumptions.
- All native smoke sessions used disposable history/configuration roots. OMP standalone smoke disabled extensions/tools; Hermes used `--cli --safe-mode`. No prompts were sent to models and no original conversations were resumed or rewritten.
- The workspace is not a Git repository; no commit or Trellis auto-commit/archive was performed.
- Final Hermes mode check returned exit 0 and stable terminal attributes restored. The only raw difference was local flags `1483 → 536872395` (`PENDIN`, `0x20000000`); no input/output/control mode, speed, or control-character setting differed. Raw tuple equality had therefore reported a false negative after the repair.

## Independent native analytics planning (2026-09-21)

The user now explicitly rejects Token Monitor as a runtime/data dependency and
requests dashboard, session usage and project views. `sidecar/token-monitor` is a
reference checkout only, excluded from Git. This supersedes earlier archive-catalog
decisions; references above remain historical evidence, not the new source policy.

### Current implementation seam evidence

- `models.py:71-97`: message/session snapshots contain no token/cost usage contract.
- `store.py:63-114`: both writable and read-only paths require schema 1; analytics
  needs a transactional app-owned migration, not a source-database modification.
- `config.py:110`, `adapters/catalog.py:24-152`, `store.py:435-476`, and
  `indexer.py:171-174`: Token Monitor discovery/reader and persisted reconciliation
  are real runtime paths. Hiding a filter option is not independence. Existing
  imported index rows must be removed as well as disabling future reads.
- `ui_layout.py:17-71`: current layout is search/filter plus session table/preview;
  preserve the working search/resume state while adding three connected views.

### Reference source findings, independently checked

- `sidecar/token-monitor/src/shared/collector.js:601-635`: pricing is delegated to
  `tokscale pricing`; its cached catalogs come from LiteLLM/OpenRouter/models.dev
  and may be refreshed over the network. Do not inherit this runtime/cache/network
  dependency into Session Hub. Reference UI totals are not evidence of invoices.
- `sidecar/token-monitor/src/shared/sessionDetail.js:96-124`: Claude usage can be
  repeated by line-UUID replay and assistant content blocks sharing `message.id`.
- Same file `:204-220`: Codex `event_msg/token_count.info.last_token_usage` exposes
  input/cached input/output/reasoning; cached input is included in input and
  reasoning is included in output. The reference's simple event loop is not proof
  that repeated cumulative snapshots are safe to sum in our independent parser.
- `sidecar/token-monitor/src/shared/providers/dsh/sessionDetail.js:123-136,169-195`:
  inherited cutoff is the last tagged end-seed marker when applicable; usage-bearing
  attempt and compaction records matter even though they are not dialogue. Final
  stream usage is used when promoted top-level usage is absent.
- Reference UI mapping from read-only scout: `renderer/dashboard.js` and
  `homeOverview.js` for totals/model ranking/activity/trend; `usageCharts.js` for
  heatmap/trend generation; `sessionRows.js` and `projectRows.js` for session/project
  rows and drilldown. These are Electron rendering references, not code to import.

### Native-source corrections to scout findings

The scout inferred that OMP/Hermes had no recorded costs because their in-tree
reference readers delegate to Tokscale. That claim is not supported and was rejected:

- Installed Hermes `hermes_state.py:8529-8556` explicitly accepts session token
  counters plus `estimated_cost_usd`, `actual_cost_usd`, `cost_status`, `cost_source`
  and `pricing_version`; it distinguishes incremental versus absolute updates.
  `:8758-8777` stores cumulative per-model/route/task data in `session_model_usage`.
  Schema presence must be checked read-only for each source. Do not sum session
  and model aggregates twice or invent daily timestamps from `last_seen`.
- Installed OMP `src/session/agent-session.ts:3341-3347` consumes
  `assistantMsg.usage.cost.total`; `session-manager.ts:356-366` sums input/output/
  cache/total/cost. `:3368-3380` explicitly zeros inherited fork monetary usage but
  retains inherited token counts for context. Therefore billing-token provenance
  requires native lineage evidence; zero cost alone is not proof of zero usage.
- Native recorded cost may itself be an estimate. Preserve its provenance rather
  than advertising it as actual billed spend. Proposed fallback is explicit local
  model/provider pricing; missing rate/components remain unknown, never `$0`.

### Parallel work and Git proof

- Global smol route is `hgemini/gemini-3.8-flash-high:medium`; both GitBootstrap
  (sonic) and UsageReferenceMap (scout) reported Gemini 3.8 execution. No global
  routing settings were changed. Main checked the risky accounting conclusions.
- `git init -b main` completed; main verified repository root and branch.
  `git check-ignore -v` verified sidecar/virtualenv/bytecode/.env/runtime/local config
  exclusions, and non-matching checks kept source/tests/pyproject and `.env.example`
  eligible. No files were staged, committed, pushed, or removed.
- Product code is unchanged during this planning pass. The earlier 45-test result
  is historical; it does not validate the not-yet-implemented analytics views.

## Independent native analytics implementation and verification (2026-09-21)

The approved expansion is implemented. Runtime source discovery/registration no
longer opens Token Monitor or `sidecar`. Only the removed-source configuration
diagnostic and transactional legacy-index migration retain the Token Monitor name.
README and design now describe schema 2, the three views and accounting scopes.

### Native accounting evidence

- Hermes `agent/usage_pricing.py:61-89` distinguishes actual/estimated/included/
  unknown provenance; its input excludes cache. `hermes_state.py:8811-8837` records
  auxiliary model tasks outside main session counters. Main model rows plus session
  residual and independent auxiliary rows therefore form disjoint coarse facts.
- OMP `pi-catalog/src/types.ts:124-156` defines inclusive total and reasoning subset;
  `src/models.ts:150-156` computes catalog-rate costs. Those costs are labeled
  estimated. Provider orchestration remains in reported total, with a warning when
  conversation components do not cover it. Parent IDs exclude inherited charges;
  delegated task rollups do not repeat child charges.
- Installed DSH `dsh-llm/lib/types/types.d.ts:129-149` defines disjoint input/cache
  components and optional total. `dsh-compaction-basic/lib/index.js:605-619` emits
  direct usage/model/provider; `dsh-agent-loop/lib/index.js:1065-1086` attempts can
  expose only a stream, without a model route. Unknown routes are not guessed.
- Codex's native `TokenUsageInfo.fill_to_context_window()` in
  <https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs> sets
  synthetic context capacity with zero components. Native compaction in
  <https://github.com/openai/codex/blob/main/codex-rs/core/src/compact.rs> uses this
  after a context-limit failure. Review identified a false 200,000-token charge;
  a regression first produced `[200000, 60, 200000, 60]`, then passed with `[60, 60]`.
  Markers update the delta baseline but emit no charge; a state DB fallback cannot
  reintroduce an explicitly excluded marker.
- Inspected Codex `thread_items` has no `id` column. Ordering now uses native
  `rollout_ordinal`, `turn_id`, `item_id`; the ingestion fixture uses that shape.

### Automated and production-path checks

- Final `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -q`:
  **76 tests passed** in 8.854 s, with no resource warnings. Migration rollback,
  native preservation, cumulative/replayed/inherited accounting, pricing precedence,
  unknown/zero coverage, local dates, project identity and UI navigation are covered.
  Existing exact-resume and terminal-restoration tests still pass.
- `task.py validate .trellis/tasks/09-21-session-hub`: both context files valid.
  Its branch warning is expected for the initialized but still uncommitted `main`.
- SQLite cancellation regression exposed a native `SQLITE_ERROR` during FTS virtual
  table setup, not `SQLITE_INTERRUPT`; the old catch-all fallback silently continued
  with a literal scan. Only a missing optional FTS table now permits fallback. The
  original cancellation scenario propagates and the literal fallback regression
  still passes. Migration test SQLite handles were also closed explicitly.
- Two independent read-only reviewers checked JSONL accounting and app/store/UI.
  The Codex capacity finding above was reproduced and fixed; the app/store review
  reported no important/critical defects. Main integrated native Hermes/OMP/DSH
  review and all validation; workers did not run competing full test suites.
- A throwaway production-adapter scenario verified Hermes **180 tokens / $2.20
  estimated**, OMP **65 child-owned tokens**, DSH **130 attempt/compaction tokens**,
  with inherited prose retained and duplicate/delegated charges excluded.
- A separate production `Indexer → Store` smoke used two synthetic native OMP
  sessions across 21 days: **74,340 tokens**, **$0.462 estimated**, 42 usage records,
  two model/provider groups and two projects. Dashboard, model, project and daily
  totals agreed. A repeat scan was identical and source SHA-256 hashes were unchanged.
  An audit guard rejected any Token Monitor/sidecar file or SQLite access: none occurred.
- Aggregate-only default-native discovery used a separate temporary index and the
  same access guard: **1,207 sessions**, **19,032 facts**, **19,014 facts with known
  token totals**, **679 sessions without known usage**, **3,516,926,167 observed
  tokens**, zero source errors, **36.63 s**. Tools: Codex, Claude, Hermes, OMP, DSH,
  direct Cursor IDE. This is a point-in-time observed subtotal, not complete billing.
  No conversation bodies, credentials or model prompts were printed/sent.

### Actual terminal checks and boundaries

- Supervised actual CLI at **140×44** displayed total/model shares/21-day heatmap/
  trend; F3 project totals were 48,720 and 25,620. Enter opened exactly one matching
  session showing 48,720; F1 retained that project scope. Keyboard time filtering
  reduced it to the observed recent 320-token call. Ctrl+Q exited with code 0.
- Supervised **80×24** exposed long paths crowding out project usage; project names
  now have a bounded first column, with full path separately scrollable. Session
  list/preview stack below 100 columns; dashboard remains vertically scrollable.
  Long selected project paths also expanded the automatic filter bar to 11 rows,
  leaving zero visible session-content rows at 80×24. The regression failed before
  fixing filter selects to height 3; afterward the session table has height 6 and
  preview height 7. This scenario is now part of the project-drilldown regression.
- Native-resume implementations were not redesigned. This expansion reran existing
  exact-target, confirmation and PTY-restoration checks; earlier isolated real
  OMP/Hermes/DSH launches remain the live-CLI evidence, not a fresh claim that missing
  Claude transcripts or every historical format can be restored.
- Missing prices/components, undated Hermes counters, unknown DSH attempt routes
  and ambiguous parent/fork ownership remain explicit limitations. No new providers,
  remote pricing, subscription/quota collection, vectors or cloud search were added.
- No staging, commit or push. Task archival/journal auto-commit is intentionally
  deferred while product changes remain uncommitted. Owned temporary data/scripts
  and supervised smoke processes are removed/stopped after verification.

## OMP-style bilingual redesign verification (2026-09-21)

- Implemented compact session-first navigation, neutral dividers and one accent,
  contextual shortcuts, full-height narrow list/reader transitions, searchable
  filter drafts with Apply/Cancel/Reset, metadata disclosure and in-place project
  pagination. Project rows retain native token/cost/count/share facts. No parser,
  pricing policy, index schema or source-history changes belong to this redesign.
- Chinese/English preferences apply without remounting or translating source text;
  atomic owner-only replacement persists the explicit choice. Regression coverage
  includes malformed preferences, failed replacement and symlink destination safety.
- Final regression command:
  `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -q`
  passed **82 tests**. Task JSONL validation passed; its branch warning reflects the
  repository's still-uncommitted initial branch, not a failed context reference.
  No separate formatter, linter or type-check command is configured in pyproject.
- Behavioral UI cases cover language changes while querying/reading and receiving
  background results, selection/scroll preservation, long-path filter identity,
  205-session project loading through the final page, live selected-project refresh,
  safe cancellation and keyboard-confirmed acceptance. Existing resume and PTY
  lifecycle tests remain included.
- Independent state and localization reviews exposed a priority-Enter cancellation
  bug, effective-target confirmation mismatch, stale project selection/detail and
  markup-interpreted tooltips. These were corrected. Runtime keyboard verification
  also exposed the app action guard blocking inherited Tab navigation; the guard
  now restricts only workspace actions. Notifications and source tooltips render
  literally, and confirmation paths wrap instead of clipping away the exact target.
- A throwaway production-app scenario rendered all views/settings/filters at
  **80x24 and 140x45** in both languages and checked preview-scroll preservation
  through language changes. Its exported SVGs were inspected; SVG fallback fonts
  overlapped some CJK glyphs, so actual PTY output was used to verify Chinese text.
- Supervised actual CLI terminals exercised English and Chinese navigation,
  settings selection/application, persisted language on restart, search and narrow
  reading/back, filters, wide project expansion/session details and the dashboard.
  Only synthetic native OMP histories were loaded (`discover_defaults=false`).
  Their observed aggregate was **77,400 tokens / $0.0720 estimated**, three sessions;
  these are fixture values, not the user's usage or billing figures.
- The actual 80x24 confirmation showed the complete prepared transcript path,
  working directory and profile. Tab/Enter launched an isolated executable fixture
  through the real foreground handoff. Captured argv contained the exact OMP
  `--resume` path, `--cwd`, `--session-dir` and `--profile default`; captured cwd
  matched, and stdin/stdout/stderr were all TTYs. Exiting the child restored the
  browser; settings and further navigation remained usable. No model prompt was sent.
- This redesign did not freshly validate every installed native CLI or OS clipboard
  transfer. Fixture handoff and existing lifecycle tests are not claims of a new
  live-provider validation. Owned supervised processes exited with code 0 and
  temporary scripts, screenshots, histories and index were removed after inspection.
- README and task artifacts reflect the approved behavior. No staging, commit,
  push or auto-committing task archival/journal operation was performed.
