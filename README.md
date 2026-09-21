<p align="right">
   <strong>English</strong> | <a href="./README.zh-CN.md">简体中文</a>
</p>

<div align="center">
  <h1>AI Session Hub</h1>
  <p><strong>A unified local terminal workspace to search, analyze, and resume all your AI conversations.</strong></p>
  <p><em>Browse history, inspect token analytics, and take off where you left off across every AI coding tool.</em></p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.13%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.13+" />
    <img src="https://img.shields.io/badge/UI-Textual_8.2-10b981?style=flat-square" alt="Textual 8.2" />
    <img src="https://img.shields.io/badge/Search-SQLite_FTS5_Trigram-003B57?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite FTS5 Trigram" />
    <img src="https://img.shields.io/badge/Privacy-100%25_Local_&_Offline-22c55e?style=flat-square" alt="100% Local" />
    <img src="https://img.shields.io/badge/Platform-macOS_%7C_Linux-0A84FF?style=flat-square" alt="Platform" />
    <img src="https://img.shields.io/badge/License-MIT-purple?style=flat-square" alt="MIT License" />
  </p>
</div>

```text
┌─ Session Hub ────────────────────────────────────────────────────────────────────────────────────────┐
│ Sessions [F2]   Projects [F3]   Dashboard [F1]                          Filters [f]   Settings [,]   │
│ Scope: All tools · All time                                                                          │
├──────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Time         Tool    Title                                  │ Conversation                           │
│ 09-21 14:32  codex   Refactor database connection pool      │ Assistant -                            │
│ 09-21 11:15  omp     Implement session indexer worker       │ To optimize query latency without      │
│ 09-20 18:40  claude  Fix streaming response timeout         │ blocking the UI, we utilize background │
│ 09-19 09:20  hermes  Add compression tip link handling      │ worker threads with transactional read │
│ 09-18 16:05  dsh     Inspect token usage events             │ snapshots...                           │
├──────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 1–5 / 1,388 sessions · 5.56B tokens · $1,602.70 estimated   │ Details [i]   Resume [r]   Help [?]    │
└──────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## What is AI Session Hub?

**AI Session Hub** is an open-source, keyboard-driven Terminal User Interface (TUI) built for developers who work across multiple AI programming assistants. 

Instead of juggling fragmented histories across disparate directory structures, CLI configurations, and proprietary storage formats, AI Session Hub reads your local AI conversation artifacts in real-time, builds a local high-performance SQLite search index, provides comprehensive multi-dimensional usage dashboards, and enables seamless one-key resume back into each tool's native environment.

**100% Local & Privacy-First**: All conversation scanning, token accounting, and search indexing happen strictly on your machine using read-only database connections. No credentials, telemetry, or network prompts are ever transmitted.

---

## Supported Tools & Capabilities

AI Session Hub supports native transcript parsing, token accounting, and verified CLI handoff across all major AI development tools:

| Tool | Native Storage | Full-Text Search | Usage & Cost | Native Resume | Verification Status |
|:-----|:---------------|:----------------:|:------------:|:-------------:|:--------------------|
| **Codex** | `~/.codex/sessions/`, `state_*.sqlite` | ✅ | ✅ | ✅ | Full history & rollout projection sync |
| **Claude Code** | `~/.claude/projects/`, `transcripts/` | ✅ | ✅ | ✅ | Native UUID resume, subagent detection |
| **Hermes Agent** | `~/.hermes/state.db` | ✅ | ✅ | ✅ | Compression tip continuation & profile tracking |
| **Oh My Pi (OMP)** | `~/.omp/agent/sessions/` | ✅ | ✅ | ✅ | Full transcript JSONL & profile isolation |
| **DeepSeek Harness (DSH)** | `~/.dsh/sessions/` | ✅ | ✅ | ✅ | Compressed streaming `.jsonl.zstd` reader |
| **Cursor IDE** | `~/Library/Application Support/Cursor/` | Metadata | — | — | Metadata discovery & project linking |

---

## Key Features

### 📊 Comprehensive Usage & Analytics Dashboard
* **8 Core KPI Cards**: Instant visibility into Total Tokens (with coverage metrics), Known Cost (estimated/recorded), Active Days count, Consecutive Day Streak, Peak Single-Day Volume, Most Used Model, Message Count, and Stored Sessions.
* **Contribution Calendar Heatmap**: A complete 7-day contribution grid displaying activity across the entire year with multi-level intensity shading.
* **Two-Column Distribution Breakdown**: 
  * **By Model**: Model name, visual progress bar (`████████░░`), token volume, percentage share, and estimated cost.
  * **By Tool**: Client tool, visual progress bar, token count, share, and session count.
* **High-Resolution Vertical Trend Chart**: Multi-line terminal bar chart with Y-axis token scale (`316.6M ┤`) and X-axis calendar dates, replacing flat single-line sparklines.
* **Daily Activity Breakdown**: Chronological table showing daily tokens, visual trend bars, cost, and proportion.

### 🔍 Lightning-Fast Full-Text Search
* **Unicode NFKC Literal Matching**: Query user questions and assistant replies with case-insensitive, full-substring precision.
* **SQLite FTS5 Trigram Acceleration**: Sub-millisecond search across tens of thousands of turns with graceful literal scan fallbacks.
* **Zero Tokenization Loss**: CJK (Chinese, Japanese, Korean) phrases, code snippets, punctuation, and SQL fragments match verbatim without parser mangling.

### 🚀 Safe Zero-Prompt Native Resume
* **Strict Verification Gate**: Before handoff, the hub validates the target executable on `PATH`, the exact working directory, source file existence, and profile environment.
* **Explicit Confirmation Dialog**: Displays the exact command arguments, working directory, and effective profile with Cancel as the initial focus.
* **Clean Terminal Suspension**: The TUI cleanly restores cooked terminal mode, yields control to the native CLI, reaps child processes safely, and re-indexes upon return.
* **No Accidental Restarts**: Incompatible versions, secondary subagent records, or missing directories fail visibly with actionable reasons rather than launching unrelated sessions.

### 📁 Structured Project Explorer
* **Fixed-Width Aligned Columns**: Monospace alignment for Project Name, Token Count, Cost, Session Count, and Usage Share across English and CJK project paths.
* **In-Place Tree Expansion**: Browse sessions directly inside their project folder without context jumping.
* **Infinite Project Pagination**: Smooth `Load more…` pagination for workspaces exceeding 200 sessions.
* **Tilde Path Shortening**: Automatic `~` home directory abbreviation prevents text wrapping.

### 🌐 Bilingual & Minimalist TUI Aesthetic
* **OMP-Inspired Design**: Neutral dark palette, subtle dividers, single accent color, and high-contrast typography.
* **Live Language Switch**: Instant runtime toggle between English and 简体中文 (`,`) with atomic persistence in `ui-preferences.json`.
* **Adaptive Terminal Layout**: Automatically refactors between side-by-side split panels and full-height stacked reader views on narrow (80x24) displays.

---

## Installation & Quick Start

### Prerequisites
* Python **3.13** or higher
* macOS or Linux with a POSIX terminal (supports Ghostty, iTerm2, Alacritty, Kitty, WezTerm, Terminal.app)

### Quick Setup

```bash
# 1. Clone the repository
git clone https://github.com/suzy56/ai-session-hub.git
cd ai-session-hub

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies in editable mode
pip install -e .

# 4. Launch AI Session Hub
ai-session-hub
```

Or run directly without installing to path:
```bash
.venv/bin/python -m ai_session_hub
```

---

## Configuration

By default, AI Session Hub scans standard paths for all supported tools automatically. You can provide a custom `config.toml` via `--config`:

```bash
ai-session-hub --config /path/to/config.toml --data-dir ~/.local/share/ai-session-hub
```

### Example `config.toml`

```toml
# Set to false to disable default ambient discovery and only scan explicit sources
discover_defaults = true

# Custom OMP profile source
[[sources]]
tool = "omp"
root = "/Users/username/.omp/profiles/work/agent"

# Custom Codex source
[[sources]]
tool = "codex"
root = "/Users/username/.codex"
profile = "default"

# Optional local model pricing rules (USD per million tokens)
# Used for cost estimation when native tool records lack cost fields
[[pricing]]
model = "gpt-5.5"
input_per_million = 2.0
output_per_million = 8.0
cache_read_per_million = 0.5
cache_write_per_million = 2.5

[[pricing]]
model = "claude-3-7-sonnet"
provider = "anthropic"
input_per_million = 3.0
output_per_million = 15.0
cache_read_per_million = 0.3
cache_write_per_million = 3.75
```

---

## Keyboard Shortcuts

| Key | Action | Context |
|:---:|:-------|:--------|
| `F1` | Switch to **Dashboard** view | Global |
| `F2` | Switch to **Sessions** view | Global |
| `F3` | Switch to **Projects** view | Global |
| `/` | Focus search input field | Sessions view |
| `f` | Open **Filters** dialog (Tool / Project / Time / Kind) | Global |
| `,` | Open **Settings** dialog (Language switch) | Global |
| `r` | Request **Native Resume** for selected session | Table / Tree |
| `i` | Show full session **Details** modal (Tokens / Provenance) | Table / Tree |
| `Enter` | Read selected conversation / Expand project folder | Table / Tree |
| `[` / `]` | Previous / Next page (Sessions) or Overview / Trend tab (Dashboard) | View |
| `PageUp` / `PageDown` | Scroll conversation preview | Preview |
| `Esc` | Close overlay dialog / Return from preview to table | Global |
| `Ctrl+R` | Refresh index manually | Global |
| `q` | Quit application (when not typing in search) | Global |
| `Ctrl+Q` | Force quit application | Global |
| `?` | Contextual help | Global |

---

## Architecture & Security

```text
┌────────────────────────────────────────────────────────┐
│                   AI Coding Tools                      │
│   Codex  ·  Claude  ·  Hermes  ·  OMP  ·  DSH  ·  ...  │
└──────────────────────────┬─────────────────────────────┘
                           │ Read-Only WAL / JSONL Streams
                           ▼
┌────────────────────────────────────────────────────────┐
│                   ai-session-hub                       │
│  ┌────────────────────┐      ┌──────────────────────┐  │
│  │ Incremental Indexer│ ───▶ │ SQLite FTS5 Storage  │  │
│  └────────────────────┘      └──────────────────────┘  │
│            │                             │             │
│            ▼                             ▼             │
│  ┌────────────────────┐      ┌──────────────────────┐  │
│  │ Resume Dispatcher  │      │ Textual TUI Engine   │  │
│  └────────────────────┘      └──────────────────────┘  │
└────────────┬─────────────────────────────┬─────────────┘
             │ Executable PTY Handoff      │ Interactive UI
             ▼                             ▼
   [Native CLI Session]             [Terminal Screen]
```

* **Read-Only Guarantees**: Native SQLite databases are opened with URI `?mode=ro` and `PRAGMA query_only=ON`. JSONL and `.zstd` streams are read with bounded buffers.
* **Isolated Storage**: The application index is stored strictly in `~/Library/Application Support/ai-session-hub/` (macOS) or `~/.local/share/ai-session-hub/` (Linux) with owner-only (`0700`) permissions. It never writes to source project repositories.
* **Safe Subprocess Lifecycle**: The native tool execution uses `subprocess.Popen` with terminal inheritance without `shell=True`. Environment variables are scrubbed of conflicting profile flags.

---

## Running Tests

The test suite includes 85+ automated integration tests covering transcript ingestion, FTS search, Unicode normalization, project aggregation, PTY terminal lifecycle, and UI responsiveness:

```bash
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -q
```

## Acknowledgments

* Inspired by the design and analytics concepts of [Token Monitor](https://github.com/Javis603/token-monitor).
* Thanks to the [Linux.do](https://linux.do/) community for project promotion and feedback.

---

## License

Released under the [MIT License](LICENSE).
