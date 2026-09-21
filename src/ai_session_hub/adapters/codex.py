from __future__ import annotations

import json
import re
import shutil
import sqlite3
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable

from ai_session_hub.adapters import register_adapter
from ai_session_hub.models import (
    LaunchSpec,
    MessageRecord,
    SessionRef,
    SessionSnapshot,
    SourceSpec,
    Unavailable,
    UsageRecord,
    canonical_session_key,
)
from ai_session_hub.source_io import (
    clean_display_text,
    get_db_and_wal_stamp,
    get_file_stat_stamp,
    iter_jsonl,
    normalize_timestamp_ms,
    open_ro_sqlite,
)
from ai_session_hub.usage import counter


_TOKEN_FIELDS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cached_input_tokens": "cache_read_tokens",
    "cache_write_input_tokens": "cache_write_tokens",
    "reasoning_output_tokens": "reasoning_tokens",
    "total_tokens": "total_tokens",
}


class _CodexUsage:
    """Turn repeated token-count snapshots into non-overlapping native facts."""

    def __init__(self, warnings: list[str]) -> None:
        self.warnings = warnings
        self.records: list[UsageRecord] = []
        self.previous: dict[str, int] | None = None
        self.turn_id: str | None = None
        self.seen_turns: set[str] = set()
        self.last_charge_turn: str | None = None

    def _decode(self, raw: object, source_id: str) -> dict[str, int]:
        if not isinstance(raw, dict):
            return {}
        values: dict[str, int] = {}
        for native, field in _TOKEN_FIELDS.items():
            value = counter(raw.get(native))
            if value is not None:
                values[field] = value
            elif native in raw:
                self.warnings.append(f"Codex usage {source_id}: invalid {native}; usage is partial")
        if "total_tokens" not in raw and "input_tokens" in values and "output_tokens" in values:
            total = counter(values["input_tokens"] + values["output_tokens"])
            if total is not None:
                values["total_tokens"] = total
        if all(field in values for field in ("input_tokens", "output_tokens", "total_tokens")):
            if values["input_tokens"] + values["output_tokens"] != values["total_tokens"]:
                self.warnings.append(f"Codex usage {source_id}: native total differs from input plus output")
        return values

    def observe(
        self, info: object, line_no: int, timestamp_ms: int | None,
        model: str | None, provider: str | None, turn_id: str | None,
        *, inherited: bool = False,
    ) -> None:
        if not isinstance(info, dict):
            return  # Idle/rate-limit ticks can have info=null.
        source_id = f"token-count:{turn_id or 'unknown-turn'}:{line_no}"
        current = self._decode(info.get("total_token_usage"), source_id)
        last = self._decode(info.get("last_token_usage"), source_id)
        if not current:
            if last:
                # A tick may repeat last_token_usage indefinitely. Without either
                # cumulative progress or a request identity it is not a new charge.
                self.warnings.append(f"Codex usage {source_id}: last usage has no cumulative counter; charge identity is unknown")
            return

        previous = self.previous
        if inherited:
            self.previous = current if previous is None else previous | current
            return
        if turn_id is not None and turn_id != self.turn_id:
            if turn_id in self.seen_turns:
                return  # Replayed completed turn, not a new reset epoch.
            self.seen_turns.add(turn_id)
            self.turn_id = turn_id
        capacity = counter(info.get("model_context_window"))
        components = ("input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens")
        if capacity and current.get("total_tokens") == capacity and all(
            current.get(field) == 0 and last.get(field) == 0 for field in components
        ):
            # Native fill_to_context_window records capacity after a context-limit
            # error, not consumed tokens. Keep its baseline for later increments.
            self.previous = current
            self.warnings.append("Codex synthetic context-capacity update excluded from usage")
            return
        if previous is None:
            delta = current
        elif any(value < previous[field] for field, value in current.items() if field in previous):
            # A reset is distinguishable from a repeated older snapshot only
            # when a new native turn identifies it. Otherwise keep the prior
            # baseline and expose the uncertainty rather than charge it again.
            self.warnings.append(f"Codex usage {source_id}: cumulative counters reset; intervening usage is unknown")
            if turn_id is not None and turn_id != self.last_charge_turn:
                delta = {field: value for field, value in last.items() if field in current and value <= current[field]}
                self.previous = current
                if delta:
                    self.records.append(UsageRecord(source_id=source_id, timestamp_ms=timestamp_ms, model=model, provider=provider, **delta))
                    self.last_charge_turn = turn_id
            return
        else:
            delta = {field: value - previous[field] for field, value in current.items() if field in previous}
            # Newly present components cannot be allocated to just this call.
            if current.keys() - previous.keys():
                self.warnings.append(f"Codex usage {source_id}: newly reported cumulative components have unknown allocation")
        self.previous = current if previous is None else previous | current
        if previous is not None and not any(delta.values()):
            return  # Unchanged cumulative counters identify repeated idle ticks.
        self.last_charge_turn = turn_id

        # Separate the current call from an earlier unattributed cumulative prefix.
        # Cache and reasoning are already subsets of input/output, not extra tokens.
        can_split = (
            "total_tokens" in last
            and "total_tokens" in delta
            and last["total_tokens"] <= delta["total_tokens"]
            and delta.keys() <= last.keys()
            and all(value <= delta[field] for field, value in last.items() if field in delta)
        )
        if can_split:
            current_call = last
            prior = {field: value - current_call[field] for field, value in delta.items() if field in current_call}
            if any(prior.values()):
                self.records.append(UsageRecord(source_id=f"{source_id}:prior", granularity="session", **prior))
            self.records.append(UsageRecord(source_id=source_id, timestamp_ms=timestamp_ms, model=model, provider=provider, **current_call))
            for field, value in current_call.items():
                if field not in current:
                    # A partial cumulative snapshot may omit a component that
                    # the identified last call still reports. Advance an exact
                    # baseline only when no earlier calls are hidden in this delta.
                    baseline = self.previous.get(field)
                    advanced = counter(baseline + value) if baseline is not None and not prior["total_tokens"] else None
                    if advanced is None:
                        self.previous.pop(field, None)
                    else:
                        self.previous[field] = advanced
        else:
            # No reliable per-call decomposition: retain the native aggregate,
            # without attributing all earlier spend to the latest model/day.
            self.records.append(UsageRecord(source_id=source_id, granularity="session", **delta))


class CodexAdapter:
    """Adapter for Codex CLI sessions (JSONL rollouts + state/history SQLite)."""

    def _find_highest_sqlite(self, root: Path, prefix: str) -> Path | None:
        matches = list(root.glob(f"{prefix}_*.sqlite"))
        if not matches:
            return None

        def extract_version(p: Path) -> int:
            m = re.search(r"(\d+)", p.stem)
            return int(m.group(1)) if m else -1

        return max(matches, key=extract_version)

    def discover(self, source: SourceSpec) -> Iterable[SessionRef]:
        root = source.canonical_root
        if not root.exists():
            return

        state_db = self._find_highest_sqlite(root, "state")
        history_db = self._find_highest_sqlite(root, "thread_history")

        # Read known thread metadata from state_db if present
        threads_meta: dict[str, dict[str, Any]] = {}
        if state_db and state_db.is_file():
            try:
                conn = open_ro_sqlite(state_db)
                cols = {row["name"] for row in conn.execute("PRAGMA table_info(threads);").fetchall()}
                query_cols = ["id"]
                for c in ["rollout_path", "cwd", "name", "title", "created_at", "updated_at", "archived", "history_mode"]:
                    if c in cols:
                        query_cols.append(c)
                rows = conn.execute(f"SELECT {','.join(query_cols)} FROM threads;").fetchall()
                for r in rows:
                    threads_meta[r["id"]] = dict(r)
                conn.close()
            except Exception:
                pass

        # Scan sessions/ and archived_sessions/
        seen_ids: set[str] = set()
        for folder_name in ["sessions", "archived_sessions", "home/sessions"]:
            folder = (root / folder_name).resolve()
            if not folder.exists():
                continue
            for jsonl_path in folder.glob("**/*.jsonl"):
                # Fast check header for session_id
                session_id = None
                try:
                    with jsonl_path.open("r", encoding="utf-8", errors="ignore") as f:
                        first_line = f.readline()
                        if first_line:
                            data = json.loads(first_line)
                            if data.get("type") == "session_meta":
                                payload = data.get("payload", {})
                                session_id = payload.get("id") or payload.get("session_id")
                except Exception:
                    pass

                # Fallback: extract UUID from filename if possible
                if not session_id:
                    uuid_match = re.search(
                        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                        jsonl_path.stem,
                        re.IGNORECASE,
                    )
                    if uuid_match:
                        session_id = uuid_match.group(1)
                    else:
                        session_id = jsonl_path.stem

                seen_ids.add(session_id)
                locators: list[Path] = [jsonl_path]
                if state_db:
                    locators.append(state_db)
                if history_db:
                    locators.append(history_db)

                # Composite revision
                rev_parts = [get_file_stat_stamp(jsonl_path) or "missing"]
                if state_db:
                    rev_parts.append(get_db_and_wal_stamp(state_db) or "missing")
                if history_db:
                    rev_parts.append(get_db_and_wal_stamp(history_db) or "missing")
                revision = "codex-v2:" + "|".join(rev_parts)

                yield SessionRef(
                    key=canonical_session_key(source.tool, root, session_id),
                    source=source,
                    native_id=session_id,
                    locators=tuple(locators),
                    revision=revision,
                )

        # Also yield state_db threads that might not have a JSONL file
        for tid, meta in threads_meta.items():
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            locators = []
            if state_db:
                locators.append(state_db)
            if history_db:
                locators.append(history_db)
            rev_parts = []
            if state_db:
                rev_parts.append(get_db_and_wal_stamp(state_db) or "missing")
            if history_db:
                rev_parts.append(get_db_and_wal_stamp(history_db) or "missing")
            revision = "codex-v2:" + "|".join(rev_parts)

            yield SessionRef(
                key=canonical_session_key(source.tool, root, tid),
                source=source,
                native_id=tid,
                locators=tuple(locators),
                revision=revision,
            )

    def read(self, ref: SessionRef, cancelled: Callable[[], bool]) -> SessionSnapshot:
        root = ref.source.canonical_root
        state_db = self._find_highest_sqlite(root, "state")
        history_db = self._find_highest_sqlite(root, "thread_history")

        # Read state metadata
        state_meta: dict[str, Any] = {}
        if state_db and state_db.is_file():
            try:
                conn = open_ro_sqlite(state_db)
                cols = {row["name"] for row in conn.execute("PRAGMA table_info(threads);").fetchall()}
                query_cols = ["id"]
                for c in ["rollout_path", "cwd", "name", "title", "created_at", "updated_at", "archived", "history_mode", "thread_source", "agent_role", "tokens_used"]:
                    if c in cols:
                        query_cols.append(c)
                row = conn.execute(f"SELECT {','.join(query_cols)} FROM threads WHERE id = ?;", (ref.native_id,)).fetchone()
                if row:
                    state_meta = dict(row)
                conn.close()
            except Exception:
                pass

        # Find JSONL file locator
        jsonl_path = None
        for loc in ref.locators:
            if loc.suffix == ".jsonl" and loc.is_file():
                jsonl_path = loc
                break
            elif loc.suffix == ".jsonl" and not loc.is_file():
                try:
                    rel = loc.relative_to(root / "sessions")
                    alt = root / "home" / "sessions" / rel
                    if alt.is_file():
                        jsonl_path = alt
                        break
                except Exception:
                    pass

        if not jsonl_path and state_meta.get("rollout_path"):
            r_path = Path(state_meta["rollout_path"])
            if r_path.is_file():
                jsonl_path = r_path
            else:
                try:
                    rel = r_path.relative_to(root / "sessions")
                    alt = root / "home" / "sessions" / rel
                    if alt.is_file():
                        jsonl_path = alt
                except Exception:
                    pass

        history_mode = state_meta.get("history_mode", "legacy")
        is_archived = bool(state_meta.get("archived", False))
        if not is_archived and jsonl_path and "archived_sessions" in str(jsonl_path):
            is_archived = True

        warnings: list[str] = []
        messages: list[MessageRecord] = []
        usage = _CodexUsage(warnings)
        usage_model = None
        usage_turn = None
        usage_provider = None
        inherited_until = 0
        unknown_inheritance = False

        # Read from thread_history if available
        db_messages: list[MessageRecord] = []
        projection_offset = 0
        projection_ordinal = 0
        if history_db and history_db.is_file():
            try:
                conn = open_ro_sqlite(history_db)
                # Check projection state
                try:
                    proj = conn.execute(
                        "SELECT next_rollout_byte_offset, next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id = ?;",
                        (ref.native_id,),
                    ).fetchone()
                    if proj:
                        projection_offset = proj[0]
                        projection_ordinal = proj[1]
                except Exception:
                    pass

                # Read items
                items = conn.execute(
                    """
                    SELECT turn_id, item_id, rollout_ordinal, created_at_ms, item_type, item_json
                    FROM thread_items
                    WHERE thread_id = ?
                    ORDER BY rollout_ordinal ASC, turn_id ASC, item_id ASC;
                    """,
                    (ref.native_id,),
                ).fetchall()

                for row in items:
                    item_type = row["item_type"]
                    item_json_raw = row["item_json"]
                    try:
                        item_data = json.loads(item_json_raw)
                    except Exception:
                        continue

                    text = ""
                    role = None
                    if item_type == "userMessage":
                        role = "user"
                        content = item_data.get("content", [])
                        if isinstance(content, list):
                            parts = []
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    parts.append(block.get("text", ""))
                                elif isinstance(block, str):
                                    parts.append(block)
                            text = "\n".join(parts)
                        elif isinstance(content, str):
                            text = content
                    elif item_type == "agentMessage":
                        role = "assistant"
                        text = item_data.get("text", "")

                    if role and text:
                        db_messages.append(
                            MessageRecord(
                                source_id=row["item_id"],
                                parent_id=None,
                                ordinal=row["rollout_ordinal"],
                                role=role,
                                text=clean_display_text(text),
                                timestamp_ms=row["created_at_ms"],
                                flags=(),
                            )
                        )
                conn.close()
            except Exception as e:
                warnings.append(f"History DB read error: {e}")

        # Read JSONL if present
        jsonl_messages: list[MessageRecord] = []
        file_header_id = None
        file_cwd = None
        started_ms = None
        updated_ms = None
        rollout_idx = 0

        if jsonl_path and jsonl_path.is_file():
            for line_no, obj, line_warnings in iter_jsonl(jsonl_path, cancelled):
                warnings.extend(line_warnings)
                msg_type = obj.get("type")
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue

                if msg_type == "session_meta":
                    file_header_id = payload.get("id") or payload.get("session_id")
                    file_cwd = payload.get("cwd")
                    started_ms = normalize_timestamp_ms(payload.get("timestamp"))
                    provider = payload.get("model_provider")
                    usage_provider = provider if isinstance(provider, str) and provider else None
                    inherited_until = max(
                        counter(payload.get("forked_from_ordinal_exclusive")) or 0,
                        counter(payload.get("subagent_history_start_ordinal")) or 0,
                    )
                    unknown_inheritance = bool(payload.get("forked_from_id")) and not inherited_until
                    if unknown_inheritance:
                        warnings.append("Codex usage: fork inheritance boundary is unknown; copied usage is not charged")
                    continue

                ts = normalize_timestamp_ms(obj.get("timestamp") or payload.get("timestamp"))
                if ts:
                    updated_ms = ts

                if msg_type == "turn_context" or (msg_type == "event_msg" and payload.get("type") == "task_started"):
                    turn = payload.get("turn_id")
                    if isinstance(turn, str) and turn:
                        usage_turn = turn
                if msg_type == "turn_context":
                    model = payload.get("model")
                    usage_model = model if isinstance(model, str) and model else None
                    provider = payload.get("model_provider")
                    if isinstance(provider, str) and provider:
                        usage_provider = provider
                elif msg_type == "event_msg" and payload.get("type") == "token_count":
                    usage.observe(
                        payload.get("info"), line_no, ts, usage_model, usage_provider, usage_turn,
                        inherited=unknown_inheritance or line_no - 1 < inherited_until,
                    )

                # Extract messages
                # 1. response_item
                if msg_type == "response_item":
                    role = payload.get("role")
                    if role in ("user", "assistant"):
                        content = payload.get("content", [])
                        parts = []
                        if isinstance(content, list):
                            for b in content:
                                if isinstance(b, dict):
                                    t = b.get("text") or b.get("input_text") or b.get("output_text")
                                    if t:
                                        parts.append(t)
                                elif isinstance(b, str):
                                    parts.append(b)
                        elif isinstance(content, str):
                            parts.append(content)
                        text = "\n".join(parts)
                        if text:
                            rollout_idx += 1
                            m_id = payload.get("id") or f"resp-{rollout_idx}"
                            jsonl_messages.append(
                                MessageRecord(
                                    source_id=m_id,
                                    parent_id=None,
                                    ordinal=rollout_idx,
                                    role=role,
                                    text=clean_display_text(text),
                                    timestamp_ms=ts,
                                    flags=(),
                                )
                            )

                # 2. event_msg.item_completed
                elif msg_type == "event_msg" and payload.get("type") == "item_completed":
                    item = payload.get("item", {})
                    item_type = item.get("type")
                    role = None
                    text = ""
                    if item_type == "UserMessage":
                        role = "user"
                        c = item.get("content", [])
                        if isinstance(c, list):
                            parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("text")]
                            text = "\n".join(parts)
                        elif isinstance(c, str):
                            text = c
                    elif item_type == "AgentMessage":
                        role = "assistant"
                        text = item.get("text", "")

                    if role and text:
                        rollout_idx += 1
                        m_id = item.get("id") or f"item-{rollout_idx}"
                        jsonl_messages.append(
                            MessageRecord(
                                source_id=m_id,
                                parent_id=None,
                                ordinal=rollout_idx,
                                role=role,
                                text=clean_display_text(text),
                                timestamp_ms=ts,
                                flags=(),
                            )
                        )
        # Merge strategy
        if history_mode == "paginated" and db_messages:
            # Use database dialogue, and check for unprojected tail in JSONL
            messages = list(db_messages)
            if jsonl_messages and projection_ordinal > 0:
                # Add unprojected tail
                for m in jsonl_messages:
                    if m.ordinal >= projection_ordinal:
                        messages.append(m)
        elif db_messages and not jsonl_messages:
            messages = list(db_messages)
        else:
            # Legacy or JSONL primary: deduplicate mirrors by source_id or turn grouping
            deduped: OrderedDict[str, MessageRecord] = OrderedDict()
            for m in jsonl_messages:
                # If identical ID already exists, preserve first completed
                if m.source_id not in deduped:
                    deduped[m.source_id] = m
            messages = list(deduped.values())

        # Ensure ordinals are strictly 1..N
        final_messages: list[MessageRecord] = []
        for i, m in enumerate(messages, 1):
            final_messages.append(
                MessageRecord(
                    source_id=m.source_id,
                    parent_id=m.parent_id,
                    ordinal=i,
                    role=m.role,
                    text=m.text,
                    timestamp_ms=m.timestamp_ms,
                    flags=m.flags,
                )
            )

        # Identity validation
        if file_header_id and file_header_id != ref.native_id:
            warnings.append(f"Header ID mismatch: header={file_header_id}, state={ref.native_id}")
            usage.records.clear()

        # Title resolution
        title = state_meta.get("name") or state_meta.get("title")
        if not title:
            # First user message (up to 96 chars)
            for m in final_messages:
                if m.role == "user" and m.text.strip():
                    first_text = m.text.strip().replace("\n", " ")
                    title = first_text[:96]
                    break
        if not title:
            title = ref.native_id

        # CWD resolution
        cwd_val = state_meta.get("cwd") or file_cwd
        cwd = Path(cwd_val).resolve() if cwd_val else None

        # Timestamps
        started_ms = normalize_timestamp_ms(state_meta.get("created_at")) or started_ms
        updated_ms = normalize_timestamp_ms(state_meta.get("updated_at")) or updated_ms

        if not usage.records and not jsonl_path and not unknown_inheritance and not inherited_until:
            state_total = counter(state_meta.get("tokens_used"))
            if state_total is not None:
                usage.records.append(UsageRecord(source_id="state:tokens_used", total_tokens=state_total, granularity="session"))
        if not usage.records:
            warnings.append("Codex usage unavailable: no independently attributable native counters")
        kind = "conversation"
        if state_meta.get("agent_role") in ("subagent", "reviewer") or state_meta.get("thread_source") == "spawn":
            kind = "subagent"
        text_state = "native" if final_messages else ("metadata-only" if not jsonl_path else "partial")

        return SessionSnapshot(
            ref=ref,
            client="codex",
            title=title,
            project_id=str(cwd) if cwd else None,
            project_label=cwd.name if cwd else None,
            cwd=cwd,
            started_ms=started_ms,
            updated_ms=updated_ms,
            kind=kind,
            archived=is_archived,
            text_state=text_state,
            messages=tuple(final_messages),
            usage=tuple(usage.records),
            warnings=tuple(warnings),
        )

    def prepare_resume(self, ref: SessionRef) -> LaunchSpec | Unavailable:
        root = ref.source.canonical_root

        # Check archived
        snapshot = self.read(ref, cancelled=lambda: False)
        if snapshot.archived:
            return Unavailable(
                "archived",
                "Codex session is archived. Please unarchive it in Codex before resuming.",
            )

        # Check executable
        exe = shutil.which("codex")
        if not exe:
            return Unavailable("missing_executable", "Codex executable 'codex' not found on PATH")

        # Check cwd
        if not snapshot.cwd or not snapshot.cwd.is_dir():
            return Unavailable(
                "missing_cwd",
                f"Session working directory does not exist: {snapshot.cwd}",
            )

        # Validate native ID format (must be valid UUID)
        if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", ref.native_id, re.I):
            return Unavailable(
                "invalid_identity",
                f"Invalid Codex session UUID: {ref.native_id}",
            )

        argv = [exe, "resume", "--cd", str(snapshot.cwd)]
        if ref.source.profile:
            argv.extend(["--profile", ref.source.profile])
        argv.append(ref.native_id)

        env_overrides = {"CODEX_HOME": str(root)}

        return LaunchSpec(
            argv=tuple(argv),
            cwd=snapshot.cwd,
            env_overrides=env_overrides,
        )


register_adapter("codex", CodexAdapter())
