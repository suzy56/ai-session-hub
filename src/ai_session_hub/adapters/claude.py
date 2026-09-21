from __future__ import annotations

import json
import re
import shutil
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
    canonical_session_key,
    UsageRecord,
)
from ai_session_hub.source_io import (
    clean_display_text,
    get_db_and_wal_stamp,
    get_file_stat_stamp,
    iter_jsonl,
    normalize_timestamp_ms,
    open_ro_sqlite,
)
from ai_session_hub.usage import amount, counter


class ClaudeAdapter:
    """Adapter for Claude Code CLI sessions (JSONL transcripts + search cache)."""

    def discover(self, source: SourceSpec) -> Iterable[SessionRef]:
        root = source.canonical_root
        if not root.exists():
            return

        seen_sessions: set[str] = set()

        # 1. Discover native JSONL transcripts in projects/ and transcripts/
        for folder_name in ["projects", "transcripts"]:
            folder = root / folder_name
            if not folder.exists():
                continue
            for jsonl_path in folder.glob("**/*.jsonl"):
                # Extract session ID from path stem or first line
                session_id = None
                uuid_match = re.search(
                    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                    jsonl_path.stem,
                    re.IGNORECASE,
                )
                if uuid_match:
                    session_id = uuid_match.group(1)
                else:
                    # Try reading first line
                    try:
                        with jsonl_path.open("r", encoding="utf-8", errors="ignore") as f:
                            for _ in range(5):
                                line = f.readline()
                                if not line:
                                    break
                                data = json.loads(line)
                                sid = data.get("sessionId") or data.get("session_id")
                                if sid:
                                    session_id = sid
                                    break
                    except Exception:
                        pass

                if not session_id:
                    session_id = jsonl_path.stem

                seen_sessions.add(session_id)
                revision = "claude-v3:" + (get_file_stat_stamp(jsonl_path) or "missing")

                yield SessionRef(
                    key=canonical_session_key(source.tool, root, session_id),
                    source=source,
                    native_id=session_id,
                    locators=(jsonl_path,),
                    revision=revision,
                )

        # 2. Discover cached sessions in .search-index/search.db
        search_db = root / ".search-index" / "search.db"
        if search_db.is_file():
            try:
                conn = open_ro_sqlite(search_db)
                rows = conn.execute("SELECT session_id, file_path, start_time FROM sessions;").fetchall()
                db_rev = get_db_and_wal_stamp(search_db) or "missing"
                for r in rows:
                    sid = r["session_id"]
                    if sid in seen_sessions:
                        continue
                    seen_sessions.add(sid)
                    yield SessionRef(
                        key=canonical_session_key(source.tool, root, sid),
                        source=source,
                        native_id=sid,
                        locators=(search_db,),
                        revision=f"claude-v3:{db_rev}:{r['start_time']}",
                    )
                conn.close()
            except Exception:
                pass

    def read(self, ref: SessionRef, cancelled: Callable[[], bool]) -> SessionSnapshot:
        root = ref.source.canonical_root

        # Determine if locator is native JSONL or search.db
        jsonl_path = None
        search_db_path = None
        for loc in ref.locators:
            if loc.suffix == ".jsonl" and loc.is_file():
                jsonl_path = loc
            elif loc.name == "search.db" and loc.is_file():
                search_db_path = loc

        if not jsonl_path and not search_db_path:
            # Re-check if a native JSONL exists on disk
            for folder in [root / "projects", root / "transcripts"]:
                if folder.exists():
                    for p in folder.glob(f"**/*{ref.native_id}*.jsonl"):
                        jsonl_path = p
                        break
                if jsonl_path:
                    break

        if jsonl_path and jsonl_path.is_file():
            return self._read_native_jsonl(ref, jsonl_path, cancelled)
        elif search_db_path and search_db_path.is_file():
            return self._read_search_db(ref, search_db_path, cancelled)
        else:
            return SessionSnapshot(
                ref=ref,
                client="claude",
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
                warnings=("No transcript or search cache found for session",),
            )

    def _read_native_jsonl(self, ref: SessionRef, path: Path, cancelled: Callable[[], bool]) -> SessionSnapshot:
        warnings: list[str] = []
        raw_records: list[dict[str, Any]] = []
        seen_uuids: set[str] = set()
        usage_by_message: dict[str, dict[str, Any]] = {}

        custom_title = None
        ai_title = None
        session_cwd = None
        started_ms = None
        updated_ms = None
        is_sidechain = False

        # Check history.jsonl for cwd mapping if available
        history_path = ref.source.canonical_root / "history.jsonl"
        history_cwds: set[Path] = set()
        if history_path.is_file():
            try:
                for _, h_obj, _ in iter_jsonl(history_path, cancelled):
                    if h_obj.get("sessionId") == ref.native_id:
                        p_val = h_obj.get("project")
                        if isinstance(p_val, str) and Path(p_val).is_absolute():
                            history_cwds.add(Path(p_val).resolve())
            except Exception:
                pass

        for line_no, obj, line_warnings in iter_jsonl(path, cancelled):
            warnings.extend(line_warnings)

            # Metadata extraction
            if obj.get("type") == "custom-title":
                custom_title = obj.get("customTitle")
            elif obj.get("type") == "ai-title":
                ai_title = obj.get("aiTitle")

            if obj.get("cwd"):
                try:
                    p = Path(obj["cwd"])
                    if p.is_absolute():
                        session_cwd = p.resolve()
                except Exception:
                    pass

            if obj.get("isSidechain") or "/subagents/" in str(path):
                is_sidechain = True

            ts = normalize_timestamp_ms(obj.get("timestamp"))
            if ts:
                if started_ms is None or ts < started_ms:
                    started_ms = ts
                if updated_ms is None or ts > updated_ms:
                    updated_ms = ts

            # Check line UUID deduplication
            line_uuid = obj.get("uuid")
            if line_uuid:
                if line_uuid in seen_uuids:
                    continue
                seen_uuids.add(line_uuid)


            # Usage belongs to the API message, including thinking/tool-only blocks.
            # UUID replay has already been removed; later blocks replace cumulative
            # fields for the same message instead of charging each block again.
            msg_data = obj.get("message")
            if (
                isinstance(msg_data, dict)
                and (msg_data.get("role") or obj.get("type")) == "assistant"
                and obj.get("sessionId", ref.native_id) == ref.native_id
                and isinstance(msg_data.get("usage"), dict)
            ):
                message_id = msg_data.get("id")
                source_id = (
                    f"message:{message_id}" if isinstance(message_id, str) and message_id
                    else f"uuid:{line_uuid}" if line_uuid else f"line:{line_no}"
                )
                entry = usage_by_message.setdefault(source_id, {"usage": {}})
                entry["usage"].update(msg_data["usage"])
                if isinstance(msg_data.get("model"), str) and msg_data["model"]:
                    entry["model"] = msg_data["model"]
                if ts is not None:
                    entry["timestamp_ms"] = ts
            raw_records.append(obj)

        # Build message chain and coalesce assistant text blocks with same message.id
        coalesced: list[dict[str, Any]] = []
        # Map raw entry UUID to message index
        uuid_to_msg_id: dict[str, str] = {}

        for obj in raw_records:
            msg_type = obj.get("type")
            msg_data = obj.get("message", {}) if isinstance(obj.get("message"), dict) else {}
            role = msg_data.get("role") or (msg_type if msg_type in ("user", "assistant") else None)

            if role not in ("user", "assistant"):
                continue

            # Extract text
            content = msg_data.get("content") or obj.get("content") or obj.get("text")
            text_blocks: list[str] = []
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        text_blocks.append(b.get("text", ""))
                    elif isinstance(b, str):
                        text_blocks.append(b)
            elif isinstance(content, str):
                text_blocks.append(content)

            text = "\n".join(text_blocks).strip()
            if not text:
                continue

            line_uuid = obj.get("uuid") or f"msg-{len(coalesced) + 1}"
            parent_uuid = obj.get("parentUuid")
            msg_id = msg_data.get("id") or line_uuid

            # Check if we should coalesce with preceding assistant message of same message.id
            if role == "assistant" and coalesced and coalesced[-1]["msg_id"] == msg_id:
                coalesced[-1]["text"] += "\n" + text
                if line_uuid:
                    uuid_to_msg_id[line_uuid] = coalesced[-1]["source_id"]
            else:
                entry = {
                    "source_id": line_uuid,
                    "msg_id": msg_id,
                    "parent_uuid": parent_uuid,
                    "role": role,
                    "text": text,
                    "timestamp_ms": normalize_timestamp_ms(obj.get("timestamp")),
                }
                coalesced.append(entry)
                if line_uuid:
                    uuid_to_msg_id[line_uuid] = line_uuid

        # Map parent_uuid through uuid_to_msg_id
        messages: list[MessageRecord] = []
        for i, item in enumerate(coalesced, 1):
            p_uuid = item["parent_uuid"]
            resolved_parent = uuid_to_msg_id.get(p_uuid) if p_uuid else None
            messages.append(
                MessageRecord(
                    source_id=item["source_id"],
                    parent_id=resolved_parent,
                    ordinal=i,
                    role=item["role"],
                    text=clean_display_text(item["text"]),
                    timestamp_ms=item["timestamp_ms"],
                    flags=(),
                )
            )

        # Determine title
        title = custom_title or ai_title
        if not title:
            for m in messages:
                if m.role == "user" and m.text.strip():
                    first_text = m.text.strip().replace("\n", " ")
                    title = first_text[:96]
                    break
        if not title:
            title = ref.native_id

        effective_cwd = session_cwd or (next(iter(history_cwds)) if len(history_cwds) == 1 else None)

        usage: list[UsageRecord] = []
        for source_id, entry in usage_by_message.items():
            native = entry["usage"]
            fields = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            values = {field: counter(native.get(field)) for field in fields}
            if any(field in native and values[field] is None for field in fields):
                warnings.append(f"Claude usage {source_id}: invalid token counter; usage is partial")
            # Anthropic input_tokens excludes both cache components. An omitted
            # cache component is unknown, not an invented zero.
            components = [values[field] for field in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
            input_tokens = counter(sum(components)) if all(value is not None for value in components) else None
            output_tokens = values["output_tokens"]
            total_tokens = counter(input_tokens + output_tokens) if input_tokens is not None and output_tokens is not None else None
            usage.append(UsageRecord(
                source_id=source_id,
                timestamp_ms=entry.get("timestamp_ms"),
                model=entry.get("model"),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=values["cache_read_input_tokens"],
                cache_write_tokens=values["cache_creation_input_tokens"],
                total_tokens=total_tokens,
            ))

        return SessionSnapshot(
            ref=ref,
            client="claude",
            title=title,
            project_id=str(effective_cwd) if effective_cwd else None,
            project_label=effective_cwd.name if effective_cwd else None,
            cwd=effective_cwd,
            started_ms=started_ms,
            updated_ms=updated_ms,
            kind="subagent" if is_sidechain else "conversation",
            archived=False,
            text_state="native" if messages else "partial",
            messages=tuple(messages),
            usage=tuple(usage),
            warnings=tuple(warnings),
        )

    def _read_search_db(self, ref: SessionRef, db_path: Path, cancelled: Callable[[], bool]) -> SessionSnapshot:
        warnings: list[str] = []
        messages: list[MessageRecord] = []
        usage: list[UsageRecord] = []
        title = ref.native_id
        started_ms = None
        updated_ms = None
        project_id = None
        project_label = None

        try:
            conn = open_ro_sqlite(db_path)
            s_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions);").fetchall()}
            m_cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages);").fetchall()}
            s_row = conn.execute("SELECT * FROM sessions WHERE session_id = ?;", (ref.native_id,)).fetchone()
            if s_row:
                title = s_row["title"] or title
                started_ms = normalize_timestamp_ms(s_row["start_time"])
                updated_ms = normalize_timestamp_ms(s_row["end_time"])
                if "project_path" in s_cols:
                    p_path = s_row["project_path"]
                    if p_path:
                        project_id = p_path
                        p_name = Path(p_path).name.lstrip("-")
                        if "Desktop-" in p_name:
                            p_name = p_name.split("Desktop-")[-1]
                        project_label = p_name or None

            query_cols = ["uuid", "type", "content", "timestamp"]
            for c in ["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "calculated_cost", "model_used"]:
                if c in m_cols:
                    query_cols.append(c)
                else:
                    query_cols.append(f"0 AS {c}")
            m_rows = conn.execute(
                f"""
                SELECT {', '.join(query_cols)}
                FROM messages
                WHERE session_id = ?
                ORDER BY timestamp ASC, rowid ASC;
                """,
                (ref.native_id,),
            ).fetchall()

            for i, m in enumerate(m_rows, 1):
                role = "assistant" if m["type"] in ("assistant", "agent") else "user"
                content = m["content"] or ""
                messages.append(
                    MessageRecord(
                        source_id=m["uuid"] or f"cache-{i}",
                        parent_id=None,
                        ordinal=i,
                        role=role,
                        text=clean_display_text(content),
                        timestamp_ms=normalize_timestamp_ms(m["timestamp"]),
                        flags=("cached",),
                    )
                )
                inp = counter(m["input_tokens"]) or 0
                out = counter(m["output_tokens"]) or 0
                cr = counter(m["cache_read_input_tokens"]) or 0
                cw = counter(m["cache_creation_input_tokens"]) or 0
                tot = inp + out + cr + cw
                cost = amount(m["calculated_cost"])
                model = m["model_used"]
                if tot > 0 or cost is not None:
                    usage.append(
                        UsageRecord(
                            source_id=m["uuid"] or f"cache_msg_{i}",
                            timestamp_ms=normalize_timestamp_ms(m["timestamp"]),
                            model=model,
                            provider="anthropic",
                            input_tokens=inp + cr + cw,
                            output_tokens=out,
                            cache_read_tokens=cr,
                            cache_write_tokens=cw,
                            total_tokens=tot,
                            cost_usd=cost if (cost is not None and cost > 0) else None,
                            cost_kind="actual" if (cost is not None and cost > 0) else "unknown",
                            granularity="event",
                        )
                    )

            if not usage and s_row and "total_tokens" in s_cols:
                tot = counter(s_row["total_tokens"])
                cost = amount(s_row["total_cost"]) if "total_cost" in s_cols else None
                if tot or cost:
                    model_name = None
                    models_json = s_row["models_used"] if "models_used" in s_cols else None
                    model_name = None
                    models_json = s_row["models_used"]
                    if models_json:
                        try:
                            m_list = json.loads(models_json)
                            if isinstance(m_list, list) and m_list:
                                model_name = str(m_list[0])
                        except Exception:
                            pass
                    inp = counter(s_row["input_tokens"]) or 0
                    out = counter(s_row["output_tokens"]) or 0
                    cr = counter(s_row["cache_read_tokens"]) or 0
                    cw = counter(s_row["cache_creation_tokens"]) or 0
                    usage.append(
                        UsageRecord(
                            source_id=f"session_totals:{ref.native_id}",
                            timestamp_ms=started_ms,
                            model=model_name,
                            provider="anthropic",
                            input_tokens=inp + cr + cw,
                            output_tokens=out,
                            cache_read_tokens=cr,
                            cache_write_tokens=cw,
                            total_tokens=tot or (inp + out + cr + cw),
                            cost_usd=cost if (cost is not None and cost > 0) else None,
                            cost_kind="actual" if (cost is not None and cost > 0) else "unknown",
                            granularity="session",
                        )
                    )
            conn.close()
        except Exception as e:
            warnings.append(f"Search DB read error: {e}")

        return SessionSnapshot(
            ref=ref,
            client="claude",
            title=title,
            project_id=project_id,
            project_label=project_label,
            cwd=None,
            started_ms=started_ms,
            updated_ms=updated_ms,
            kind="conversation",
            archived=False,
            text_state="cached",
            messages=tuple(messages),
            usage=tuple(usage),
            warnings=tuple(warnings),
        )

    def prepare_resume(self, ref: SessionRef) -> LaunchSpec | Unavailable:
        root = ref.source.canonical_root
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", ref.native_id, re.I):
            return Unavailable("invalid_identity", f"Invalid Claude session UUID: {ref.native_id}")
        native_path = next((path for path in ref.locators if path.suffix == ".jsonl" and path.is_file()), None)
        if native_path is None:
            for folder in (root / "projects", root / "transcripts"):
                native_path = next(folder.glob(f"**/{ref.native_id}.jsonl"), None)
                if native_path is not None:
                    break
        if native_path is None:
            return Unavailable(
                "unsupported_resume",
                "Claude 仅保留搜索缓存，原生 JSONL 转录不存在。请从备份恢复原转录到此 Claude 配置目录后刷新；缓存不能直接恢复会话。",
            )
        session_ids = {obj["sessionId"] for _, obj, _ in iter_jsonl(native_path, lambda: False) if isinstance(obj.get("sessionId"), str)}
        if session_ids != {ref.native_id}:
            return Unavailable("invalid_identity", "Claude 转录的 sessionId 与所选会话不一致；请刷新索引。")
        snapshot = self._read_native_jsonl(ref, native_path, lambda: False)
        if snapshot.kind == "subagent":
            return Unavailable("secondary_session", "Claude 子代理转录不能作为顶级会话恢复。")
        exe = shutil.which("claude")
        if not exe:
            return Unavailable("missing_executable", "Claude executable 'claude' not found on PATH")
        if snapshot.cwd is None:
            return Unavailable("unknown_cwd", "Claude 原生转录没有可靠的绝对工作目录。")
        if not snapshot.cwd.is_dir():
            return Unavailable("missing_cwd", f"Session working directory does not exist: {snapshot.cwd}")
        return LaunchSpec(
            argv=(exe, "--resume", ref.native_id),
            cwd=snapshot.cwd,
            env_overrides={"CLAUDE_CONFIG_DIR": str(root)},
        )


register_adapter("claude", ClaudeAdapter())
