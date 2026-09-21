# Local AI Session Hub

## Goal

Give the user one independent local terminal application to understand AI usage through dashboard, session and project views, search their user/assistant history, and continue a selected conversation in its original tool.

## Confirmed requirements

- The user requested browsing and searching all locally available AI conversations, with tool-specific resume.
- The user approved creating an independent project under `/Users/pugge/Desktop/person`; the project is `/Users/pugge/Desktop/person/ai-session-hub`.
- The browser/search/resume milestone, compatibility repair and approved independent native-usage/dashboard expansion are implemented.
- The user's new request supersedes the original Token Monitor catalog decision: no runtime dependency on Token Monitor, its databases, exports, background process, or `sidecar` reference checkout.
- The user requested a dashboard, token usage in the session view, and a project view, using the supplied screenshots and `sidecar/token-monitor` as reference.
- The user explicitly authorized initializing this project as a Git repository, excluding `sidecar`; no commit or push was requested.
- The user requested parallel Gemini 3.8 subagents for simple independent tasks. Main owns accounting correctness, cross-layer integration and verification; global model settings must not be changed.

## Evidence

See `research.md` for the inspected application version, archive schema, local source presence, and CLI help evidence. Archive counts are a point-in-time observation, not a completeness claim.

## Requirements

- R1: Provide a keyboard-driven session list with tool, title, project, last activity, and available actions.
- R2: Filter by tool, project, and time; search both user and assistant text with matching excerpts and conversation context. Chinese and English queries must work.
- R3: Read source records locally and maintain a separate incremental search index. Browsing and indexing must not mutate original conversations or send their content to a service.
- R4: Retain catalog entries when only metadata is available, with an explicit indication that full text is unavailable. Do not describe title-only matching as full-text search.
- R5: Resume a specific conversation using the original tool, native session identifier, working directory, and source profile/home when known. Suspend the TUI during the foreground subprocess and restore it after exit.
- R6: Explain missing executables, missing source files, missing project directories, and unsupported resume mechanisms; do not silently start a new session or resume the most recent unrelated session.
- R7: Distinguish IDE conversations, CLI conversations, subagent records, and archived conversations wherever source metadata supports it; identifiers from one source must not be passed to another without verified compatibility.
- R8: Show scan progress and failures while keeping the interface responsive. Handle partial records from active sessions and malformed individual files without losing other results.
- R9: Provide a dashboard with total tokens, available cost, model ranking/shares, daily activity heatmap, active-day count, and usage trend. Show scope and data coverage explicitly.
- R10: Show per-session tokens, model(s), message count and available cost alongside existing metadata/search/resume; expose input/output/cache/reasoning detail where the native source supports it.
- R11: Provide a project view with project path/name, token and available-cost totals, session count and usage share; opening a project filters its sessions. Do not merge unrelated paths with equal display names.
- R12: Derive usage independently from native Codex, Claude Code, Hermes, OMP and DSH sources. Missing usage or pricing is unknown, not zero. Usage aggregates must not double-count cumulative snapshots, stream mirrors, cached/reasoning subsets or inherited session copies. Conversation-text retention must not imply charging inherited text again. Cost policy: preserve native actual/estimated provenance; otherwise use explicitly configured local model/provider rates and label estimates, with unknown/partial coverage for missing rates.
- R13: Initialize Git and ignore `sidecar`, virtual environments, generated artifacts and local/private configuration. Reference code remains reference-only; source histories/credentials must not be staged.

## Acceptance criteria

- AC1 (R1, R4, R12): Catalog and analytics work with Token Monitor absent. Previously imported Token Monitor-only rows and links are removed from the app-owned index during migration; native conversations remain searchable. Configuration no longer accepts a Token Monitor source. Directly read IDE metadata stays explicitly limited.
- AC2 (R2): A Chinese phrase and an English phrase present only in an assistant reply are searchable and open the correct surrounding conversation.
- AC3 (R3, R8): Appending a turn updates the index without duplicate messages; a malformed or partially written record does not crash the browser; original history files remain unchanged.
- AC4 (R5): For each accepted native-resume adapter, a terminal integration check verifies the exact executable arguments and working directory, foreground handoff, and return to the session browser. Live resume validation remains an explicit implementation validation step.
- AC5 (R6, R7): Unsupported or unavailable resume actions display a reason and never invoke another tool or an unrelated session.
- AC6 (R1, R8): Search, selection, preview scrolling, and cancellation work via keyboard on the user's macOS terminal during background indexing.
- AC7 (R9, R12): Dashboard totals, model shares and daily charts agree with native synthetic usage fixtures and the selected time/platform scope; unknown model/date/usage coverage is visible rather than fabricated.
- AC8 (R10, R12): Session rows and detail expose real usage, including correct cumulative-to-delta handling and no duplicate cache/reasoning or inherited billing; refresh/restart do not inflate totals. Existing search and exact native resume remain usable.
- AC9 (R11, R12): Project totals agree with the same accounting facts as the dashboard and session view; same-named distinct paths remain separate and keyboard drilldown selects only the chosen project.
- AC10 (R13): The project root is a Git repository; `git check-ignore` confirms `sidecar` and generated/private artifacts are excluded while source/tests remain eligible. No automatic staging, commit or push.
- AC11 (R8–R11): Actual supervised TUI interaction verifies all three views, narrow-terminal navigation, filters, project drilldown and return to sessions during background scanning; no fabricated screenshot data or model prompts.

## Approved Milestone Scope

The user approved the first milestone scope: a unified discoverable catalog with complete CLI paths first; IDE entries may initially be metadata-only with explicit unavailable-action reasons.

- Expose directly discoverable native conversations and directly supported IDE metadata across configured sources/profiles; historical archive-only platforms are no longer claimed as native coverage.
- Complete full-text search and verified native resume for CLI tools: Codex, Claude Code, Hermes, OMP, and DSH.
- Label IDE and metadata-only entries explicitly with unavailable-action reasons; do not launch unrelated sessions.
- Preserve the user's all-conversation goal through independent native readers, not a third-party application's collected archive.
- The new requested milestone adds native usage analytics and three connected TUI views. Screenshots are information/layout references, not a request to introduce Electron or a web server.

## Exclusions and evidence limits

Cloud sync, prompt submission, cross-tool conversation migration and native CLI installation remain excluded. Subscription/quota collection requiring provider credentials is not implied by the token-usage screenshots. No costs may be presented as an invoice solely from token counts. Claude cache-only history remains searchable; missing native transcripts/usage are not reconstructed from Token Monitor.

## Artifact status

The independent native-usage expansion is implemented: schema-2 migration, five native accounting adapters, explicit local pricing and three connected Textual views. Regression and actual-terminal evidence is recorded in `research.md`. Git initialization is complete; no staging, commit or push was requested or performed.

## Compatibility correction (2026-09-21)

- Fix non-Codex resume using installed native CLI contracts, including OMP project-bucket sessions misclassified as subagents. Preserve restrictions for genuine secondary sessions.
- Historical repair treated Token Monitor as a catalog rather than a platform. This runtime integration is now explicitly superseded by R12/AC1 and must be removed, not merely hidden in UI.
- Compare independent parsers and accounting behavior with the local `sidecar/token-monitor` reference source; distinguish usage, full-text and resume capabilities.
- Measure local search latency, remove avoidable work, and assess SQLite versus optional semantic/vector search. Preserve literal Chinese/English matching and local-only indexing.
- Verify terminal interaction and native resume with no unsolicited prompts; report CLI/environment blockers rather than claiming all platforms passed.

## OMP-style interface redesign — approved and implemented

The user approved the redesign before implementation. It preserves R1–R13 and
AC1–AC11; this section supersedes earlier layout descriptions, not accounting rules.

- R14: Replace oversized bilingual navigation, colorful nested borders and emoji
  with a compact terminal-native visual hierarchy: one accent, muted metadata,
  thin separators, explicit keyboard focus and ordinary text/line glyphs. No
  Nerd Font requirement. Source conversation text is not stripped of emoji.
- R15: Add a settings dialog with Chinese/English interface selection, immediate
  application and restart persistence. Translate application-owned navigation,
  filters, headings, status, help and confirmation/reason labels consistently;
  do not translate source dialogue, paths, IDs, model names or raw diagnostics.
- R16: Make session browsing the initial view. Keep dashboard and project
  views reachable with existing F1/F2/F3 bindings. Collapse inactive filters into
  a compact scope summary; provide a searchable filter dialog and explicit reset.
  Display short project names plus disambiguating paths, not wrapped path slugs.
- R17: Prioritize session title and dialogue over dense metadata. Keep time visible,
  expose full metadata/usage on demand, preserve search/selection/preview position
  on language/view changes, and show contextual shortcuts. Narrow layouts use
  list/detail navigation rather than two unusably short stacked panels.
- R18: Preserve in-place project expansion and exact session selection; provide
  explicit incremental loading beyond the current 200-child limit. Filtering
  and scope labels must agree with the data shown in each view. No accounting
  algorithm change is included.

Acceptance additions:
- AC12 (R14, R16, R17): At 80x24 and 140x45 the actual terminal surface has usable
  navigation/search/list/detail, visible focus and no app-owned emoji or duplicate
  bilingual labels. Unfocused borders do not compete with selection.
- AC13 (R15, R17): Switch both languages while searching or previewing; retain
  the selected native session/query/filter/scroll state and observe the chosen
  language after restart. A failed preferences save is visible, never false success.
- AC14 (R16, R18): Search/select/reset a long-path project filter; browse a synthetic
  project with more than 200 sessions and reach its last session without leaving
  the project view. Escape closes the current overlay before changing focus.
- AC15 (R1–R8, R17): Keyboard search, pagination, preview and confirmed native-tool
  handoff remain usable; no typing shortcut starts resume or quits unexpectedly.

Boundary: redesign UI/presentation and local preferences only; preserve source
readers, usage facts, pricing policy, index schema and exact resume validation.
Approved trade-offs: session-first startup replaces dashboard-first; filter dialogs
reduce clutter but add one action; narrow detail is a separate surface with Escape
back. Terminal verification and remaining clipboard/live-native limits are recorded
in `research.md`.
