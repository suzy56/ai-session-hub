from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ai_session_hub.adapters.claude import ClaudeAdapter
from ai_session_hub.adapters.codex import CodexAdapter
from ai_session_hub.models import SessionSnapshot, SourceSpec


SESSION_ID = "11111111-1111-1111-1111-111111111111"


def codex_tokens(input_tokens: int, output_tokens: int, cached: int = 0, reasoning: int = 0) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached,
        "reasoning_output_tokens": reasoning,
        "total_tokens": input_tokens + output_tokens,
    }


def token_event(total: object, last: object = None) -> dict:
    return {
        "type": "event_msg",
        "timestamp": "2026-09-21T12:00:00Z",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": total, "last_token_usage": last},
        },
    }


def turn(turn_id: str, model: str = "fixture-model") -> dict:
    return {"type": "turn_context", "payload": {"turn_id": turn_id, "model": model}}


class JsonlUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def read_codex(self, entries: list[dict], *, header: dict | None = None, trailing: str = "") -> SessionSnapshot:
        folder = self.root / "sessions"
        folder.mkdir(exist_ok=True)
        path = folder / "rollout.jsonl"
        metadata = {"id": SESSION_ID, "cwd": str(self.root), "model_provider": "fixture-provider"}
        metadata.update(header or {})
        records = [{"type": "session_meta", "payload": metadata}, *entries]
        path.write_text("".join(json.dumps(record) + "\n" for record in records) + trailing, encoding="utf-8")
        adapter = CodexAdapter()
        ref = next(adapter.discover(SourceSpec(tool="codex", root=self.root)))
        return adapter.read(ref, lambda: False)

    def read_claude(self, entries: list[dict]) -> SessionSnapshot:
        folder = self.root / "projects"
        folder.mkdir(exist_ok=True)
        path = folder / f"{SESSION_ID}.jsonl"
        path.write_text("".join(json.dumps(record) + "\n" for record in entries), encoding="utf-8")
        adapter = ClaudeAdapter()
        ref = next(adapter.discover(SourceSpec(tool="claude", root=self.root)))
        return adapter.read(ref, lambda: False)

    def test_codex_idle_ticks_do_not_charge_but_identical_distinct_calls_do(self) -> None:
        first = codex_tokens(120, 25, cached=100, reasoning=5)
        total = codex_tokens(240, 50, cached=200, reasoning=10)
        snapshot = self.read_codex([
            turn("one", "first-model"),
            token_event(first, first),
            token_event(first, first),
            turn("two", "second-model"),
            token_event(first, first),  # Start-of-turn replay of the last call.
            token_event(total, first),
            token_event(total, first),
        ])
        self.assertEqual([record.total_tokens for record in snapshot.usage], [145, 145])
        self.assertEqual([record.model for record in snapshot.usage], ["first-model", "second-model"])
        self.assertEqual(sum(record.input_tokens for record in snapshot.usage), 240)
        self.assertEqual(sum(record.output_tokens for record in snapshot.usage), 50)
        self.assertEqual(sum(record.cache_read_tokens for record in snapshot.usage), 200)
        self.assertEqual(sum(record.reasoning_tokens for record in snapshot.usage), 10)
        self.assertEqual({record.provider for record in snapshot.usage}, {"fixture-provider"})
        self.assertEqual({record.cost_usd for record in snapshot.usage}, {None})
        self.assertEqual({record.cache_write_tokens for record in snapshot.usage}, {None})
        self.assertNotEqual(snapshot.usage[0].source_id, snapshot.usage[1].source_id)

    def test_codex_context_capacity_markers_do_not_charge_or_pollute_deltas(self) -> None:
        capacity = {**codex_tokens(0, 0), "total_tokens": 200000}
        marker = token_event(capacity, capacity)
        marker["payload"]["info"]["model_context_window"] = 200000
        with sqlite3.connect(self.root / "state_5.sqlite") as connection:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, tokens_used INTEGER)")
            connection.execute("INSERT INTO threads VALUES (?, 200000)", (SESSION_ID,))
        connection.close()
        self.assertEqual(self.read_codex([turn("full"), marker]).usage, ())
        call = codex_tokens(50, 10)
        cumulative = {**call, "total_tokens": 200060}
        snapshot = self.read_codex([
            turn("full"), marker,
            turn("after-full"), token_event(cumulative, call),
            turn("full-again"), marker,
            turn("after-full-again"), token_event(cumulative, call),
        ])
        self.assertEqual([record.total_tokens for record in snapshot.usage], [60, 60])
        self.assertEqual(sum(record.input_tokens for record in snapshot.usage), 100)
        self.assertTrue(all(record.granularity == "event" for record in snapshot.usage))

    def test_codex_initial_aggregate_is_not_invented_current_day_or_model(self) -> None:
        snapshot = self.read_codex([
            turn("latest", "latest-model"),
            token_event(codex_tokens(250, 50, cached=100, reasoning=20), codex_tokens(60, 10, cached=20, reasoning=5)),
        ])
        prefix, current = snapshot.usage
        self.assertEqual((prefix.total_tokens, prefix.input_tokens, prefix.output_tokens), (230, 190, 40))
        self.assertEqual((prefix.timestamp_ms, prefix.model, prefix.provider, prefix.granularity), (None, None, None, "session"))
        self.assertEqual((current.total_tokens, current.model, current.granularity), (70, "latest-model", "event"))
        self.assertEqual(current.timestamp_ms, int(datetime(2026, 9, 21, 12, tzinfo=timezone.utc).timestamp() * 1000))

    def test_codex_partial_cumulative_components_do_not_overlap_later_recovery(self) -> None:
        call = codex_tokens(50, 10, cached=20, reasoning=2)
        snapshot = self.read_codex([
            turn("one"), token_event(call, call),
            token_event({"total_tokens": 120}, call),
            token_event(codex_tokens(150, 30, cached=60, reasoning=6), call),
        ])
        self.assertEqual([record.total_tokens for record in snapshot.usage], [60, 60, 60])
        self.assertEqual(sum(record.input_tokens for record in snapshot.usage), 150)
        self.assertEqual(sum(record.output_tokens for record in snapshot.usage), 30)
        self.assertEqual(sum(record.cache_read_tokens for record in snapshot.usage), 60)

    def test_codex_reset_and_replayed_old_turn_do_not_recharge_cumulative_base(self) -> None:
        first = codex_tokens(100, 20)
        reset = codex_tokens(10, 2)
        snapshot = self.read_codex([
            turn("first"), token_event(first, first),
            turn("after-reset"), token_event(reset, reset), token_event(reset, reset),
            turn("first"), token_event(first, first),  # Replay from an earlier completed turn.
            turn("after-reset"), token_event(codex_tokens(20, 4), reset),
        ])
        self.assertEqual([record.total_tokens for record in snapshot.usage], [120, 12, 12])
        self.assertTrue(any("reset" in warning for warning in snapshot.warnings))

    def test_codex_malformed_and_partial_ticks_preserve_known_usage_and_dialogue(self) -> None:
        first = codex_tokens(100, 20)
        malformed = {"input_tokens": True, "output_tokens": -1, "total_tokens": "120"}
        snapshot = self.read_codex([
            turn("one"), token_event(first, first),
            token_event(malformed, first), token_event(None, first),
            {"type": "response_item", "payload": {"id": "reply", "role": "assistant", "content": "Still searchable"}},
            token_event(codex_tokens(150, 30), codex_tokens(50, 10)),
        ], trailing='{"type":"event_msg","payload":')
        self.assertEqual([record.total_tokens for record in snapshot.usage], [120, 60])
        self.assertEqual([message.text for message in snapshot.messages], ["Still searchable"])
        self.assertTrue(any("invalid" in warning for warning in snapshot.warnings))
        self.assertTrue(any("charge identity is unknown" in warning for warning in snapshot.warnings))

    def test_codex_source_zero_is_distinct_from_missing_usage(self) -> None:
        missing = self.read_codex([token_event(None, codex_tokens(40, 10))])
        self.assertEqual(missing.usage, ())
        zero = self.read_codex([token_event(codex_tokens(0, 0))])
        self.assertEqual([record.total_tokens for record in zero.usage], [0])
        self.assertIsNone(zero.usage[0].cost_usd)
        self.assertIsNone(zero.usage[0].cache_write_tokens)

    def test_codex_inherited_prefix_is_context_not_another_charge(self) -> None:
        inherited = codex_tokens(100, 20)
        snapshot = self.read_codex([
            turn("parent"), token_event(inherited, inherited),
            turn("child"), token_event(codex_tokens(110, 22), codex_tokens(10, 2)),
        ], header={"forked_from_id": "parent-session", "forked_from_ordinal_exclusive": 3})
        self.assertEqual([record.total_tokens for record in snapshot.usage], [12])
        unknown = self.read_codex([
            turn("child"), token_event(inherited, inherited),
        ], header={"forked_from_id": "parent-session"})
        self.assertEqual(unknown.usage, ())

    def test_codex_state_counter_is_an_undated_fallback_not_an_extra_charge(self) -> None:
        with sqlite3.connect(self.root / "state_5.sqlite") as connection:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, tokens_used INTEGER, history_mode TEXT)")
            connection.execute("INSERT INTO threads VALUES (?, 321, 'paginated')", (SESSION_ID,))
        connection.close()
        adapter = CodexAdapter()
        source = SourceSpec(tool="codex", root=self.root)
        coarse = adapter.read(next(adapter.discover(source)), lambda: False)
        self.assertEqual([record.total_tokens for record in coarse.usage], [321])
        self.assertEqual((coarse.usage[0].timestamp_ms, coarse.usage[0].model, coarse.usage[0].input_tokens), (None, None, None))
        current = codex_tokens(100, 20)
        detailed = self.read_codex([turn("one"), token_event(current, current)])
        self.assertEqual([record.total_tokens for record in detailed.usage], [120])

    def test_claude_final_usage_includes_cache_once_and_counts_tool_only_requests(self) -> None:
        initial_usage = {"input_tokens": 2, "cache_read_input_tokens": 8, "cache_creation_input_tokens": 4, "output_tokens": 1}
        first = {
            "type": "assistant", "sessionId": SESSION_ID, "uuid": "thinking-block",
            "message": {"id": "request-one", "model": "fixture-model", "content": [{"type": "thinking", "thinking": "not dialogue"}], "usage": initial_usage},
        }
        snapshot = self.read_claude([
            first,
            {"type": "assistant", "uuid": "text-block", "message": {"id": "request-one", "content": "Readable answer", "usage": {"output_tokens": 3}}},
            {"type": "assistant", "uuid": "tool-block", "message": {"id": "request-one", "content": [{"type": "tool_use", "id": "tool"}], "usage": {"output_tokens": 7}}},
            first,  # UUID replay must not replace final output with the initial 1.
            {"type": "assistant", "uuid": "other-request", "message": {"id": "request-two", "content": [{"type": "tool_use", "id": "other-tool"}], "usage": initial_usage | {"output_tokens": 7}}},
            {"type": "assistant", "sessionId": "inherited-parent", "uuid": "copied", "message": {"id": "parent-request", "usage": initial_usage}},
        ])
        self.assertEqual([record.total_tokens for record in snapshot.usage], [21, 21])
        self.assertEqual([record.input_tokens for record in snapshot.usage], [14, 14])
        self.assertEqual([record.output_tokens for record in snapshot.usage], [7, 7])
        self.assertEqual([record.cache_read_tokens for record in snapshot.usage], [8, 8])
        self.assertEqual([record.cache_write_tokens for record in snapshot.usage], [4, 4])
        self.assertEqual([message.text for message in snapshot.messages], ["Readable answer"])
        self.assertEqual({record.reasoning_tokens for record in snapshot.usage}, {None})
        self.assertEqual({record.cost_usd for record in snapshot.usage}, {None})
        self.assertEqual({record.cost_kind for record in snapshot.usage}, {"unknown"})

    def test_claude_missing_or_malformed_components_are_not_zero(self) -> None:
        snapshot = self.read_claude([
            {"type": "assistant", "uuid": "partial", "message": {"id": "partial-request", "usage": {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 5}}},
            {"type": "assistant", "uuid": "bad", "message": {"id": "bad-request", "usage": {"input_tokens": -4, "output_tokens": True, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
            {"type": "assistant", "uuid": "zero", "message": {"id": "zero-request", "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
        ])
        partial, malformed, zero = snapshot.usage
        self.assertEqual((partial.input_tokens, partial.total_tokens, partial.cache_write_tokens), (None, None, None))
        self.assertEqual((partial.output_tokens, partial.cache_read_tokens), (3, 5))
        self.assertEqual((malformed.input_tokens, malformed.output_tokens, malformed.total_tokens), (None, None, None))
        self.assertEqual((zero.input_tokens, zero.output_tokens, zero.total_tokens), (0, 0, 0))
        self.assertTrue(any("invalid token counter" in warning for warning in snapshot.warnings))


if __name__ == "__main__":
    unittest.main()
