from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ai_session_hub.models import MessageRecord, SessionRef, SessionSnapshot, SourceSpec, UsageRecord
from ai_session_hub.store import SearchFilters, Store
from ai_session_hub.usage import PricingRule


class TestUsageStore(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "index.sqlite3")
        self.addCleanup(self.store.close)

    def snapshot(self, key: str, usage: tuple[UsageRecord, ...] = (), **changes) -> SessionSnapshot:
        source = SourceSpec("omp", self.root / "source")
        snapshot = SessionSnapshot(
            SessionRef(key, source, key, (), "revision"), "omp", key, None, "Same name",
            self.root / key, 1, 9999999999999, "conversation", False, "native",
            (MessageRecord("m", None, 0, "assistant", "retained searchable prose", 1),), usage,
        )
        return replace(snapshot, **changes)

    def test_accounting_parity_and_unknown_coverage(self) -> None:
        facts = (
            UsageRecord("one", 1000, "shared", "a", 100, 20, 30, 10, 5, 120, 0.5, "actual"),
            UsageRecord("two", 2000, "shared", "b", 50, 10, 0, 0, None, 60, 0.2, "estimated"),
        )
        self.store.apply(self.snapshot("known", facts))
        self.store.apply(self.snapshot("zero", (UsageRecord("zero", total_tokens=0),)))
        self.store.apply(self.snapshot("unknown", (UsageRecord("partial", input_tokens=8),)))
        result = self.store.analytics(SearchFilters())
        summary = result.summary
        self.assertEqual((summary.total_tokens, summary.input_tokens, summary.output_tokens), (180, 158, 30))
        self.assertEqual((summary.cache_read_tokens, summary.cache_write_tokens, summary.reasoning_tokens), (30, 10, 5))
        self.assertEqual((summary.session_count, summary.unknown_sessions, summary.message_count), (3, 1, 3))
        self.assertEqual((summary.records, summary.priced_records, summary.estimated_records), (4, 2, 1))
        self.assertEqual(summary.known_token_records, 3)
        self.assertAlmostEqual(summary.cost_usd, 0.7)
        page = self.store.session_usage(["known", "zero", "unknown", "absent"])
        self.assertNotIn("absent", page)
        self.assertEqual(page["known"].total_tokens, 180)
        self.assertEqual(page["zero"].total_tokens, 0)
        self.assertEqual(page["zero"].unknown_sessions, 0)
        self.assertIsNone(page["unknown"].total_tokens)
        self.assertIsNone(page["unknown"].cost_usd)
        self.assertEqual(sum(g.summary.total_tokens or 0 for g in result.projects), summary.total_tokens)
        self.assertEqual(sum(g.summary.total_tokens or 0 for g in result.models), summary.total_tokens)
        named_models = [g for g in result.models if g.summary.models]
        self.assertEqual({tuple(json.loads(g.key)) for g in named_models}, {("a", "shared"), ("b", "shared")})
        self.assertEqual([g.summary.models for g in named_models], [("shared",), ("shared",)])
        self.assertEqual(sum(d.total_tokens for d in result.days), 180)

    def test_pricing_precedence_missing_components_and_native_cost(self) -> None:
        base = UsageRecord("base", model="m", provider="p", input_tokens=100, output_tokens=20,
                           cache_read_tokens=30, cache_write_tokens=10)
        self.store.apply(self.snapshot("exact", (base,)))
        self.store.apply(self.snapshot("generic", (replace(base, provider="other"),)))
        self.store.apply(self.snapshot("native", (replace(base, cost_usd=0, cost_kind="actual"),)))
        self.store.apply(self.snapshot("missing-cache", (replace(base, cache_write_tokens=None),)))
        self.store.apply(self.snapshot("negative", (replace(base, input_tokens=20),)))
        self.store.apply(self.snapshot("missing-rate", (replace(base, model="other"),)))
        self.store.apply(self.snapshot("all-zero", (replace(base, model=None, provider=None, input_tokens=0,
                                                              output_tokens=0, cache_read_tokens=0, cache_write_tokens=0),)))
        rules = (PricingRule("m", input_per_million=10, output_per_million=20,
                             cache_read_per_million=30, cache_write_per_million=40),
                 PricingRule("m", "p", 1, 2, 3, 4))
        rows = self.store.session_usage(["exact", "generic", "native", "missing-cache", "negative", "missing-rate", "all-zero"], pricing=rules)
        self.assertAlmostEqual(rows["exact"].cost_usd, (60 + 40 + 90 + 40) / 1000000)
        self.assertAlmostEqual(rows["generic"].cost_usd, (600 + 400 + 900 + 400) / 1000000)
        self.assertEqual((rows["native"].cost_usd, rows["native"].estimated_records), (0, 0))
        for key in ("missing-cache", "negative", "missing-rate"):
            self.assertIsNone(rows[key].cost_usd)
            self.assertEqual(rows[key].priced_records, 0)
        self.assertEqual((rows["all-zero"].cost_usd, rows["all-zero"].priced_records), (0, 1))
        self.assertEqual(rows["exact"].estimated_records, 1)
        result = self.store.analytics(SearchFilters(), pricing=rules)
        self.assertAlmostEqual(result.summary.cost_usd, sum(r.cost_usd or 0 for r in rows.values()))
        self.assertEqual((result.summary.priced_records, result.summary.estimated_records), (4, 3))
        # Exact-provider rules do not borrow missing rates from a generic rule.
        incomplete = (*rules[:1], PricingRule("m", "p", 1, 2, None, 4))
        self.assertIsNone(self.store.session_usage(["exact"], pricing=incomplete)["exact"].cost_usd)
        # A zero cache component needs no rate, but an unknown one still does.
        self.store.apply(self.snapshot("no-cache", (replace(base, cache_read_tokens=0, cache_write_tokens=0),)))
        no_cache = self.store.session_usage(["no-cache"], pricing=(PricingRule("m", "p", 1, 2),))["no-cache"]
        self.assertAlmostEqual(no_cache.cost_usd, 140 / 1000000)

    def test_usage_time_differs_from_activity_and_coarse_is_unattributed(self) -> None:
        self.store.apply(self.snapshot("active", (
            UsageRecord("old", timestamp_ms=1000, total_tokens=10),
            UsageRecord("recent", timestamp_ms=3000, total_tokens=30),
            UsageRecord("coarse", timestamp_ms=4000, total_tokens=50, granularity="session"),
            UsageRecord("undated", total_tokens=7),
        )))
        self.store.apply(self.snapshot("inactive", (UsageRecord("new", timestamp_ms=3000, total_tokens=5),), updated_ms=0))
        self.store.apply(self.snapshot("no-usage"))
        all_time = self.store.analytics(SearchFilters())
        bounded = self.store.analytics(SearchFilters(time_bound_ms=2000))
        self.assertEqual((all_time.summary.total_tokens, bounded.summary.total_tokens), (102, 35))
        self.assertEqual((bounded.unattributed_tokens, all_time.unattributed_tokens), (57, 57))
        self.assertEqual((bounded.summary.session_count, bounded.summary.unknown_sessions), (3, 1))
        self.assertEqual(sum(d.total_tokens for d in bounded.days), 35)
        self.assertEqual(self.store.search("", SearchFilters(time_bound_ms=2000)).total_count, 2)
        self.assertEqual(sum(g.summary.total_tokens or 0 for g in bounded.projects), 35)
        self.assertEqual(sum(g.summary.total_tokens or 0 for g in bounded.models), 35)

    @unittest.skipUnless(hasattr(time, "tzset"), "requires POSIX local timezone support")
    def test_daily_buckets_use_local_calendar(self) -> None:
        # 2026-01-01 01:00 UTC is still December 31 in Los Angeles.
        self.store.apply(self.snapshot("dated", (UsageRecord("u", 1767229200000, total_tokens=12),)))
        previous = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/Los_Angeles"
            time.tzset()
            self.assertEqual([(d.day, d.total_tokens) for d in self.store.analytics(SearchFilters()).days], [("2025-12-31", 12)])
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()

    def test_replace_repeat_rollback_and_cascade(self) -> None:
        first = self.snapshot("session", (UsageRecord("u", total_tokens=100),))
        self.store.apply(first)
        self.store.apply(first)
        self.assertEqual(self.store.analytics(SearchFilters()).summary.total_tokens, 100)
        bad = replace(first, title="uncommitted", usage=(UsageRecord("duplicate", total_tokens=10), UsageRecord("duplicate", total_tokens=20)))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.apply(bad)
        self.assertEqual(self.store.get_session("session").title, "session")
        self.assertEqual(self.store.session_usage(["session"])["session"].total_tokens, 100)
        self.store.apply(replace(first, usage=(UsageRecord("new", total_tokens=7),)))
        loaded = self.store.get_session("session")
        self.assertEqual(loaded.usage, (UsageRecord("new", total_tokens=7),))
        self.assertEqual(self.store.analytics(SearchFilters()).summary.total_tokens, 7)
        self.store.remove_session("session")
        self.assertEqual(self.store.session_usage(["session"]), {})
        self.assertIsNone(self.store.analytics(SearchFilters()).summary.total_tokens)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0], 0)

    def test_projects_resolve_paths_and_do_not_merge_equal_labels(self) -> None:
        target = self.root / "parent" / "same"
        target.mkdir(parents=True)
        link = self.root / "linked"
        link.symlink_to(target, target_is_directory=True)
        self.store.apply(self.snapshot("one", (UsageRecord("u", total_tokens=10),), cwd=target))
        self.store.apply(self.snapshot("linked", (UsageRecord("u", total_tokens=15),), cwd=link))
        self.store.apply(self.snapshot("two", (UsageRecord("u", total_tokens=25),), cwd=self.root / "other" / "same"))
        self.store.apply(self.snapshot("unknown", cwd=Path("relative"), project_id=None))
        groups = self.store.analytics(SearchFilters()).projects
        self.assertEqual(len(groups), 3)
        keyed = {g.key: g.summary for g in groups}
        self.assertEqual(keyed[str(target.resolve())].total_tokens, 25)
        self.assertEqual(keyed[str(target.resolve())].session_count, 2)
        self.assertIsNone(keyed[""].total_tokens)
        self.assertEqual([g.key for g in groups[:2]], sorted(g.key for g in groups[:2]))
        for group in groups:
            filters = SearchFilters(project_key=group.key)
            self.assertEqual(self.store.analytics(filters).summary, group.summary)
            self.assertEqual(self.store.search("", filters).total_count, group.summary.session_count)


class TestNativeMigration(unittest.TestCase):
    def legacy_index(self, path: Path, *, fail: bool = False) -> None:
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.executescript("""
                CREATE TABLE sessions (
                    key TEXT PRIMARY KEY, tool TEXT NOT NULL, source_root TEXT NOT NULL,
                    profile TEXT, sessions_dir TEXT, client TEXT NOT NULL, native_id TEXT NOT NULL,
                    title TEXT NOT NULL, title_norm TEXT NOT NULL, project_id TEXT, project_label TEXT,
                    project_norm TEXT, cwd TEXT, started_ms INTEGER, updated_ms INTEGER,
                    kind TEXT NOT NULL, archived INTEGER NOT NULL, text_state TEXT NOT NULL,
                    indexed_at INTEGER NOT NULL, revision TEXT NOT NULL, locators TEXT NOT NULL,
                    warnings TEXT NOT NULL, alias_of TEXT REFERENCES sessions(key) ON DELETE SET NULL
                );
                CREATE INDEX idx_sessions_alias_of ON sessions(alias_of);
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, session_key TEXT REFERENCES sessions(key) ON DELETE CASCADE,
                    source_id TEXT, parent_id TEXT, ordinal INTEGER, role TEXT, body TEXT,
                    body_norm TEXT, timestamp_ms INTEGER, flags TEXT, UNIQUE(session_key, source_id)
                );
                CREATE VIRTUAL TABLE message_fts USING fts5(body_norm, content='messages', content_rowid='id', tokenize='trigram');
                CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO message_fts(rowid, body_norm) VALUES (new.id, new.body_norm);
                END;
                CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO message_fts(message_fts, rowid, body_norm) VALUES ('delete', old.id, old.body_norm);
                END;
                CREATE TABLE session_aliases (
                    session_key TEXT REFERENCES sessions(key) ON DELETE CASCADE,
                    compatible_client TEXT, external_id TEXT, PRIMARY KEY(session_key, compatible_client, external_id)
                );
                CREATE TABLE source_status (
                    source_key TEXT PRIMARY KEY, tool TEXT, root TEXT, profile TEXT,
                    last_scanned_at INTEGER, status TEXT, error TEXT
                );
                INSERT INTO sessions VALUES ('native','omp','/absent-native',NULL,NULL,'omp','native','Native','native',NULL,NULL,'','/project',1,2,'conversation',0,'native',0,'old','[]','[]',NULL);
                INSERT INTO sessions VALUES ('archive','token-monitor','/absent-archive',NULL,NULL,'omp','archive','Imported','imported',NULL,NULL,'',NULL,1,2,'conversation',0,'metadata-only',0,'old','[]','[]','native');
                INSERT INTO messages VALUES (1,'native','message',NULL,1,'assistant','中文检索 native retained','中文检索 native retained',2,'[]');
                INSERT INTO session_aliases VALUES ('native','omp','native');
                INSERT INTO session_aliases VALUES ('archive','omp','native');
                INSERT INTO source_status VALUES ('native','omp','/absent-native',NULL,0,'ready',NULL);
                INSERT INTO source_status VALUES ('archive','token-monitor','/absent-archive',NULL,0,'ready',NULL);
                PRAGMA user_version = 1;
            """)
            if fail:
                conn.execute("CREATE TRIGGER migration_failure BEFORE DELETE ON source_status BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")

    def test_cutover_preserves_native_search_without_source_reads(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "index.sqlite3"
            self.legacy_index(path)
            with patch("ai_session_hub.store.open_ro_sqlite", side_effect=AssertionError("migration read a source")):
                store = Store(path)
            try:
                self.assertEqual(store.conn.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual([r.session_key for r in store.search("native retained", SearchFilters()).results], ["native"])
                self.assertEqual(store.search("中文检索", SearchFilters()).total_count, 1)
                self.assertIsNone(store.get_session("archive"))
                self.assertEqual([s["tool"] for s in store.get_source_statuses()], ["omp"])
                self.assertEqual(store.get_session("native").ref.revision, "")
                self.assertEqual(store.analytics(SearchFilters(project_key="/project")).summary.unknown_sessions, 1)
                self.assertNotIn("alias_of", {r["name"] for r in store.conn.execute("PRAGMA table_info(sessions)")})
                self.assertIsNone(store.conn.execute("SELECT name FROM sqlite_master WHERE name='session_aliases'").fetchone())
                reader = Store(path, read_only=True)
                try:
                    self.assertEqual(reader.search("native retained", SearchFilters()).total_count, 1)
                    self.assertEqual(reader.session_usage(["native"])["native"].message_count, 1)
                finally:
                    reader.close()
                store.remove_session("native")
                self.assertEqual(store.search("native retained", SearchFilters()).total_count, 0)
            finally:
                store.close()

    def test_failed_cutover_rolls_back_legacy_rows_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "index.sqlite3"
            self.legacy_index(path, fail=True)
            with self.assertRaises(sqlite3.IntegrityError):
                Store(path)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT alias_of FROM sessions WHERE key='archive'").fetchone()[0], "native")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM session_aliases").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM message_fts WHERE message_fts MATCH 'retained'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
