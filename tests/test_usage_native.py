from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import zstandard as zstd

from ai_session_hub.adapters.dsh import DshAdapter
from ai_session_hub.adapters.hermes import HermesAdapter
from ai_session_hub.adapters.omp import OmpAdapter
from ai_session_hub.models import SourceSpec


class TestNativeUsage(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def hermes_database(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.root / "state.db")
        conn.executescript("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, model TEXT, billing_provider TEXT,
                input_tokens INTEGER, output_tokens INTEGER,
                cache_read_tokens INTEGER, cache_write_tokens INTEGER,
                reasoning_tokens INTEGER, estimated_cost_usd REAL,
                actual_cost_usd REAL, cost_status TEXT, cost_source TEXT
            );
            CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, content TEXT);
        """)
        return conn

    def read_hermes(self, session_id: str = "s"):
        adapter = HermesAdapter()
        ref = next(ref for ref in adapter.discover(SourceSpec("hermes", self.root)) if ref.native_id == session_id)
        return adapter.read(ref, lambda: False)

    def test_hermes_model_residual_excludes_auxiliary_counters(self) -> None:
        with closing(self.hermes_database()) as conn:
            conn.execute("INSERT INTO sessions VALUES ('s','initial','p',100,40,20,10,15,2,0,'estimated','official_docs_snapshot')")
            conn.executescript("""
                CREATE TABLE session_model_usage (
                    session_id TEXT, model TEXT, billing_provider TEXT,
                    input_tokens INTEGER, output_tokens INTEGER,
                    cache_read_tokens INTEGER, cache_write_tokens INTEGER,
                    reasoning_tokens INTEGER, estimated_cost_usd REAL,
                    actual_cost_usd REAL, cost_status TEXT, cost_source TEXT,
                    billing_base_url TEXT, billing_mode TEXT, task TEXT,
                    first_seen REAL, last_seen REAL
                );
            """)
            conn.executemany("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                ("s", "a", "p", 60, 20, 10, 5, 10, 1, 0, "estimated", "official_docs_snapshot", "route", "api", "", 1000, 2000),
                ("s", "b", "p", 20, 10, 5, 5, 3, .5, 0, "estimated", "official_docs_snapshot", "route", "api", "", 1000, 2000),
                ("s", "a", "p", 7, 3, 0, 0, 2, .2, 0, None, None, "route", "api", "compression", 1000, 2000),
            ])
            conn.commit()
        snap = self.read_hermes()
        self.assertEqual(sum(record.total_tokens for record in snap.usage), 180)
        self.assertEqual(sum(record.reasoning_tokens for record in snap.usage), 17)
        self.assertAlmostEqual(sum(record.cost_usd for record in snap.usage), 2.2)
        residual = next(record for record in snap.usage if record.source_id == "session:residual")
        self.assertEqual((residual.input_tokens, residual.output_tokens, residual.total_tokens), (25, 10, 35))
        self.assertIsNone(residual.model)
        self.assertTrue(all(record.timestamp_ms is None and record.granularity == "session" for record in snap.usage))
        self.assertEqual(snap.usage, self.read_hermes().usage)

    def test_hermes_cost_defaults_are_not_actual_spend(self) -> None:
        with closing(self.hermes_database()) as conn:
            conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                ("unknown", "m", "p", 0, 0, 0, 0, 0, 0, 0, "unknown", None),
                ("free", "m", "p", 0, 0, 0, 0, 0, 0, 0, "actual", "provider_cost_api"),
                ("estimated", "m", "p", 10, 5, 0, 0, 2, .2, 0, None, None),
                ("unverified", "m", "p", 10, 5, 0, 0, 2, 0, .4, "actual", "official_docs_snapshot"),
            ])
            conn.commit()
        unknown = self.read_hermes("unknown").usage[0]
        self.assertEqual(unknown.total_tokens, 0)
        self.assertIsNone(unknown.cost_usd)
        free = self.read_hermes("free").usage[0]
        self.assertEqual((free.cost_usd, free.cost_kind), (0, "actual"))
        estimate = self.read_hermes("estimated").usage[0]
        self.assertEqual((estimate.cost_usd, estimate.cost_kind), (.2, "estimated"))
        unverified = self.read_hermes("unverified")
        self.assertIsNone(unverified.usage[0].cost_usd)
        self.assertTrue(any("provenance" in warning for warning in unverified.warnings))

    def test_hermes_missing_components_and_unknown_model_schema(self) -> None:
        with closing(self.hermes_database()) as conn:
            conn.execute("INSERT INTO sessions VALUES ('s',NULL,NULL,10,5,NULL,NULL,NULL,NULL,NULL,NULL,NULL)")
            conn.execute("CREATE TABLE session_model_usage (session_id TEXT, unsupported TEXT)")
            conn.commit()
        snap = self.read_hermes()
        self.assertEqual(snap.usage[0].output_tokens, 5)
        self.assertIsNone(snap.usage[0].input_tokens)
        self.assertIsNone(snap.usage[0].total_tokens)
        self.assertIsNone(snap.usage[0].model)
        self.assertIsNone(snap.usage[0].cache_read_tokens)
        self.assertTrue(any("schema" in warning for warning in snap.warnings))

    def write_omp(self, filename: str, records: list[dict]) -> Path:
        path = self.root / "sessions" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def omp_message(entry_id: str, cost: float, timestamp: int = 1_800_000_002_000) -> dict:
        return {
            "type": "message", "id": entry_id, "timestamp": timestamp,
            "message": {
                "role": "assistant", "model": "m", "provider": "p", "content": "searchable reply",
                "usage": {"input": 10, "output": 20, "cacheRead": 30, "cacheWrite": 5,
                          "reasoningTokens": 12, "totalTokens": 65, "cost": {"total": cost}},
            },
        }

    def test_omp_fork_tokens_not_recharged_and_tool_rollup_excluded(self) -> None:
        original = self.omp_message("inherited", .5, 1_800_000_000_000)
        self.write_omp("parent.jsonl", [{"type": "session", "id": "parent"}, original])
        inherited = self.omp_message("inherited", 0, 1_800_000_000_000)
        own = self.omp_message("own", 0)
        auxiliary = {
            "type": "model_usage", "id": "title", "timestamp": 1_800_000_003_000,
            "model": "tiny", "provider": "p", "usage": {
                "input": 2, "output": 3, "cacheRead": 0, "cacheWrite": 0,
                "totalTokens": 5, "cost": {"total": .01},
            },
        }
        self.write_omp("fork.jsonl", [
            {"type": "session", "id": "fork", "parentSession": "parent", "timestamp": 1_800_000_001_000},
            inherited, own, own, auxiliary,
            {"type": "message", "id": "task-rollup", "message": {
                "role": "toolResult", "toolName": "task", "details": {"usage": original["message"]["usage"]},
            }},
        ])
        adapter = OmpAdapter()
        refs = {ref.native_id: ref for ref in adapter.discover(SourceSpec("omp", self.root))}
        parent = adapter.read(refs["parent"], lambda: False)
        fork = adapter.read(refs["fork"], lambda: False)
        self.assertEqual(sum(record.total_tokens for record in parent.usage + fork.usage), 135)
        self.assertEqual({record.source_id for record in fork.usage}, {"own", "title"})
        own_usage = next(record for record in fork.usage if record.source_id == "own")
        self.assertEqual((own_usage.input_tokens, own_usage.output_tokens, own_usage.reasoning_tokens), (45, 20, 12))
        self.assertEqual((own_usage.cost_usd, own_usage.cost_kind), (0, "estimated"))
        inherited_text = next(message for message in fork.messages if message.source_id == "inherited")
        self.assertEqual(inherited_text.text, "searchable reply")
        self.assertIn("inherited", inherited_text.flags)

    def test_omp_missing_parent_does_not_charge_pre_fork_usage(self) -> None:
        self.write_omp("fork.jsonl", [
            {"type": "session", "id": "fork", "parentSession": "missing", "timestamp": 1_800_000_001_000},
            self.omp_message("old", 0, 1_800_000_000_000), self.omp_message("new", .5),
        ])
        adapter = OmpAdapter()
        ref = next(adapter.discover(SourceSpec("omp", self.root)))
        snap = adapter.read(ref, lambda: False)
        self.assertEqual([record.source_id for record in snap.usage], ["new"])
        self.assertEqual([message.source_id for message in snap.messages], ["old", "new"])
        self.assertTrue(any("lineage" in warning for warning in snap.warnings))

    def test_omp_tool_only_generation_has_usage_without_prose(self) -> None:
        event = self.omp_message("tool-only", .5)
        event["message"]["content"] = [{"type": "toolCall", "name": "read", "arguments": {}}]
        del event["message"]["usage"]["cost"]
        self.write_omp("tools.jsonl", [{"type": "session", "id": "tools"}, event])
        adapter = OmpAdapter()
        ref = next(adapter.discover(SourceSpec("omp", self.root)))
        snap = adapter.read(ref, lambda: False)
        self.assertEqual(snap.messages, ())
        self.assertEqual(snap.usage[0].total_tokens, 65)
        self.assertIsNone(snap.usage[0].cost_usd)

    def write_dsh(self, records: list[dict]):
        path = self.root / "sessions" / "project" / "dsh-session" / "session.v3.jsonl.zstd"
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = "\n".join(json.dumps(record) for record in records).encode("utf-8")
        path.write_bytes(zstd.ZstdCompressor().compress(raw))
        adapter = DshAdapter()
        ref = next(adapter.discover(SourceSpec("dsh", self.root)))
        return adapter.read(ref, lambda: False)

    @staticmethod
    def dsh_usage(size: int = 10) -> dict:
        return {"inputTokens": size, "outputTokens": 20, "cacheReadTokens": 30,
                "cacheWriteTokens": 5, "reasoningTokens": 12}

    @classmethod
    def dsh_message(cls, seq: int, identity: str, usage: dict | None = None) -> dict:
        return {"type": "assistant/message", "seq": seq, "time": 1_800_000_000_000 + seq,
                "data": {"message": {"id": identity, "content": [{"type": "text", "text": identity}],
                                     "source": {"kind": "model", "provider": "p", "model": "m"}},
                         "usage": usage if usage is not None else cls.dsh_usage()}}

    def test_dsh_last_seed_marker_stream_attempt_and_compaction(self) -> None:
        own = self.dsh_message(5, "own")
        own["data"]["stream"] = [{"chunk": {"type": "usage", "usage": self.dsh_usage(999)}}]
        attempt = {"type": "assistant/attempt", "seq": 6, "time": 1_800_000_000_006, "data": {
            "stream": [{"chunk": {"type": "usage", "usage": self.dsh_usage(999)}},
                       {"chunk": {"type": "usage", "usage": self.dsh_usage(2)}}],
        }}
        snap = self.write_dsh([
            {"type": "session", "id": "dsh-session", "isSeeded": True},
            self.dsh_message(0, "old"),
            {"type": "session/end-seed", "seq": 1, "data": {"inherited": True}},
            self.dsh_message(2, "also-old"),
            {"type": "session/end-seed", "seq": 4, "data": {"inherited": True}},
            own, own, attempt, attempt,
            {"type": "compaction/summary", "seq": 7, "time": 1_800_000_000_007, "data": {
                "model": "compression", "provider": "p", "usage": self.dsh_usage(3),
            }},
            {"type": "session/end-seed", "seq": 8, "data": {}},
        ])
        self.assertEqual(sum(record.total_tokens for record in snap.usage), 180)
        self.assertEqual(sum(record.reasoning_tokens for record in snap.usage), 36)
        self.assertEqual({record.model for record in snap.usage}, {"m", "compression", None})
        self.assertTrue(all(record.cost_usd is None for record in snap.usage))
        self.assertEqual([message.text for message in snap.messages], ["old", "also-old", "own"])
        self.assertIn("inherited", snap.messages[0].flags)
        self.assertIn("inherited", snap.messages[1].flags)
        self.assertNotIn("inherited", snap.messages[2].flags)
        self.assertEqual(next(record for record in snap.usage if record.model == "m").timestamp_ms, 1_800_000_000_005)

    def test_dsh_legacy_seed_cut_counts_events_not_messages(self) -> None:
        snap = self.write_dsh([
            {"type": "session", "id": "dsh-session", "seedLength": 2},
            {"type": "session/title", "seq": 0, "data": {"title": "Synthetic"}},
            self.dsh_message(1, "parent"), self.dsh_message(2, "child"),
        ])
        self.assertEqual([record.source_id for record in snap.usage], ["assistant/message:child"])
        self.assertEqual([message.text for message in snap.messages], ["parent", "child"])
        self.assertIn("inherited", snap.messages[0].flags)
        self.assertNotIn("inherited", snap.messages[1].flags)

    def test_dsh_unknown_seed_retains_prose_not_billing(self) -> None:
        snap = self.write_dsh([
            {"type": "session", "id": "dsh-session", "isSeeded": True},
            self.dsh_message(0, "retained"),
            {"type": "session/end-seed", "seq": 1, "data": {}},
        ])
        self.assertEqual(snap.usage, ())
        self.assertEqual(snap.messages[0].text, "retained")
        self.assertIn("partial", snap.messages[0].flags)
        self.assertTrue(snap.warnings)

    def test_dsh_native_total_preserves_unknown_cache_and_date(self) -> None:
        record = self.dsh_message(0, "total", {"inputTokens": 10, "outputTokens": 20, "totalTokens": 65})
        del record["time"]
        snap = self.write_dsh([{"type": "session", "id": "dsh-session"}, record])
        usage = snap.usage[0]
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.total_tokens), (45, 20, 65))
        self.assertIsNone(usage.cache_read_tokens)
        self.assertIsNone(usage.cache_write_tokens)
        self.assertIsNone(usage.reasoning_tokens)
        self.assertIsNone(usage.timestamp_ms)


if __name__ == "__main__":
    unittest.main()
