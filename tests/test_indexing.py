from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ai_session_hub.config import load_config
from ai_session_hub.indexer import Indexer
from ai_session_hub.models import SourceSpec
from ai_session_hub.store import SearchFilters, Store


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class TestIndexing(unittest.TestCase):
    """Test incremental indexing, native identity, and source isolation."""

    def test_append_reply_incremental(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src_dir = root / "source" / "sessions"
            src_dir.mkdir(parents=True)
            db_path = root / "index.sqlite3"

            jsonl_file = src_dir / "sess-1.jsonl"
            jsonl_file.write_text(
                json.dumps({"type": "session", "id": "omp-inc-1", "cwd": td, "timestamp": 1000}) + "\n" +
                json.dumps({"type": "message", "id": "m1", "message": {"role": "user", "content": "Question 1"}}) + "\n",
                encoding="utf-8",
            )

            store = Store(db_path)
            source = SourceSpec(tool="omp", root=src_dir.parent)
            indexer = Indexer(store, [source])

            # 1. Initial scan
            indexer.scan_all(cancelled=lambda: False)
            res1 = store.search("", SearchFilters())
            self.assertEqual(res1.total_count, 1)
            snap1 = store.get_session(res1.results[0].session_key)
            self.assertIsNotNone(snap1)
            self.assertEqual(len(snap1.messages), 1)

            # 2. Append turn
            with jsonl_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "message", "id": "m2", "message": {"role": "assistant", "content": "Reply 1"}}) + "\n")

            # 3. Rescan
            indexer.scan_all(cancelled=lambda: False)
            snap2 = store.get_session(res1.results[0].session_key)
            self.assertIsNotNone(snap2)
            self.assertEqual(len(snap2.messages), 2)
            self.assertEqual(snap2.messages[1].text, "Reply 1")

            # 4. Rescan without changes: idempotent
            indexer.scan_all(cancelled=lambda: False)
            snap3 = store.get_session(res1.results[0].session_key)
            self.assertEqual(len(snap3.messages), 2)
            store.close()

    def test_rewrite_truncate_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src_dir = root / "source" / "sessions"
            src_dir.mkdir(parents=True)
            db_path = root / "index.sqlite3"

            jsonl_file = src_dir / "sess-1.jsonl"
            lines = [
                json.dumps({"type": "session", "id": "omp-rew-1", "cwd": td, "timestamp": 1000}),
                json.dumps({"type": "message", "id": "m1", "message": {"role": "user", "content": "Q1"}}),
                json.dumps({"type": "message", "id": "m2", "message": {"role": "assistant", "content": "A1"}}),
                json.dumps({"type": "message", "id": "m3", "message": {"role": "user", "content": "Q2"}}),
            ]
            jsonl_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            store = Store(db_path)
            source = SourceSpec(tool="omp", root=src_dir.parent)
            indexer = Indexer(store, [source])
            indexer.scan_all(cancelled=lambda: False)

            res = store.search("", SearchFilters())
            self.assertEqual(len(store.get_session(res.results[0].session_key).messages), 3)

            # Rewrite with only 1 message
            new_lines = [
                json.dumps({"type": "session", "id": "omp-rew-1", "cwd": td, "timestamp": 1000}),
                json.dumps({"type": "message", "id": "m1", "message": {"role": "user", "content": "Q1 only"}}),
            ]
            # Ensure mtime updates
            time_ns = os.stat(jsonl_file).st_mtime_ns + 100_000_000
            jsonl_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            os.utime(jsonl_file, ns=(time_ns, time_ns))

            indexer.scan_all(cancelled=lambda: False)
            snap = store.get_session(res.results[0].session_key)
            self.assertEqual(len(snap.messages), 1)
            self.assertEqual(snap.messages[0].text, "Q1 only")
            store.close()

    def test_malformed_record_tolerance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src_dir = root / "source" / "sessions"
            src_dir.mkdir(parents=True)
            db_path = root / "index.sqlite3"

            # File 1: clean
            f1 = src_dir / "clean.jsonl"
            f1.write_text(
                json.dumps({"type": "session", "id": "clean-1", "cwd": td}) + "\n" +
                json.dumps({"type": "message", "id": "c1", "message": {"role": "user", "content": "Clean Msg"}}) + "\n",
                encoding="utf-8",
            )

            # File 2: has a corrupted line
            f2 = src_dir / "dirty.jsonl"
            f2.write_text(
                json.dumps({"type": "session", "id": "dirty-1", "cwd": td}) + "\n" +
                "THIS IS NOT JSON CORRUPTED LINE\n" +
                json.dumps({"type": "message", "id": "d1", "message": {"role": "user", "content": "Valid Msg After Error"}}) + "\n",
                encoding="utf-8",
            )

            store = Store(db_path)
            source = SourceSpec(tool="omp", root=src_dir.parent)
            indexer = Indexer(store, [source])
            indexer.scan_all(cancelled=lambda: False)

            res = store.search("", SearchFilters())
            self.assertEqual(res.total_count, 2)

            dirty_snap = None
            clean_snap = None
            for r in res.results:
                s = store.get_session(r.session_key)
                if s and s.ref.native_id == "dirty-1":
                    dirty_snap = s
                elif s and s.ref.native_id == "clean-1":
                    clean_snap = s

            self.assertIsNotNone(clean_snap)
            self.assertEqual(len(clean_snap.messages), 1)
            self.assertIsNotNone(dirty_snap)
            self.assertEqual(len(dirty_snap.messages), 1)
            self.assertEqual(dirty_snap.messages[0].text, "Valid Msg After Error")
            self.assertTrue(len(dirty_snap.warnings) > 0)
            store.close()

    def test_wal_only_committed_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src_dir = root / "hermes_root"
            src_dir.mkdir(parents=True)
            db_path = root / "index.sqlite3"

            state_db = src_dir / "state.db"
            conn = sqlite3.connect(state_db)
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, title TEXT, cwd TEXT, started_at INTEGER, ended_at INTEGER);")
            conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id INTEGER, role TEXT, content TEXT, timestamp INTEGER);")
            conn.execute("INSERT INTO sessions VALUES (1, 'Initial Title', ?, 1000, 1000);", (td,))
            conn.execute("INSERT INTO messages VALUES (1, 1, 'user', 'Initial Question', 1000);")
            conn.commit()

            store = Store(db_path)
            source = SourceSpec(tool="hermes", root=src_dir)
            indexer = Indexer(store, [source])
            indexer.scan_all(cancelled=lambda: False)

            res1 = store.search("", SearchFilters())
            self.assertEqual(res1.total_count, 1)

            # Add change that stays in WAL (keep conn open so WAL is not auto-checkpointed)
            conn.execute("INSERT INTO sessions VALUES (2, 'WAL Title', ?, 2000, 2000);", (td,))
            conn.execute("INSERT INTO messages VALUES (2, 2, 'user', 'WAL Question', 2000);")
            conn.commit()

            # Ensure wal file exists
            wal_file = src_dir / "state.db-wal"
            self.assertTrue(wal_file.exists())

            # Rescan
            indexer.scan_all(cancelled=lambda: False)
            res2 = store.search("", SearchFilters())
            self.assertEqual(res2.total_count, 2)
            conn.close()
            store.close()

    def test_source_files_remain_unmodified(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src_dir = root / "source" / "sessions"
            src_dir.mkdir(parents=True)
            db_path = root / "index.sqlite3"

            jsonl_file = src_dir / "sess-1.jsonl"
            jsonl_file.write_text(
                json.dumps({"type": "session", "id": "omp-chk-1", "cwd": td}) + "\n" +
                json.dumps({"type": "message", "id": "m1", "message": {"role": "user", "content": "Checksum check"}}) + "\n",
                encoding="utf-8",
            )
            before_hash = file_sha256(jsonl_file)

            store = Store(db_path)
            source = SourceSpec(tool="omp", root=src_dir.parent)
            indexer = Indexer(store, [source])
            indexer.scan_all(cancelled=lambda: False)

            after_hash = file_sha256(jsonl_file)
            self.assertEqual(before_hash, after_hash, "Indexing MUST NOT modify source history files")
            store.close()

    def test_same_native_id_distinct_roots_and_unsupported_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            root1 = root / "root1" / "sessions"
            root2 = root / "root2" / "sessions"
            root1.mkdir(parents=True)
            root2.mkdir(parents=True)

            shared_uuid = "44444444-4444-4444-4444-444444444444"

            # Both roots have a session with the exact same ID
            (root1 / "sess.jsonl").write_text(
                json.dumps({"type": "session_meta", "payload": {"id": shared_uuid, "cwd": td}}) + "\n" +
                json.dumps({"type": "response_item", "payload": {"id": "m1", "role": "user", "content": [{"text": "Root 1"}]}}) + "\n",
                encoding="utf-8",
            )
            (root2 / "sess.jsonl").write_text(
                json.dumps({"type": "session_meta", "payload": {"id": shared_uuid, "cwd": td}}) + "\n" +
                json.dumps({"type": "response_item", "payload": {"id": "m2", "role": "user", "content": [{"text": "Root 2"}]}}) + "\n",
                encoding="utf-8",
            )

            store = Store(root / "index.sqlite3")
            s1 = SourceSpec(tool="codex", root=root1.parent)
            s2 = SourceSpec(tool="codex", root=root2.parent)
            unsupported = SourceSpec(tool="removed-tool", root=root / "unsupported")

            indexer = Indexer(store, [unsupported, s1, s2])
            indexer.scan_all(cancelled=lambda: False)

            res = store.search("", SearchFilters())
            self.assertEqual(res.total_count, 2)
            self.assertEqual(len({r.session_key for r in res.results}), 2)
            statuses = store.get_source_statuses()
            self.assertEqual(next(s["status"] for s in statuses if s["tool"] == "removed-tool"), "error")
            indexer.scan_all(cancelled=lambda: False)
            self.assertEqual(store.search("Root 1", SearchFilters()).total_count, 1)
            self.assertEqual(store.search("Root 2", SearchFilters()).total_count, 1)
            from unittest.mock import patch
            from ai_session_hub.adapters import get_adapter

            adapter = get_adapter("codex")
            discover = adapter.discover

            def fail_one(source):
                if source.canonical_root == s1.canonical_root:
                    raise PermissionError("synthetic unreadable source")
                return discover(source)

            with patch.object(adapter, "discover", side_effect=fail_one):
                indexer.scan_all(cancelled=lambda: False)
            self.assertEqual(store.search("Root 1", SearchFilters()).total_count, 1)
            self.assertEqual(store.search("Root 2", SearchFilters()).total_count, 1)
            failed = next(s for s in store.get_source_statuses() if s["root"] == str(s1.canonical_root))
            self.assertEqual(failed["status"], "error")
            store.close()

    def test_data_dir_overlap_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source_dir = base / "source"
            source_dir.mkdir()
            nested_data_dir = source_dir / "nested_index"

            # Create config attempting to place data_dir inside source root
            cfg_path = base / "config.toml"
            cfg_path.write_text(
                f"""
                discover_defaults = false
                [[sources]]
                tool = "codex"
                root = "{source_dir}"
                """,
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as ctx:
                load_config(config_path=cfg_path, data_dir=nested_data_dir)
            self.assertIn("overlaps", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
