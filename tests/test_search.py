from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ai_session_hub.models import (
    MessageRecord,
    SessionRef,
    SessionSnapshot,
    SourceSpec,
)
from ai_session_hub.store import SearchFilters, Store


class TestSearch(unittest.TestCase):
    """Test full-text search, trigram matching, literal queries, and Unicode normalization."""

    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.db_path = Path(self.td.name) / "index.sqlite3"
        self.store = Store(self.db_path)

        # Seed sample sessions
        src = SourceSpec(tool="codex", root=Path(self.td.name))

        # Session 1: Assistant containing Chinese phrase and English phrase
        ref1 = SessionRef(key="key-1", source=src, native_id="1", locators=(), revision="r1")
        m1_1 = MessageRecord(source_id="m1_1", parent_id=None, ordinal=1, role="user", text="Tell me something interesting.", timestamp_ms=1000)
        m1_2 = MessageRecord(
            source_id="m1_2",
            parent_id="m1_1",
            ordinal=2,
            role="assistant",
            text="这里包含中文检索短语以及 Assistant-only resume phrase for testing.",
            timestamp_ms=2000,
        )
        snap1 = SessionSnapshot(
            ref=ref1, client="codex", title="Session One", project_id="proj1", project_label="Project 1",
            cwd=Path(self.td.name), started_ms=1000, updated_ms=2000, kind="conversation",
            archived=False, text_state="native", messages=(m1_1, m1_2)
        )
        self.store.apply(snap1)

        # Session 2: SQL metacharacters and symbols
        ref2 = SessionRef(key="key-2", source=src, native_id="2", locators=(), revision="r1")
        m2_1 = MessageRecord(
            source_id="m2_1",
            parent_id=None,
            ordinal=1,
            role="user",
            text='Query contains 100% of the text, an underscore _symbol, a "quote", and " OR 1=1 -- injection attempt.',
            timestamp_ms=3000,
        )
        snap2 = SessionSnapshot(
            ref=ref2, client="codex", title="Session Two Symbols", project_id="proj2", project_label="Project 2",
            cwd=Path(self.td.name), started_ms=3000, updated_ms=3000, kind="conversation",
            archived=False, text_state="native", messages=(m2_1,)
        )
        self.store.apply(snap2)

        # Session 3: Metadata-only hit (no body text)
        ref3 = SessionRef(key="key-3", source=src, native_id="3", locators=(), revision="r1")
        snap3 = SessionSnapshot(
            ref=ref3, client="cursor-ide", title="Metadata Only Special Keyword", project_id="proj3", project_label="Project 3",
            cwd=None, started_ms=4000, updated_ms=4000, kind="conversation",
            archived=False, text_state="metadata-only", messages=()
        )
        self.store.apply(snap3)

        # Session 4: Unicode normalization with length-changing characters (ligatures and full-width)
        ref4 = SessionRef(key="key-4", source=src, native_id="4", locators=(), revision="r1")
        # 'ﬁ' (U+FB01) normalizes to 'fi' (2 characters) in NFKC
        m4_1 = MessageRecord(
            source_id="m4_1",
            parent_id=None,
            ordinal=1,
            role="assistant",
            text="The ﬁle is located at /path/to/ﬁle and has ＡＢＣ fullwidth text.",
            timestamp_ms=5000,
        )
        snap4 = SessionSnapshot(
            ref=ref4, client="claude", title="Session Four Ligature", project_id="proj4", project_label="Project 4",
            cwd=Path(self.td.name), started_ms=5000, updated_ms=5000, kind="conversation",
            archived=False, text_state="native", messages=(m4_1,)
        )
        self.store.apply(snap4)

    def tearDown(self) -> None:
        self.store.close()
        self.td.cleanup()

    def test_assistant_search_chinese_and_english(self) -> None:
        # Search assistant-only Chinese phrase
        res_zh = self.store.search("中文检索短语", SearchFilters())
        self.assertEqual(res_zh.total_count, 1)
        self.assertEqual(res_zh.results[0].session_key, "key-1")
        self.assertEqual(res_zh.results[0].match_kind, "body")
        self.assertEqual(res_zh.results[0].matched_message_id, "m1_2")
        self.assertIn("中文检索短语", res_zh.results[0].excerpt or "")

        # Search assistant-only English phrase
        res_en = self.store.search("Assistant-only resume phrase", SearchFilters())
        self.assertEqual(res_en.total_count, 1)
        self.assertEqual(res_en.results[0].session_key, "key-1")
        self.assertEqual(res_en.results[0].match_kind, "body")

        # Context returns conversation
        ctx = self.store.context("key-1", "m1_2")
        self.assertEqual(len(ctx), 2)
        self.assertEqual(ctx[0].text, "Tell me something interesting.")
        self.assertIn("中文检索短语", ctx[1].text)

    def test_short_query_and_literal_metacharacters(self) -> None:
        # Two-character Chinese query (< 3 chars, uses instr directly)
        res_short = self.store.search("中文", SearchFilters())
        self.assertEqual(res_short.total_count, 1)
        self.assertEqual(res_short.results[0].session_key, "key-1")

        # Literal percent sign
        res_pct = self.store.search("100%", SearchFilters())
        self.assertEqual(res_pct.total_count, 1)
        self.assertEqual(res_pct.results[0].session_key, "key-2")

        # Literal underscore
        res_und = self.store.search("_symbol", SearchFilters())
        self.assertEqual(res_und.total_count, 1)
        self.assertEqual(res_und.results[0].session_key, "key-2")

        # Literal quote
        res_q = self.store.search('"quote"', SearchFilters())
        self.assertEqual(res_q.total_count, 1)
        self.assertEqual(res_q.results[0].session_key, "key-2")

        # SQL-looking text
        res_sql = self.store.search('" OR 1=1 --', SearchFilters())
        self.assertEqual(res_sql.total_count, 1)
        self.assertEqual(res_sql.results[0].session_key, "key-2")

    def test_metadata_only_matching(self) -> None:
        # Search keyword only present in Session 3's title
        res = self.store.search("Special Keyword", SearchFilters())
        self.assertEqual(res.total_count, 1)
        self.assertEqual(res.results[0].session_key, "key-3")
        self.assertEqual(res.results[0].match_kind, "metadata")
        self.assertIsNone(res.results[0].matched_message_id)

    def test_unicode_normalization(self) -> None:
        # Search using normalized 'file' when source has ligature 'ﬁle'
        res_lig = self.store.search("file", SearchFilters())
        self.assertEqual(res_lig.total_count, 1)
        self.assertEqual(res_lig.results[0].session_key, "key-4")
        self.assertIsNotNone(res_lig.results[0].excerpt)

        # Search using lowercase 'abc' when source has full-width 'ＡＢＣ'
        res_fw = self.store.search("abc", SearchFilters())
        self.assertEqual(res_fw.total_count, 1)
        self.assertEqual(res_fw.results[0].session_key, "key-4")

    def test_fts_disabled_fallback(self) -> None:
        # Disable FTS to test fallback literal search
        self.store.fts_available = False
        res = self.store.search("中文检索短语", SearchFilters())
        self.assertEqual(res.total_count, 1)
        self.assertEqual(res.results[0].session_key, "key-1")
        self.assertEqual(res.results[0].match_kind, "body")

    def test_delete_and_update_fts_sync(self) -> None:
        # Verify Session 1 matches before update
        res = self.store.search("中文检索短语", SearchFilters())
        self.assertEqual(res.total_count, 1)

        # Remove Session 1
        self.store.remove_session("key-1")

        # Search should return 0 results
        res_after = self.store.search("中文检索短语", SearchFilters())
        self.assertEqual(res_after.total_count, 0)

    def test_filtered_pages_keep_body_priority_and_earliest_message(self) -> None:
        src = SourceSpec(tool="omp", root=Path(self.td.name))
        for key, body, updated, kind in (
            ("page-b", True, 100, "conversation"),
            ("page-a", True, 100, "conversation"),
            ("page-meta", False, 900, "conversation"),
            ("page-secondary", True, 200, "subagent"),
            ("page-old", True, 1, "conversation"),
        ):
            messages = tuple(
                MessageRecord(f"{key}-{ordinal}", None, ordinal, "assistant", "abc literal %_", updated)
                for ordinal in (20, 1)
            ) if body else ()
            self.store.apply(SessionSnapshot(
                ref=SessionRef(key, src, key, (), "r1"), client="omp",
                title="abc literal %_", project_id="pages", project_label="Pages",
                cwd=Path(self.td.name), started_ms=updated, updated_ms=updated,
                kind=kind, archived=False, text_state="native" if body else "metadata-only",
                messages=messages,
            ))
        filters = SearchFilters(tool="omp", project="pages", time_bound_ms=50, kind="conversation")
        for accelerated in (True, False):
            self.store.fts_available = accelerated
            for query in ("abc literal %_", "ab", "%_"):
                with self.subTest(fts=accelerated, query=query):
                    first = self.store.search(query, filters, limit=1)
                    self.assertEqual(first.total_count, 3)
                    self.assertEqual(first.results[0].session_key, "page-a")
                    self.assertEqual(first.results[0].matched_message_id, "page-a-1")
                    rest = self.store.search(query, filters, offset=1, limit=2)
                    self.assertEqual([r.session_key for r in rest.results], ["page-b", "page-meta"])
                    self.assertEqual([r.match_kind for r in rest.results], ["body", "metadata"])
                    past_end = self.store.search(query, filters, offset=3, limit=1)
                    self.assertEqual(past_end.total_count, 3)
                    self.assertEqual(past_end.results, ())
                    self.assertEqual(self.store.search(query, SearchFilters(tool="dsh")).total_count, 0)
        metadata = self.store.search("abc", SearchFilters(kind="metadata-only"))
        self.assertEqual([r.session_key for r in metadata.results], ["page-meta"])
        self.assertEqual(metadata.results[0].match_kind, "metadata")

    def test_empty_fts_result_does_not_scan_message_corpus(self) -> None:
        if not self.store.fts_available:
            self.skipTest("SQLite trigram FTS unavailable")
        with self.store.conn:
            self.store.conn.executemany(
                "INSERT INTO messages(session_key, source_id, ordinal, role, body, body_norm, flags) VALUES ('key-1', ?, ?, 'assistant', ?, ?, '[]')",
                ((f"bulk-{i}", i + 100, "unrelated message", "unrelated message") for i in range(2000)),
            )
        # Empty trigram postings must finish without work proportional to 2,000
        # unrelated bodies. A literal fallback exhausts this VM-step allowance.
        steps = 0

        def exhausted() -> int:
            nonlocal steps
            steps += 100
            return int(steps >= 5000)

        self.store.conn.set_progress_handler(exhausted, 100)
        try:
            page = self.store.search("unfindablephrase", SearchFilters())
            self.assertEqual(page.total_count, 0)
            self.assertEqual(page.results, ())
        finally:
            self.store.conn.set_progress_handler(None, 0)

    def test_cancelled_fts_query_does_not_retry_literal_scan(self) -> None:
        interrupted = False

        def cancel_once() -> int:
            nonlocal interrupted
            if interrupted:
                return 0
            interrupted = True
            return 1

        self.store.conn.set_progress_handler(cancel_once, 1)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.store.search("phrase", SearchFilters())
        finally:
            self.store.conn.set_progress_handler(None, 0)

    def test_fts_error_preserves_literal_results(self) -> None:
        self.store.conn.execute("DROP TABLE message_fts")
        page = self.store.search("中文检索短语", SearchFilters())
        self.assertEqual([r.session_key for r in page.results], ["key-1"])
        self.assertEqual(page.results[0].matched_message_id, "m1_2")

    def test_length_changing_normalization_keeps_distant_match_visible(self) -> None:
        text = "ﬁ " + "earlier context " * 30 + "中文检索短语"
        with self.store.conn:
            self.store.conn.execute(
                "UPDATE messages SET body = ?, body_norm = ? WHERE source_id = 'm1_2'",
                (text, text.replace("ﬁ", "fi")),
            )
        page = self.store.search("中文检索短语", SearchFilters())
        self.assertEqual(page.results[0].excerpt, text)

    def test_exact_project_filter_is_independent_of_display_label(self) -> None:
        from dataclasses import replace

        original = self.store.get_session("key-1")
        self.store.apply(replace(original, cwd=Path(self.td.name) / "one" / "same", project_label="Same"))
        other = self.store.get_session("key-2")
        self.store.apply(replace(other, cwd=Path(self.td.name) / "two" / "same", project_label="Same"))
        exact = str((Path(self.td.name) / "one" / "same").resolve())
        self.assertEqual(
            [r.session_key for r in self.store.search("", SearchFilters(project_key=exact)).results],
            ["key-1"],
        )
        self.assertEqual(self.store.search("Same", SearchFilters(project_key=exact)).total_count, 1)
        self.assertEqual(self.store.search("", SearchFilters(project="Same")).total_count, 2)
        self.store.apply(replace(other, cwd=Path("relative"), project_id=None))
        self.assertEqual(
            [r.session_key for r in self.store.search("", SearchFilters(project_key="")).results],
            ["key-2"],
        )

    def test_read_only_store_sees_only_committed_updates_and_rejects_writes(self) -> None:
        reader = Store(self.db_path, read_only=True)
        try:
            self.store.conn.execute("UPDATE sessions SET title_norm = 'uncommittedneedle' WHERE key = 'key-1'")
            self.assertEqual(reader.search("uncommittedneedle", SearchFilters()).total_count, 0)
            self.store.conn.commit()
            self.assertEqual(reader.search("uncommittedneedle", SearchFilters()).total_count, 1)
            with self.assertRaises(sqlite3.OperationalError):
                reader.conn.execute("DELETE FROM sessions")
        finally:
            self.store.conn.rollback()
            reader.close()


if __name__ == "__main__":
    unittest.main()
