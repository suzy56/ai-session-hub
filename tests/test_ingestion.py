from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import zstandard as zstd

from ai_session_hub.adapters.claude import ClaudeAdapter
from ai_session_hub.adapters.codex import CodexAdapter
from ai_session_hub.adapters.dsh import DshAdapter
from ai_session_hub.adapters.hermes import HermesAdapter
from ai_session_hub.adapters.omp import OmpAdapter
from ai_session_hub.models import SessionRef, SourceSpec, canonical_session_key


class TestIngestion(unittest.TestCase):
    """Test ingestion correctness and boundaries across all adapters."""

    def test_codex_ingestion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sessions_dir = root / "sessions"
            sessions_dir.mkdir(parents=True)

            # Create state_1.sqlite
            state_db = root / "state_1.sqlite"
            conn = sqlite3.connect(state_db)
            conn.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    rollout_path TEXT,
                    cwd TEXT,
                    name TEXT,
                    title TEXT,
                    created_at INTEGER,
                    updated_at INTEGER,
                    archived INTEGER,
                    history_mode TEXT,
                    thread_source TEXT,
                    agent_role TEXT
                );
                """
            )
            conn.execute(
                """
                INSERT INTO threads VALUES (
                    '11111111-1111-1111-1111-111111111111',
                    'rollout.jsonl',
                    '/tmp/work',
                    'Test Thread',
                    'Thread Title',
                    1000,
                    2000,
                    0,
                    'paginated',
                    'user',
                    'main'
                );
                """
            )
            conn.commit()
            conn.close()

            # Create thread_history_1.sqlite
            hist_db = root / "thread_history_1.sqlite"
            conn = sqlite3.connect(hist_db)
            conn.execute(
                """
                CREATE TABLE thread_items (
                    thread_id TEXT,
                    turn_id TEXT,
                    item_id TEXT,
                    rollout_ordinal INTEGER,
                    created_at_ms INTEGER,
                    item_type TEXT,
                    item_json TEXT
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE thread_history_projection_state (
                    thread_id TEXT PRIMARY KEY,
                    next_rollout_byte_offset INTEGER,
                    next_rollout_ordinal INTEGER
                );
                """
            )
            # Insert paginated user and agent messages
            u_json = json.dumps({"content": [{"type": "text", "text": "What is Python?"}]})
            a_json = json.dumps({"text": "Python is a programming language."})
            conn.execute(
                "INSERT INTO thread_items VALUES ('11111111-1111-1111-1111-111111111111', 't1', 'u1', 1, 1000, 'userMessage', ?);",
                (u_json,),
            )
            conn.execute(
                "INSERT INTO thread_items VALUES ('11111111-1111-1111-1111-111111111111', 't1', 'a1', 2, 1500, 'agentMessage', ?);",
                (a_json,),
            )
            conn.execute(
                "INSERT INTO thread_history_projection_state VALUES ('11111111-1111-1111-1111-111111111111', 500, 3);"
            )
            conn.commit()
            conn.close()

            # Create rollout JSONL with unprojected tail (ordinal 3)
            jsonl_file = sessions_dir / "rollout.jsonl"
            lines = [
                json.dumps({"type": "session_meta", "payload": {"id": "11111111-1111-1111-1111-111111111111", "cwd": "/tmp/work", "timestamp": 1000}}),
                json.dumps({"type": "response_item", "payload": {"id": "u1", "role": "user", "content": [{"text": "What is Python?"}]}}),
                json.dumps({"type": "response_item", "payload": {"id": "a1", "role": "assistant", "content": [{"text": "Python is a programming language."}]}}),
                # Unprojected tail
                json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {"id": "u2", "type": "UserMessage", "content": [{"text": "Tell me more."}]}}}),
            ]
            jsonl_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            adapter = CodexAdapter()
            source = SourceSpec(tool="codex", root=root)
            refs = list(adapter.discover(source))
            self.assertEqual(len(refs), 1)
            ref = refs[0]
            self.assertEqual(ref.native_id, "11111111-1111-1111-1111-111111111111")

            snap = adapter.read(ref, cancelled=lambda: False)
            self.assertEqual(snap.title, "Test Thread")
            self.assertEqual(snap.client, "codex")
            self.assertEqual(len(snap.messages), 3)
            self.assertEqual(snap.messages[0].text, "What is Python?")
            self.assertEqual(snap.messages[1].text, "Python is a programming language.")
            self.assertEqual(snap.messages[2].text, "Tell me more.")

    def test_claude_ingestion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            projects_dir = root / "projects" / "my-project"
            projects_dir.mkdir(parents=True)

            # JSONL transcript with coalescing assistant blocks and UUID deduplication
            t_file = projects_dir / "session-22222222-2222-2222-2222-222222222222.jsonl"
            lines = [
                json.dumps({"type": "custom-title", "customTitle": "Custom Claude Title"}),
                json.dumps({"type": "user", "uuid": "u1", "message": {"role": "user", "content": "Explain Rust."}, "cwd": td}),
                json.dumps({"type": "user", "uuid": "u1", "message": {"role": "user", "content": "Explain Rust duplicate."}, "cwd": td}),  # duplicate uuid
                # Two assistant blocks with same message id coalescing into one turn
                json.dumps({"type": "assistant", "uuid": "a1", "parentUuid": "u1", "message": {"id": "asst-turn-1", "role": "assistant", "content": [{"type": "text", "text": "Rust is fast."}]}}),
                json.dumps({"type": "assistant", "uuid": "a2", "parentUuid": "a1", "message": {"id": "asst-turn-1", "role": "assistant", "content": [{"type": "text", "text": "It has memory safety."}]}}),
            ]
            t_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            # Also create search.db with a cache-only session
            search_db_dir = root / ".search-index"
            search_db_dir.mkdir(parents=True)
            search_db = search_db_dir / "search.db"
            conn = sqlite3.connect(search_db)
            conn.execute(
                """
                CREATE TABLE sessions (
                    session_id TEXT PRIMARY KEY,
                    project_path TEXT,
                    title TEXT,
                    start_time INTEGER,
                    end_time INTEGER,
                    file_path TEXT
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE messages (
                    uuid TEXT PRIMARY KEY,
                    session_id TEXT,
                    type TEXT,
                    content TEXT,
                    timestamp INTEGER
                );
                """
            )
            conn.execute(
                "INSERT INTO sessions VALUES ('33333333-3333-3333-3333-333333333333', '/tmp/old', 'Cached Session', 1000, 2000, '/missing/file.jsonl');"
            )
            conn.execute(
                "INSERT INTO messages VALUES ('m1', '33333333-3333-3333-3333-333333333333', 'user', 'Cached query', 1000);"
            )
            conn.commit()
            conn.close()

            adapter = ClaudeAdapter()
            source = SourceSpec(tool="claude", root=root)
            refs = {r.native_id: r for r in adapter.discover(source)}
            self.assertIn("22222222-2222-2222-2222-222222222222", refs)
            self.assertIn("33333333-3333-3333-3333-333333333333", refs)

            # Check native session coalescing
            snap_native = adapter.read(refs["22222222-2222-2222-2222-222222222222"], cancelled=lambda: False)
            self.assertEqual(snap_native.title, "Custom Claude Title")
            self.assertEqual(snap_native.text_state, "native")
            self.assertEqual(len(snap_native.messages), 2)
            self.assertEqual(snap_native.messages[0].text, "Explain Rust.")
            self.assertEqual(snap_native.messages[1].text, "Rust is fast.\nIt has memory safety.")
            self.assertEqual(snap_native.messages[1].parent_id, "u1")

            # Check cached session
            snap_cached = adapter.read(refs["33333333-3333-3333-3333-333333333333"], cancelled=lambda: False)
            self.assertEqual(snap_cached.title, "Cached Session")
            self.assertEqual(snap_cached.text_state, "cached")
            self.assertEqual(len(snap_cached.messages), 1)
            self.assertEqual(snap_cached.messages[0].text, "Cached query")

    def test_hermes_ingestion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_db = root / "state.db"
            conn = sqlite3.connect(state_db)
            conn.execute(
                """
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY,
                    source TEXT,
                    title TEXT,
                    cwd TEXT,
                    started_at INTEGER,
                    ended_at INTEGER,
                    parent_session_id INTEGER,
                    end_reason TEXT,
                    model_config TEXT,
                    archived INTEGER
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY,
                    session_id INTEGER,
                    role TEXT,
                    content TEXT,
                    timestamp INTEGER,
                    active INTEGER,
                    compacted INTEGER,
                    tool_name TEXT,
                    tool_call_id TEXT,
                    tool_calls TEXT,
                    display_kind TEXT,
                    extra_flags TEXT
                );
                """
            )
            conn.execute(
                "INSERT INTO sessions VALUES (101, 'cli', 'Hermes Test', ?, 1000, 2000, NULL, 'compression', '', 0);",
                (td,),
            )
            # Child session (compression tip)
            conn.execute(
                "INSERT INTO sessions VALUES (102, 'cli', 'Hermes Cont', ?, 2001, 3000, 101, '', '', 0);",
                (td,),
            )

            # Messages for session 101: sentinel JSON, compaction summary split, rewound message
            sentinel_content = "\x00json:" + json.dumps([{"type": "text", "text": "Decoded from sentinel."}])
            conn.execute(
                "INSERT INTO messages VALUES (1, 101, 'assistant', ?, 1000, 1, 0, NULL, NULL, NULL, NULL, '');",
                (sentinel_content,),
            )

            # Compaction user row with split
            compaction_user_text = (
                "[PRIOR CONTEXT — for reference only; not a new message]\nLive user turn after compaction\n"
                "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n"
                "Summary below"
            )
            conn.execute(
                "INSERT INTO messages VALUES (2, 101, 'user', ?, 1500, 1, 0, NULL, NULL, NULL, NULL, '_compressed_summary');",
                (compaction_user_text,),
            )

            # Rewound message (active=0, compacted=0) -> should be excluded
            conn.execute(
                "INSERT INTO messages VALUES (3, 101, 'user', 'Rewound message', 1600, 0, 0, NULL, NULL, NULL, NULL, '');"
            )

            conn.commit()
            conn.close()

            adapter = HermesAdapter()
            source = SourceSpec(tool="hermes", root=root)
            refs = {r.native_id: r for r in adapter.discover(source)}
            self.assertIn("101", refs)
            self.assertIn("102", refs)

            snap = adapter.read(refs["101"], cancelled=lambda: False)
            self.assertEqual(len(snap.messages), 2)
            self.assertEqual(snap.messages[0].text, "Decoded from sentinel.")
            self.assertEqual(snap.messages[1].text, "Live user turn after compaction")

            # Check compression tip following
            conn = sqlite3.connect(state_db)
            conn.row_factory = sqlite3.Row
            tip_id, tip_cwd, chain_warnings = adapter._find_compression_tip(conn, "101")
            conn.close()
            self.assertEqual(tip_id, "102")

    def test_omp_ingestion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sessions_dir = root / "sessions"
            sessions_dir.mkdir(parents=True)

            # Main conversation JSONL
            main_file = sessions_dir / "main_session.jsonl"
            lines = [
                json.dumps({"type": "title", "title": "Fixed Title Slot"}),
                json.dumps({"type": "session", "id": "omp-1", "cwd": td, "timestamp": 1000, "title": "Old Title", "version": 1}),
                json.dumps({"type": "message", "id": "m1", "parentId": None, "timestamp": 1000, "message": {"role": "user", "content": "Hello OMP"}}),
                json.dumps({"type": "message", "id": "m2", "parentId": "m1", "timestamp": 1100, "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello User"}]}}),
            ]
            main_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            # Secondary advisor JSONL
            advisor_file = sessions_dir / "__advisor.jsonl"
            adv_lines = [
                json.dumps({"type": "session", "id": "omp-adv", "cwd": td, "timestamp": 1000}),
                json.dumps({"type": "message", "id": "a1", "message": {"role": "assistant", "content": "Advisor advice"}}),
            ]
            advisor_file.write_text("\n".join(adv_lines) + "\n", encoding="utf-8")

            adapter = OmpAdapter()
            source = SourceSpec(tool="omp", root=root)
            refs = list(adapter.discover(source))
            self.assertEqual(len(refs), 2)

            for r in refs:
                snap = adapter.read(r, cancelled=lambda: False)
                if snap.ref.native_id == "omp-1":
                    self.assertEqual(snap.title, "Fixed Title Slot")
                    self.assertEqual(snap.kind, "conversation")
                    self.assertEqual(len(snap.messages), 2)
                    self.assertEqual(snap.messages[0].text, "Hello OMP")
                    self.assertEqual(snap.messages[1].text, "Hello User")
                elif snap.ref.native_id == "omp-adv":
                    self.assertEqual(snap.kind, "advisor")

    def test_dsh_ingestion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sess_dir = root / "sessions" / "encoded-cwd" / "dsh-session-1"
            sess_dir.mkdir(parents=True)

            zst_file = sess_dir / "session.v3.jsonl.zstd"

            # Create uncompressed JSONL content
            lines = [
                json.dumps({"type": "session", "data": {"id": "dsh-session-1", "cwd": td, "createdAt": 1000, "seedLength": 2}}),
                json.dumps({"type": "session/title", "seq": 0, "data": {"title": "DSH Title"}}),
                # Seeded user message
                json.dumps({"type": "user/message", "seq": 1, "data": {"id": "u0", "content": "Seeded instruction", "source": {"kind": "user"}}}),
                json.dumps({"type": "session/end-seed", "seq": 2}),
                # Real user turn
                json.dumps({"type": "user/message", "seq": 3, "data": {"id": "u1", "content": [{"type": "image", "data": "not searchable"}, {"type": "text", "text": "How does DSH work?"}], "source": {"kind": "user"}}}),
                json.dumps({"type": "assistant/message", "seq": 4, "data": {"message": {"id": "a1", "content": [{"type": "thinking", "thinking": "not a reply"}, {"type": "text", "text": "DSH uses zstd storage."}]}}}),
            ]
            raw_bytes = ("\n".join(lines) + "\n").encode("utf-8")

            # Compress with zstandard
            cctx = zstd.ZstdCompressor()
            zst_file.write_bytes(cctx.compress(raw_bytes))

            adapter = DshAdapter()
            source = SourceSpec(tool="dsh", root=root)
            refs = list(adapter.discover(source))
            self.assertEqual(len(refs), 1)

            snap = adapter.read(refs[0], cancelled=lambda: False)
            self.assertEqual(snap.title, "DSH Title")
            self.assertEqual(snap.client, "dsh")
            self.assertEqual(snap.text_state, "native")
            self.assertEqual(snap.warnings, ())
            self.assertEqual(len(snap.messages), 3)
            self.assertEqual(snap.messages[0].text, "Seeded instruction")
            self.assertIn("inherited", snap.messages[0].flags)
            self.assertEqual(snap.messages[1].text, "How does DSH work?")
            self.assertNotIn("inherited", snap.messages[1].flags)
            self.assertEqual(snap.messages[2].text, "DSH uses zstd storage.")



if __name__ == "__main__":
    unittest.main()
