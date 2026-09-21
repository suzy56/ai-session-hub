from __future__ import annotations

import json
import os
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ai_session_hub.models import (
    MessageRecord,
    SessionRef,
    SessionSnapshot,
    SourceSpec,
    UsageRecord,
)
from ai_session_hub.source_io import open_ro_sqlite
from ai_session_hub.usage import AnalyticsSnapshot, DailyUsage, PricingRule, UsageGroup, UsageSummary


def normalize_text(text: str) -> str:
    """Normalize text using Unicode NFKC normalization and casefolding for literal search."""
    if not text:
        return ""
    return unicodedata.normalize("NFKC", text).casefold()


@dataclass(frozen=True)
class SearchFilters:
    tool: str | None = None
    project: str | None = None
    time_bound_ms: int | None = None
    kind: str | None = None
    project_key: str | None = None


@dataclass(frozen=True)
class SearchResult:
    session_key: str
    client: str
    title: str
    project_label: str | None
    cwd: str | None
    updated_ms: int | None
    text_state: str
    match_kind: Literal["body", "metadata", "none"]
    matched_message_id: str | None
    excerpt: str | None
    warnings: tuple[str, ...]
    kind: str = "conversation"
    archived: bool = False


@dataclass(frozen=True)
class SearchPage:
    results: tuple[SearchResult, ...]
    total_count: int
    offset: int
    limit: int


class Store:
    """Application SQLite store for session catalog, messages, and trigram FTS."""

    def __init__(self, db_path: Path, *, read_only: bool = False):
        self.db_path = db_path.resolve()
        if read_only:
            self.conn = open_ro_sqlite(self.db_path)
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version != 2:
                self.conn.close()
                raise RuntimeError(f"Unsupported index schema version {version}; please update ai-session-hub")
            self.fts_available = self._check_fts_available(self.conn.cursor())
            return

        # Ensure directory exists with owner-only permissions (0700)
        parent = self.db_path.parent
        old_umask = os.umask(0o077)
        try:
            parent.mkdir(parents=True, exist_ok=True)
            parent.chmod(0o700)
        finally:
            os.umask(old_umask)

        self.conn = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row

        # Set owner-only permissions on database file
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

        self._init_db()

    def _init_db(self) -> None:
        c = self.conn.cursor()
        c.execute("PRAGMA journal_mode = WAL;")
        c.execute("PRAGMA foreign_keys = ON;")
        c.execute("PRAGMA busy_timeout = 5000;")

        version = c.execute("PRAGMA user_version;").fetchone()[0]
        if version > 2:
            self.conn.close()
            raise RuntimeError(f"Unsupported index schema version {version}; please update ai-session-hub")

        if version < 2:
            try:
                c.execute("BEGIN IMMEDIATE")
                if version == 0:
                    self._create_schema(c)
                else:
                    self._migrate_v1(c)
                self._create_usage_schema(c)
                c.execute("PRAGMA user_version = 2")
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                self.conn.close()
                raise

        # Check FTS5 availability
        self.fts_available = self._check_fts_available(c)

    def _check_fts_available(self, c: sqlite3.Cursor) -> bool:
        try:
            c.execute("SELECT 1 FROM message_fts LIMIT 1;")
            return True
        except sqlite3.OperationalError:
            return False

    def _create_schema(self, c: sqlite3.Cursor) -> None:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                key TEXT PRIMARY KEY,
                tool TEXT NOT NULL,
                source_root TEXT NOT NULL,
                profile TEXT,
                sessions_dir TEXT,
                client TEXT NOT NULL,
                native_id TEXT NOT NULL,
                title TEXT NOT NULL,
                title_norm TEXT NOT NULL,
                project_id TEXT,
                project_label TEXT,
                project_norm TEXT,
                cwd TEXT,
                started_ms INTEGER,
                updated_ms INTEGER,
                kind TEXT NOT NULL,
                archived INTEGER NOT NULL,
                text_state TEXT NOT NULL,
                indexed_at INTEGER NOT NULL,
                revision TEXT NOT NULL,
                locators TEXT NOT NULL,
                warnings TEXT NOT NULL,
                project_key TEXT NOT NULL DEFAULT ''
            );
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_ms DESC);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_tool ON sessions(tool);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_key);")

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT NOT NULL REFERENCES sessions(key) ON DELETE CASCADE,
                source_id TEXT NOT NULL,
                parent_id TEXT,
                ordinal INTEGER NOT NULL,
                role TEXT NOT NULL,
                body TEXT NOT NULL,
                body_norm TEXT NOT NULL,
                timestamp_ms INTEGER,
                flags TEXT NOT NULL,
                UNIQUE(session_key, source_id)
            );
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_ordinal ON messages(session_key, ordinal);")

        # FTS5 Trigram table
        try:
            c.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
                    body_norm,
                    content='messages',
                    content_rowid='id',
                    tokenize='trigram'
                );
                """
            )
            # Triggers for FTS5 synchronization
            c.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO message_fts(rowid, body_norm) VALUES (new.id, new.body_norm);
                END;
                """
            )
            c.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO message_fts(message_fts, rowid, body_norm) VALUES ('delete', old.id, old.body_norm);
                END;
                """
            )
            c.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
                    INSERT INTO message_fts(message_fts, rowid, body_norm) VALUES ('delete', old.id, old.body_norm);
                    INSERT INTO message_fts(rowid, body_norm) VALUES (new.id, new.body_norm);
                END;
                """
            )
        except sqlite3.OperationalError:
            pass

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS source_status (
                source_key TEXT PRIMARY KEY,
                tool TEXT NOT NULL,
                root TEXT NOT NULL,
                profile TEXT,
                last_scanned_at INTEGER,
                status TEXT NOT NULL,
                error TEXT
            );
            """
        )

    @staticmethod
    def _project_key(cwd: Path | None, project_id: str | None) -> str:
        # Never anchor a recorded relative path to the indexer's own directory.
        if cwd is not None and cwd.is_absolute():
            return str(cwd.resolve())
        return project_id or ""

    def _migrate_v1(self, c: sqlite3.Cursor) -> None:
        # This entire cutover shares the caller's explicit DDL transaction.
        c.execute("UPDATE sessions SET alias_of = NULL")
        c.execute("DELETE FROM sessions WHERE tool = 'token-monitor'")
        c.execute("DELETE FROM source_status WHERE tool = 'token-monitor'")
        c.execute("DROP TABLE session_aliases")
        c.execute("DROP INDEX IF EXISTS idx_sessions_alias_of")
        c.execute("ALTER TABLE sessions DROP COLUMN alias_of")
        c.execute("ALTER TABLE sessions ADD COLUMN project_key TEXT NOT NULL DEFAULT ''")
        rows = c.execute("SELECT key, cwd, project_id FROM sessions").fetchall()
        c.executemany(
            "UPDATE sessions SET project_key = ?, revision = '' WHERE key = ?",
            ((self._project_key(Path(r["cwd"]) if r["cwd"] else None, r["project_id"]), r["key"])
             for r in rows),
        )
        c.execute("CREATE INDEX idx_sessions_project ON sessions(project_key)")

    @staticmethod
    def _create_usage_schema(c: sqlite3.Cursor) -> None:
        c.execute("""CREATE TABLE usage_records (
            session_key TEXT NOT NULL REFERENCES sessions(key) ON DELETE CASCADE,
            source_id TEXT NOT NULL,
            timestamp_ms INTEGER,
            model TEXT,
            provider TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            reasoning_tokens INTEGER,
            total_tokens INTEGER,
            cost_usd REAL,
            cost_kind TEXT NOT NULL,
            granularity TEXT NOT NULL,
            PRIMARY KEY (session_key, source_id)
        )""")
        c.execute("CREATE INDEX idx_usage_time ON usage_records(timestamp_ms)")
        c.execute("CREATE INDEX idx_usage_model ON usage_records(provider, model)")

    @staticmethod
    def _session_filter(filters: SearchFilters, *, activity_time: bool = False) -> tuple[list[str], list[object]]:
        clauses = ["1 = 1"]
        params: list[object] = []
        if filters.tool:
            clauses.append("s.client = ?")
            params.append(filters.tool)
        if filters.project:
            clauses.append("(s.project_label = ? OR s.project_id = ?)")
            params.extend((filters.project, filters.project))
        if filters.project_key is not None:
            clauses.append("s.project_key = ?")
            params.append(filters.project_key)
        if activity_time and filters.time_bound_ms is not None:
            clauses.append("s.updated_ms >= ?")
            params.append(filters.time_bound_ms)
        if filters.kind:
            clauses.append("s.text_state = ?" if filters.kind == "metadata-only" else "s.kind = ?")
            params.append(filters.kind)
        return clauses, params

    def _usage_query(
        self, filters: SearchFilters, pricing: tuple[PricingRule, ...], keys: list[str] | None = None
    ) -> tuple[str, list[object]]:
        """One SQL accounting path for every aggregate, including page summaries."""
        clauses, params = self._session_filter(filters)
        if keys is not None:
            clauses.append("s.key IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(keys))
        pairs = self.conn.execute(
            "SELECT DISTINCT model, provider FROM usage_records WHERE model IS NOT NULL"
        ).fetchall()
        rates_data: list[dict[str, Any]] = []
        for m, p in pairs:
            best_rule = None
            for r in pricing:
                if r.model.lower() == m.lower():
                    if r.provider and p and r.provider.lower() == p.lower():
                        best_rule = r
                        break
                    elif r.provider is None and best_rule is None:
                        best_rule = r
            if best_rule:
                rates_data.append({
                    "m": m,
                    "p": p,
                    "in": best_rule.input_per_million,
                    "out": best_rule.output_per_million,
                    "rd": best_rule.cache_read_per_million,
                    "wr": best_rule.cache_write_per_million,
                })
        rule_params = [json.dumps(rates_data)]
        rates_sql = """
            SELECT 
                json_extract(value, '$.m') AS model,
                json_extract(value, '$.p') AS provider,
                CAST(json_extract(value, '$.in') AS REAL) AS input_rate,
                CAST(json_extract(value, '$.out') AS REAL) AS output_rate,
                CAST(json_extract(value, '$.rd') AS REAL) AS read_rate,
                CAST(json_extract(value, '$.wr') AS REAL) AS write_rate
            FROM json_each(?)
        """
        bound = "1 = 1"
        if filters.time_bound_ms is not None:
            bound = "granularity = 'event' AND timestamp_ms >= ?"
            params.append(filters.time_bound_ms)
        sql = f"""WITH
            rates(model, provider, input_rate, output_rate, read_rate, write_rate) AS ({rates_sql}),
            eligible AS MATERIALIZED (
                SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_key = s.key) AS message_count
                FROM sessions s WHERE {' AND '.join(clauses)}
            ),
            calculated AS MATERIALIZED (
                SELECT u.*,
                    COALESCE(u.total_tokens, u.input_tokens + u.output_tokens) AS observed_total,
                    CASE WHEN u.input_tokens IS NOT NULL AND u.output_tokens IS NOT NULL
                        AND u.cache_read_tokens IS NOT NULL AND u.cache_write_tokens IS NOT NULL
                        AND u.input_tokens >= u.cache_read_tokens + u.cache_write_tokens
                        AND (u.input_tokens = u.cache_read_tokens + u.cache_write_tokens OR r.input_rate IS NOT NULL)
                        AND (u.output_tokens = 0 OR r.output_rate IS NOT NULL)
                        AND (u.cache_read_tokens = 0 OR r.read_rate IS NOT NULL)
                        AND (u.cache_write_tokens = 0 OR r.write_rate IS NOT NULL)
                    THEN ((u.input_tokens - u.cache_read_tokens - u.cache_write_tokens) * COALESCE(r.input_rate, 0)
                        + u.output_tokens * COALESCE(r.output_rate, 0)
                        + u.cache_read_tokens * COALESCE(r.read_rate, 0)
                        + u.cache_write_tokens * COALESCE(r.write_rate, 0)) / 1000000.0
                    END AS estimated_cost
                FROM eligible s JOIN usage_records u ON u.session_key = s.key
                LEFT JOIN rates r ON r.model = u.model AND (r.provider = u.provider OR (r.provider IS NULL AND u.provider IS NULL))
            ),
            facts AS MATERIALIZED (
                SELECT *, COALESCE(cost_usd, estimated_cost) AS effective_cost,
                    CASE WHEN cost_usd IS NOT NULL THEN cost_kind = 'estimated'
                         ELSE estimated_cost IS NOT NULL END AS estimated
                FROM calculated WHERE {bound}
            )
        """
        return sql, [*rule_params, *params]

    @staticmethod
    def _usage_summary(row: sqlite3.Row) -> UsageSummary:
        return UsageSummary(
            total_tokens=row["total_tokens"], input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"], cache_read_tokens=row["cache_read_tokens"],
            cache_write_tokens=row["cache_write_tokens"], reasoning_tokens=row["reasoning_tokens"],
            cost_usd=row["cost_usd"], records=row["records"], priced_records=row["priced_records"],
            known_token_records=row["known_token_records"],
            estimated_records=row["estimated_records"] or 0, session_count=row["session_count"],
            unknown_sessions=row["unknown_sessions"], message_count=row["message_count"] or 0,
            models=tuple(sorted(json.loads(row["models"]))),
        )

    def _usage_groups(self, sql: str, params: list[object], dimension: str) -> tuple[UsageGroup, ...]:
        identity, label = {
            "all": ("''", "''"),
            "session": ("s.key", "s.title"),
            "project": ("s.project_key", "COALESCE(NULLIF(s.project_label, ''), NULLIF(s.project_key, ''), '未知项目')"),
            "model": ("json_array(f.provider, f.model)", "COALESCE(f.model, '未知模型') || ' / ' || COALESCE(f.provider, '未知 Provider')"),
            "tool": ("s.client", "s.client"),
        }[dimension]
        rows = self.conn.execute(sql + f""",
            joined AS MATERIALIZED (
                SELECT {identity} AS group_key, {label} AS group_label, s.key AS session_key,
                    s.message_count, f.source_id, f.model, f.observed_total, f.input_tokens,
                    f.output_tokens, f.cache_read_tokens, f.cache_write_tokens, f.reasoning_tokens,
                    f.effective_cost, f.estimated
                FROM eligible s LEFT JOIN facts f ON f.session_key = s.key
            ), members AS (
                SELECT DISTINCT group_key, session_key, message_count FROM joined
            ), message_counts AS (
                SELECT group_key, SUM(message_count) AS message_count FROM members GROUP BY group_key
            )
            SELECT j.group_key, MIN(j.group_label) AS group_label,
                SUM(observed_total) AS total_tokens, SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens,
                SUM(cache_write_tokens) AS cache_write_tokens, SUM(reasoning_tokens) AS reasoning_tokens,
                SUM(effective_cost) AS cost_usd, COUNT(source_id) AS records,
                COUNT(observed_total) AS known_token_records,
                COUNT(effective_cost) AS priced_records, SUM(estimated) AS estimated_records,
                COUNT(DISTINCT session_key) AS session_count,
                COUNT(DISTINCT session_key) - COUNT(DISTINCT CASE WHEN observed_total IS NOT NULL THEN session_key END) AS unknown_sessions,
                MAX(mc.message_count) AS message_count,
                json_group_array(DISTINCT model) FILTER (WHERE model IS NOT NULL) AS models
            FROM joined j JOIN message_counts mc ON mc.group_key = j.group_key
            GROUP BY j.group_key ORDER BY total_tokens DESC NULLS LAST, j.group_key
        """, params).fetchall()
        return tuple(UsageGroup(r["group_key"], r["group_label"], self._usage_summary(r)) for r in rows)

    def session_usage(
        self, keys: list[str], *, pricing: tuple[PricingRule, ...] = ()
    ) -> dict[str, UsageSummary]:
        if not keys:
            return {}
        sql, params = self._usage_query(SearchFilters(), pricing, keys)
        return {group.key: group.summary for group in self._usage_groups(sql, params, "session")}

    def analytics(
        self, filters: SearchFilters, *, pricing: tuple[PricingRule, ...] = ()
    ) -> AnalyticsSnapshot:
        sql, params = self._usage_query(filters, pricing)
        # All panels observe the same committed facts even during background scans.
        self.conn.execute("SAVEPOINT analytics_read")
        try:
            totals = self._usage_groups(sql, params, "all")
            models = self._usage_groups(sql, params, "model")
            tools = self._usage_groups(sql, params, "tool")
            projects = self._usage_groups(sql, params, "project")
            days = self.conn.execute(sql + """
                SELECT date(timestamp_ms / 1000.0, 'unixepoch', 'localtime') AS day,
                    SUM(observed_total) AS tokens, SUM(effective_cost) AS cost
                FROM facts WHERE granularity = 'event' AND timestamp_ms IS NOT NULL
                GROUP BY day HAVING day IS NOT NULL AND COUNT(observed_total) > 0 ORDER BY day
            """, params).fetchall()
            unattributed = self.conn.execute(sql + """
                SELECT SUM(observed_total) FROM calculated
                WHERE granularity != 'event' OR timestamp_ms IS NULL
            """, params).fetchone()[0]
            return AnalyticsSnapshot(
                summary=totals[0].summary if totals else UsageSummary(),
                models=models, tools=tools, projects=projects,
                days=tuple(DailyUsage(r["day"], r["tokens"], r["cost"]) for r in days),
                unattributed_tokens=unattributed or 0,
            )
        finally:
            self.conn.execute("RELEASE analytics_read")

    def apply(self, snapshot: SessionSnapshot) -> None:
        """Apply a session snapshot transactionally."""
        ref = snapshot.ref
        src = ref.source
        title_norm = normalize_text(snapshot.title)
        project_norm = normalize_text(snapshot.project_label or snapshot.project_id or "")

        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO sessions (
                    key, tool, source_root, profile, sessions_dir, client, native_id,
                    title, title_norm, project_id, project_label, project_norm,
                    cwd, started_ms, updated_ms, kind, archived, text_state,
                    indexed_at, revision, locators, warnings, project_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    ref.key,
                    src.tool,
                    str(src.canonical_root),
                    src.profile,
                    str(src.canonical_sessions_dir) if src.sessions_dir else None,
                    snapshot.client,
                    ref.native_id,
                    snapshot.title,
                    title_norm,
                    snapshot.project_id,
                    snapshot.project_label,
                    project_norm,
                    str(snapshot.cwd) if snapshot.cwd else None,
                    snapshot.started_ms,
                    snapshot.updated_ms,
                    snapshot.kind,
                    1 if snapshot.archived else 0,
                    snapshot.text_state,
                    int(os.path.getmtime(self.db_path) * 1000) if self.db_path.exists() else 0,
                    ref.revision,
                    json.dumps([str(p) for p in ref.locators]),
                    json.dumps(list(snapshot.warnings)),
                    self._project_key(snapshot.cwd, snapshot.project_id),
                ),
            )

            # Replace messages: delete existing, insert new
            self.conn.execute("DELETE FROM messages WHERE session_key = ?;", (ref.key,))
            for msg in snapshot.messages:
                body_norm = normalize_text(msg.text)
                self.conn.execute(
                    """
                    INSERT INTO messages (
                        session_key, source_id, parent_id, ordinal, role, body, body_norm, timestamp_ms, flags
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        ref.key,
                        msg.source_id,
                        msg.parent_id,
                        msg.ordinal,
                        msg.role,
                        msg.text,
                        body_norm,
                        msg.timestamp_ms,
                        json.dumps(list(msg.flags)),
                    ),
                )

            self.conn.execute("DELETE FROM usage_records WHERE session_key = ?", (ref.key,))
            self.conn.executemany(
                """INSERT INTO usage_records (
                    session_key, source_id, timestamp_ms, model, provider,
                    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                    reasoning_tokens, total_tokens, cost_usd, cost_kind, granularity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ((ref.key, u.source_id, u.timestamp_ms, u.model, u.provider,
                  u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens,
                  u.reasoning_tokens, u.total_tokens, u.cost_usd, u.cost_kind, u.granularity)
                 for u in snapshot.usage),
            )

    def remove_session(self, session_key: str) -> None:
        """Remove a session and its associated messages and usage facts."""
        with self.conn:
            self.conn.execute("DELETE FROM sessions WHERE key = ?;", (session_key,))

    def mark_session_unavailable(self, session_key: str, reason: str) -> None:
        """Mark a session as unavailable with a diagnostic reason."""
        with self.conn:
            row = self.conn.execute("SELECT warnings FROM sessions WHERE key = ?;", (session_key,)).fetchone()
            if row is not None:
                warnings = json.loads(row[0]) if row[0] else []
                if reason not in warnings:
                    warnings.append(reason)
                self.conn.execute(
                    "UPDATE sessions SET text_state = 'unavailable', warnings = ? WHERE key = ?;",
                    (json.dumps(warnings), session_key),
                )

    def update_source_status(
        self,
        source_key: str,
        tool: str,
        root: str,
        profile: str | None,
        status: str,
        error: str | None = None,
    ) -> None:
        """Update scan status and diagnostics for a source root."""
        with self.conn:
            now_ms = int(os.times().system * 1000)
            self.conn.execute(
                """
                INSERT OR REPLACE INTO source_status (
                    source_key, tool, root, profile, last_scanned_at, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (source_key, tool, root, profile, now_ms, status, error),
            )

    def get_source_statuses(self) -> list[dict]:
        """Fetch status and diagnostics for all configured sources."""
        rows = self.conn.execute(
            "SELECT source_key, tool, root, profile, last_scanned_at, status, error FROM source_status;"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_session(self, key: str) -> SessionSnapshot | None:
        """Retrieve a session snapshot by key."""
        row = self.conn.execute("SELECT * FROM sessions WHERE key = ?;", (key,)).fetchone()
        if row is None:
            return None

        # Fetch messages
        msg_rows = self.conn.execute(
            "SELECT source_id, parent_id, ordinal, role, body, timestamp_ms, flags FROM messages WHERE session_key = ? ORDER BY ordinal ASC;",
            (key,),
        ).fetchall()
        messages = tuple(
            MessageRecord(
                source_id=m["source_id"],
                parent_id=m["parent_id"],
                ordinal=m["ordinal"],
                role=m["role"],
                text=m["body"],
                timestamp_ms=m["timestamp_ms"],
                flags=tuple(json.loads(m["flags"])) if m["flags"] else (),
            )
            for m in msg_rows
        )

        usage_rows = self.conn.execute(
            "SELECT * FROM usage_records WHERE session_key = ? ORDER BY source_id", (key,)
        ).fetchall()
        usage = tuple(UsageRecord(**{k: r[k] for k in r.keys() if k != "session_key"}) for r in usage_rows)

        locators = tuple(Path(p) for p in json.loads(row["locators"])) if row["locators"] else ()
        warnings = tuple(json.loads(row["warnings"])) if row["warnings"] else ()

        source = SourceSpec(
            tool=row["tool"],
            root=Path(row["source_root"]),
            profile=row["profile"],
            sessions_dir=Path(row["sessions_dir"]) if row["sessions_dir"] else None,
        )
        ref = SessionRef(
            key=row["key"],
            source=source,
            native_id=row["native_id"],
            locators=locators,
            revision=row["revision"],
        )

        return SessionSnapshot(
            ref=ref,
            client=row["client"],
            title=row["title"],
            project_id=row["project_id"],
            project_label=row["project_label"],
            cwd=Path(row["cwd"]) if row["cwd"] else None,
            started_ms=row["started_ms"],
            updated_ms=row["updated_ms"],
            kind=row["kind"],
            archived=bool(row["archived"]),
            text_state=row["text_state"],
            messages=messages,
            usage=usage,
            warnings=warnings,
        )

    def search(
        self,
        query: str,
        filters: SearchFilters,
        offset: int = 0,
        limit: int = 100,
    ) -> SearchPage:
        """Search sessions by query and filters."""
        query = query.strip()
        norm_q = normalize_text(query)

        where_clauses, params = self._session_filter(filters, activity_time=True)

        base_where = " AND ".join(where_clauses)

        if not norm_q:
            # Empty query: order by updated_ms DESC, key ASC
            count_sql = f"SELECT COUNT(*) FROM sessions s WHERE {base_where};"
            total_count = self.conn.execute(count_sql, params).fetchone()[0]

            rows_sql = f"""
                SELECT s.key, s.client, s.title, s.project_label, s.cwd,
                       s.updated_ms, s.text_state, s.warnings, s.kind, s.archived
                FROM sessions s
                WHERE {base_where}
                ORDER BY s.updated_ms DESC NULLS LAST, s.key ASC
                LIMIT ? OFFSET ?;
            """
            rows = self.conn.execute(rows_sql, (*params, limit, offset)).fetchall()

            results = tuple(
                SearchResult(
                    session_key=r["key"],
                    client=r["client"],
                    title=r["title"],
                    project_label=r["project_label"],
                    cwd=r["cwd"],
                    updated_ms=r["updated_ms"],
                    text_state=r["text_state"],
                    match_kind="none",
                    matched_message_id=None,
                    excerpt=None,
                    warnings=tuple(json.loads(r["warnings"])) if r["warnings"] else (),
                    kind=r["kind"],
                    archived=bool(r["archived"]),
                )
                for r in rows
            )
            return SearchPage(results=results, total_count=total_count, offset=offset, limit=limit)

        # Keep candidates and deduplication in SQLite; only the requested page's
        # original bodies cross into Python. A successful empty MATCH is final.
        use_fts = len(norm_q) >= 3 and self.fts_available
        while True:
            if use_fts:
                body_from = "message_fts JOIN messages m ON message_fts.rowid = m.id JOIN eligible e ON e.key = m.session_key"
                body_where = "message_fts MATCH ? AND instr(m.body_norm, ?) > 0"
                body_params = ['"' + norm_q.replace('"', '""') + '"', norm_q]
            else:
                # Drive literal scans from eligible sessions, so narrow filters
                # do not inspect every message in unrelated tools/projects.
                body_from = "eligible e CROSS JOIN messages m ON m.session_key = e.key"
                body_where = "instr(m.body_norm, ?) > 0"
                body_params = [norm_q]

            sql = f"""
                WITH eligible AS MATERIALIZED (
                    SELECT s.key, s.updated_ms, s.title_norm, s.project_norm
                    FROM sessions s WHERE {base_where}
                ), body_candidates AS (
                    SELECT m.session_key, m.id,
                           ROW_NUMBER() OVER (
                               PARTITION BY m.session_key ORDER BY m.ordinal, m.id
                           ) AS position
                    FROM {body_from} WHERE {body_where}
                ), matches AS MATERIALIZED (
                    SELECT e.key, e.updated_ms, b.id AS message_id
                    FROM eligible e
                    LEFT JOIN body_candidates b ON b.session_key = e.key AND b.position = 1
                    WHERE b.id IS NOT NULL
                       OR instr(e.title_norm, ?) > 0 OR instr(e.project_norm, ?) > 0
                ), page AS MATERIALIZED (
                    SELECT * FROM matches
                    ORDER BY message_id IS NULL, updated_ms DESC NULLS LAST, key ASC
                    LIMIT ? OFFSET ?
                )
                SELECT totals.total_count, s.key, s.client, s.title, s.project_label,
                       s.cwd, s.updated_ms, s.text_state, s.warnings, s.kind, s.archived,
                       m.source_id, m.body
                FROM (SELECT COUNT(*) AS total_count FROM matches) totals
                LEFT JOIN page p ON 1 = 1
                LEFT JOIN sessions s ON s.key = p.key
                LEFT JOIN messages m ON m.id = p.message_id
                ORDER BY p.message_id IS NULL, p.updated_ms DESC NULLS LAST, p.key ASC
            """
            try:
                rows = self.conn.execute(
                    sql, (*params, *body_params, norm_q, norm_q, limit, offset)
                ).fetchall()
                break
            except sqlite3.OperationalError as exc:
                # SQLite can misreport cancellation during virtual-table setup as
                # SQLITE_ERROR. Only a missing optional FTS table permits retry.
                if not use_fts or str(exc) != "no such table: message_fts":
                    raise
                use_fts = False

        results = tuple(
            SearchResult(
                session_key=r["key"],
                client=r["client"],
                title=r["title"],
                project_label=r["project_label"],
                cwd=r["cwd"],
                updated_ms=r["updated_ms"],
                text_state=r["text_state"],
                match_kind="body" if r["source_id"] is not None else "metadata",
                matched_message_id=r["source_id"],
                excerpt=self._make_excerpt(r["body"], norm_q) if r["source_id"] is not None else None,
                warnings=tuple(json.loads(r["warnings"])) if r["warnings"] else (),
                kind=r["kind"],
                archived=bool(r["archived"]),
            )
            for r in rows if r["key"] is not None
        )
        return SearchPage(results=results, total_count=rows[0]["total_count"], offset=offset, limit=limit)

    def _make_excerpt(self, body: str, norm_q: str, window: int = 40) -> str:
        """Create a readable excerpt around the match."""
        norm_body = normalize_text(body)
        idx = norm_body.find(norm_q)
        if idx == -1 or len(norm_body) != len(body):
            # Normalized offsets cannot safely slice the original text.
            return body

        start = max(0, idx - window)
        end = min(len(body), idx + len(norm_q) + window)
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(body) else ""
        return prefix + body[start:end] + suffix

    def context(self, session_key: str, message_id: str | None = None) -> tuple[MessageRecord, ...]:
        """Fetch messages for a session. If message_id is provided, returns conversation context."""
        rows = self.conn.execute(
            """
            SELECT source_id, parent_id, ordinal, role, body, timestamp_ms, flags
            FROM messages
            WHERE session_key = ?
            ORDER BY ordinal ASC;
            """,
            (session_key,),
        ).fetchall()

        messages = [
            MessageRecord(
                source_id=r["source_id"],
                parent_id=r["parent_id"],
                ordinal=r["ordinal"],
                role=r["role"],
                text=r["body"],
                timestamp_ms=r["timestamp_ms"],
                flags=tuple(json.loads(r["flags"])) if r["flags"] else (),
            )
            for r in rows
        ]
        return tuple(messages)

    def close(self) -> None:
        """Close database connection."""
        self.conn.close()
