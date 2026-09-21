from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from contextlib import closing
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
    normalize_timestamp_ms,
    open_ro_sqlite,
)
from ai_session_hub.usage import amount, counter


class HermesAdapter:
    """Adapter for Hermes CLI/agent sessions via state.db."""

    def discover(self, source: SourceSpec) -> Iterable[SessionRef]:
        root = source.canonical_root
        state_db = root / "state.db"
        if not state_db.is_file():
            return

        db_rev = get_db_and_wal_stamp(state_db) or "missing"

        try:
            with closing(open_ro_sqlite(state_db)) as conn:
                cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions);").fetchall()}
                query_cols = ["id"]
                for c in ["updated_at", "started_at"]:
                    if c in cols:
                        query_cols.append(c)
                rows = conn.execute(f"SELECT {','.join(query_cols)} FROM sessions;").fetchall()

            for r in rows:
                sid = str(r["id"])
                up_val = r["updated_at"] if "updated_at" in cols else (r["started_at"] if "started_at" in cols else 0)
                yield SessionRef(
                    key=canonical_session_key(source.tool, root, sid),
                    source=source,
                    native_id=sid,
                    locators=(state_db,),
                    revision=f"hermes-v3:{db_rev}:{up_val}",
                )
        except Exception:
            pass

    def _decode_content(self, raw_text: str, warnings: list[str]) -> tuple[str, bool]:
        """Decode Hermes message content, handling NUL-prefixed '\\x00json:' sentinels."""
        if not raw_text:
            return "", False

        if raw_text.startswith("\x00json:"):
            payload_str = raw_text[6:]
            try:
                data = json.loads(payload_str)
                if isinstance(data, list):
                    parts = []
                    for b in data:
                        if isinstance(b, dict):
                            t = b.get("text") or b.get("content")
                            if t and isinstance(t, str):
                                parts.append(t)
                        elif isinstance(b, str):
                            parts.append(b)
                    return "\n".join(parts), True
                elif isinstance(data, dict):
                    t = data.get("text") or data.get("content") or ""
                    return str(t), True
                return str(data), True
            except Exception as e:
                warnings.append(f"Malformed Hermes json sentinel: {e}")
                return raw_text, True

        return raw_text, False

    def _split_compaction_summary(self, text: str) -> str:
        """Handle _compressed_summary user rows to extract live user dialogue."""
        marker1 = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
        marker1_lead = "[PRIOR CONTEXT — for reference only; not a new message]"
        marker2 = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"

        if marker1 in text:
            parts = text.split(marker1, 1)
            live = parts[0]
            if marker1_lead in live:
                live = live.replace(marker1_lead, "")
            return live.strip()
        elif marker2 in text:
            parts = text.split(marker2, 1)
            return parts[1].strip()

        return text

    _TOKEN_COLUMNS = (
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
    )
    _USAGE_COLUMNS = (*_TOKEN_COLUMNS, "model", "billing_provider", "estimated_cost_usd",
                      "actual_cost_usd", "cost_status", "cost_source")

    @staticmethod
    def _cost(row: dict[str, Any], warnings: list[str]) -> tuple[float | None, str]:
        status, source = row.get("cost_status"), row.get("cost_source")
        actual = amount(row.get("actual_cost_usd"))
        estimated = amount(row.get("estimated_cost_usd"))
        if status == "actual" and source in ("provider_cost_api", "provider_generation_api", "custom_contract") and actual is not None:
            return actual, "actual"
        if status == "actual":
            warnings.append("Hermes actual cost lacks supported provenance; not treated as billed spend")
        # Model-usage schema defaults both amounts to 0 even when unpriced.
        # Auxiliary writers omit status but a positive estimated amount is explicit.
        if estimated is not None and (status == "estimated" or estimated > 0):
            return estimated, "estimated"
        if status not in (None, "", "unknown", "estimated", "actual"):
            warnings.append(f"Hermes unsupported cost status: {status}")
        return None, "unknown"

    @classmethod
    def _usage_record(cls, source_id: str, row: dict[str, Any], warnings: list[str]) -> UsageRecord:
        values = {name: counter(row.get(name)) for name in cls._TOKEN_COLUMNS}
        # Hermes CanonicalUsage stores uncached input; reasoning is in output.
        inputs = [values[name] for name in ("input_tokens", "cache_read_tokens", "cache_write_tokens")]
        inclusive_input = sum(inputs) if all(value is not None for value in inputs) else None
        if any(row.get(name) is not None and values[name] is None for name in cls._TOKEN_COLUMNS):
            warnings.append("Hermes invalid native token counter; affected usage remains unknown")
        if inclusive_input is None and any(value is not None for value in inputs):
            warnings.append("Hermes input/cache breakdown incomplete; inclusive input remains unknown")
        output = values["output_tokens"]
        reasoning = values["reasoning_tokens"]
        if output is not None and reasoning is not None and reasoning > output:
            warnings.append("Hermes reasoning exceeds inclusive output")
        cost, cost_kind = cls._cost(row, warnings)
        model = row.get("model")
        if not isinstance(model, str) or model in ("", "unknown"):
            model = None
            warnings.append("Hermes aggregate usage has no attributable model route")
        return UsageRecord(
            source_id=source_id, model=model,
            provider=row.get("billing_provider") if isinstance(row.get("billing_provider"), str) and row["billing_provider"] else None,
            input_tokens=inclusive_input,
            output_tokens=output, cache_read_tokens=values["cache_read_tokens"],
            cache_write_tokens=values["cache_write_tokens"], reasoning_tokens=reasoning,
            total_tokens=inclusive_input + output if inclusive_input is not None and output is not None else None,
            cost_usd=cost, cost_kind=cost_kind, granularity="session",
        )

    @classmethod
    def _read_usage(cls, conn: sqlite3.Connection, session_id: str, session: dict[str, Any],
                    warnings: list[str], cancelled: Callable[[], bool]) -> tuple[UsageRecord, ...]:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # The inspected native writer has coarse counters, not a per-call table.
        # Do not reinterpret an unknown future timeline as additive events.
        if tables & {"usage_events", "session_usage_events", "usage_timeline"}:
            warnings.append("Hermes unsupported usage timeline schema; using native coarse counters only")
        rows: list[dict[str, Any]] = []
        if "session_model_usage" in tables:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(session_model_usage)")}
            if {"session_id", "model", "input_tokens", "output_tokens"} <= cols:
                selected = [c for c in (*cls._USAGE_COLUMNS, "billing_base_url", "billing_mode", "task") if c in cols]
                rows = [dict(row) for row in conn.execute(
                    f"SELECT {','.join(selected)} FROM session_model_usage WHERE session_id=?", (session_id,)
                )]
            else:
                warnings.append("Hermes unsupported session_model_usage schema; using session counters")
        records: list[UsageRecord] = []
        main_rows = []
        for row in rows:
            if cancelled():
                break
            # task!='' is independent auxiliary work, absent from session totals.
            if not row.get("task"):
                main_rows.append(row)
            identity = json.dumps([row.get(k) for k in ("model", "billing_provider", "billing_base_url", "billing_mode", "task")], separators=(",", ":"))
            records.append(cls._usage_record("model:" + hashlib.sha256(identity.encode()).hexdigest(), row, warnings))
        residual: dict[str, Any] = {}
        for name in cls._TOKEN_COLUMNS:
            total = counter(session.get(name))
            if session.get(name) is not None and total is None:
                warnings.append("Hermes invalid session token counter; affected residual remains unknown")
            attributed = [counter(row.get(name)) for row in main_rows]
            if total is None or any(value is None for value in attributed):
                residual[name] = None
            else:
                difference = total - sum(attributed)
                if difference < 0:
                    warnings.append("Hermes model counters exceed session counters; preserving model facts without double counting")
                residual[name] = max(0, difference)
        # Reconcile costs separately within their provenance; estimated and actual
        # amounts are alternate valuations, never independent charges.
        session_cost, session_kind = cls._cost(session, warnings)
        model_costs = [cls._cost(row, warnings) for row in main_rows]
        if session_cost is not None and all(cost is not None and kind == session_kind for cost, kind in model_costs):
            remaining = max(0.0, session_cost - sum(cost for cost, _ in model_costs))
            residual["cost_status"] = session_kind
            residual["cost_source"] = session.get("cost_source")
            residual["actual_cost_usd" if session_kind == "actual" else "estimated_cost_usd"] = remaining
        elif session_cost is not None and main_rows:
            warnings.append("Hermes session/model cost coverage cannot be reconciled; retaining model costs only")
        if not main_rows:
            residual["model"] = session.get("model")
            residual["billing_provider"] = session.get("billing_provider")
        has_residual = any((residual.get(name) or 0) > 0 for name in cls._TOKEN_COLUMNS)
        has_cost_residual = any((residual.get(name) or 0) > 0 for name in ("estimated_cost_usd", "actual_cost_usd"))
        if has_residual or has_cost_residual or (not main_rows and (session_cost is not None or any(residual.get(name) is not None for name in cls._TOKEN_COLUMNS))):
            records.append(cls._usage_record("session:residual", residual, warnings))
        if records:
            warnings.append("Hermes usage is cumulative session/model accounting; no daily timestamps are recorded")
        return tuple(records)

    def read(self, ref: SessionRef, cancelled: Callable[[], bool]) -> SessionSnapshot:
        state_db = ref.locators[0]
        warnings: list[str] = []

        if not state_db.is_file():
            return SessionSnapshot(
                ref=ref,
                client="hermes",
                title=ref.native_id,
                project_id=None,
                project_label=None,
                cwd=None,
                started_ms=None,
                updated_ms=None,
                kind="conversation",
                archived=False,
                text_state="unavailable",
                messages=(),
                warnings=("state.db not found",),
            )

        session_meta: dict[str, Any] = {}
        messages: list[MessageRecord] = []
        usage: tuple[UsageRecord, ...] = ()
        conn: sqlite3.Connection | None = None

        try:
            conn = open_ro_sqlite(state_db)
            conn.execute("BEGIN")
            s_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions);").fetchall()}
            s_query_cols = ["id"]
            for c in ["source", "title", "cwd", "started_at", "ended_at", "parent_session_id", "end_reason", "model_config", "archived", *self._USAGE_COLUMNS]:
                if c in s_cols:
                    s_query_cols.append(c)

            s_row = conn.execute(
                f"SELECT {','.join(s_query_cols)} FROM sessions WHERE id = ?;",
                (ref.native_id,),
            ).fetchone()
            if s_row:
                session_meta = dict(s_row)
            usage = self._read_usage(conn, ref.native_id, session_meta, warnings, cancelled)

            # Messages
            m_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages);").fetchall()}
            m_query_cols = ["id", "role", "content"]
            for c in ["timestamp", "active", "compacted", "tool_name", "tool_call_id", "tool_calls", "display_kind", "extra_flags"]:
                if c in m_cols:
                    m_query_cols.append(c)

            where_clauses = ["session_id = ?"]
            if "active" in m_cols and "compacted" in m_cols:
                where_clauses.append("(COALESCE(active, 1) = 1 OR compacted = 1)")
            elif "active" in m_cols:
                where_clauses.append("COALESCE(active, 1) = 1")

            where_sql = " AND ".join(where_clauses)
            m_rows = conn.execute(
                f"SELECT {','.join(m_query_cols)} FROM messages WHERE {where_sql} ORDER BY id ASC;",
                (ref.native_id,),
            ).fetchall()

            # Process rows with compaction deduplication
            dedup_map: dict[tuple, dict[str, Any]] = {}

            for row in m_rows:
                if cancelled():
                    break

                role = row["role"]
                if role not in ("user", "assistant"):
                    continue

                # Check synthetic display event
                if "display_kind" in m_cols:
                    dk = row["display_kind"]
                    if dk and dk != "hidden":
                        continue

                raw_content = row["content"] or ""
                decoded_text, is_sentinel = self._decode_content(raw_content, warnings)

                # Check if tagged with _compressed_summary
                extra_flags = row["extra_flags"] if "extra_flags" in m_cols else ""
                is_compressed_summary = "_compressed_summary" in str(extra_flags)

                if is_compressed_summary and role == "user":
                    decoded_text = self._split_compaction_summary(decoded_text)
                    if not decoded_text:
                        # Pure summary without live dialogue
                        continue

                clean_text = clean_display_text(decoded_text).strip()
                if not clean_text:
                    continue

                ts = normalize_timestamp_ms(row["timestamp"]) if "timestamp" in m_cols else None
                tool_call_id = row["tool_call_id"] if "tool_call_id" in m_cols else None
                tool_calls = row["tool_calls"] if "tool_calls" in m_cols else None
                tool_name = row["tool_name"] if "tool_name" in m_cols else None
                active = row["active"] if "active" in m_cols else 1

                # Deduplication key
                dedup_key = (role, clean_text, ts, tool_call_id, tool_calls, tool_name)
                entry = {
                    "id": row["id"],
                    "role": role,
                    "text": clean_text,
                    "timestamp": ts,
                    "active": active,
                }

                if dedup_key not in dedup_map:
                    dedup_map[dedup_key] = entry
                else:
                    # Prefer active, then larger id
                    existing = dedup_map[dedup_key]
                    if (active and not existing["active"]) or (row["id"] > existing["id"]):
                        dedup_map[dedup_key] = entry

            # Sort by id ASC
            sorted_entries = sorted(dedup_map.values(), key=lambda x: x["id"])
            for i, entry in enumerate(sorted_entries, 1):
                messages.append(
                    MessageRecord(
                        source_id=str(entry["id"]),
                        parent_id=None,
                        ordinal=i,
                        role=entry["role"],
                        text=entry["text"],
                        timestamp_ms=entry["timestamp"],
                        flags=(),
                    )
                )

        except Exception as e:
            warnings.append(f"Error reading Hermes state.db: {e}")
        finally:
            if conn is not None:
                conn.close()

        # Metadata
        title = session_meta.get("title")
        if not title:
            for m in messages:
                if m.role == "user" and m.text.strip():
                    first_text = m.text.strip().replace("\n", " ")
                    title = first_text[:96]
                    break
        if not title:
            title = ref.native_id

        cwd_str = session_meta.get("cwd")
        cwd = Path(cwd_str).resolve() if cwd_str else None

        started_ms = normalize_timestamp_ms(session_meta.get("started_at"))
        updated_ms = normalize_timestamp_ms(session_meta.get("ended_at")) or started_ms
        if messages and messages[-1].timestamp_ms:
            if updated_ms is None or messages[-1].timestamp_ms > updated_ms:
                updated_ms = messages[-1].timestamp_ms

        archived = bool(session_meta.get("archived", False))

        return SessionSnapshot(
            ref=ref,
            client="hermes",
            title=title,
            project_id=str(cwd) if cwd else None,
            project_label=cwd.name if cwd else None,
            cwd=cwd,
            started_ms=started_ms,
            updated_ms=updated_ms,
            kind="conversation",
            archived=archived,
            text_state="native" if messages else "metadata-only",
            messages=tuple(messages),
            usage=usage,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _find_compression_tip(self, conn: sqlite3.Connection, start_id: str) -> tuple[str, Path | None, list[str]]:
        """Follow the native compression lineage, never a branch or delegate."""
        current_id = start_id
        visited: set[str] = set()
        s_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        m_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        columns = [name for name in ("id", "cwd", "end_reason", "ended_at", "started_at", "last_activity_at", "source", "model_config") if name in s_cols]
        final_cwd = None
        for _ in range(100):
            if current_id in visited:
                return current_id, None, ["Cycle detected in Hermes compression chain"]
            visited.add(current_id)
            raw = conn.execute(f"SELECT {','.join(columns)} FROM sessions WHERE id = ?", (current_id,)).fetchone()
            if raw is None:
                return current_id, None, ["Hermes session identity is missing"]
            row = dict(raw)
            cwd = row.get("cwd")
            final_cwd = Path(cwd).resolve() if isinstance(cwd, str) and Path(cwd).is_absolute() else None
            if row.get("end_reason") != "compression" or "parent_session_id" not in s_cols:
                return current_id, final_cwd, []
            children = []
            for raw_child in conn.execute(f"SELECT {','.join(columns)} FROM sessions WHERE parent_session_id = ?", (current_id,)):
                child = dict(raw_child)
                try:
                    config = json.loads(child.get("model_config") or "{}")
                except (TypeError, ValueError):
                    return current_id, None, ["Malformed Hermes continuation metadata"]
                if not isinstance(config, dict):
                    return current_id, None, ["Malformed Hermes continuation metadata"]
                if child.get("source") == "tool" or config.get("_branched_from") is not None or config.get("_delegate_from") is not None:
                    continue
                message_time = None
                if {"session_id", "timestamp"}.issubset(m_cols):
                    message_time = conn.execute("SELECT MAX(timestamp) FROM messages WHERE session_id = ?", (child["id"],)).fetchone()[0]
                activity = [normalize_timestamp_ms(value) for value in (child.get("last_activity_at"), message_time) if value is not None]
                recency = max((value for value in activity if value is not None), default=normalize_timestamp_ms(child.get("started_at")) or 0)
                priority = 2 if child.get("end_reason") == "compression" else (1 if child.get("ended_at") is None else 0)
                children.append(((priority, recency, normalize_timestamp_ms(child.get("started_at")) or 0, str(child["id"])), child))
            if not children:
                return current_id, final_cwd, []
            current_id = str(max(children, key=lambda item: item[0])[1]["id"])
        return current_id, None, ["Hermes compression chain exceeds 100 hops"]

    def prepare_resume(self, ref: SessionRef) -> LaunchSpec | Unavailable:
        root = ref.source.canonical_root
        state_db = root / "state.db"
        if not state_db.is_file() or not ref.locators or ref.locators[0].resolve() != state_db:
            return Unavailable("missing_source", f"Hermes state database not found: {state_db}")
        if not ref.native_id or ref.native_id != ref.native_id.strip() or ref.native_id.startswith("-") or ref.native_id.lower() in ("latest", "import"):
            return Unavailable("invalid_identity", "Hermes 恢复必须使用已验证的原生 ID，不能使用 latest 或其他选择器。")

        named_layout = root.parent.name == "profiles"
        profile = (ref.source.profile or (root.name if named_layout else "default")).strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", profile) or profile in {"hermes", "test", "tmp", "root", "sudo"}:
            return Unavailable("unsupported_profile", f"无效 Hermes profile：{profile}")
        hermes_home = root.parent.parent if named_layout else root
        effective_root = hermes_home if profile == "default" else hermes_home / "profiles" / profile
        if effective_root.resolve() != root:
            return Unavailable("unsupported_profile", "Hermes profile 与所选会话存储目录不一致。")

        exe = shutil.which("hermes")
        if not exe:
            return Unavailable("missing_executable", "Hermes executable 'hermes' not found on PATH")
        try:
            with closing(open_ro_sqlite(state_db)) as conn:
                conn.execute("BEGIN")
                tip_id, tip_cwd, chain_warnings = self._find_compression_tip(conn, ref.native_id)
        except (sqlite3.Error, OSError) as exc:
            return Unavailable("unsupported_resume", f"Failed to follow Hermes session chain: {exc}")
        if chain_warnings:
            return Unavailable("invalid_identity", "; ".join(chain_warnings))
        if not tip_id or tip_id != tip_id.strip() or tip_id.startswith("-") or tip_id.lower() in ("latest", "import"):
            return Unavailable("invalid_identity", "Hermes 压缩续接目标不是有效原生 ID。")
        if tip_cwd is None:
            return Unavailable("unknown_cwd", "Hermes 最终续接会话未记录绝对工作目录；不能使用父会话或当前目录代替。")
        if not tip_cwd.is_dir():
            return Unavailable("missing_cwd", f"Session working directory does not exist: {tip_cwd}")

        return LaunchSpec(
            argv=(exe, "--profile", profile, "--in", str(tip_cwd), "--resume", tip_id),
            cwd=tip_cwd,
            env_overrides={"HERMES_HOME": str(hermes_home)},
        )


register_adapter("hermes", HermesAdapter())
