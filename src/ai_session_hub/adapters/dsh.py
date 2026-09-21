from __future__ import annotations

import io
import json
import shutil
from pathlib import Path
from typing import Any, Callable, Iterable

import zstandard as zstd

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
    get_file_stat_stamp,
    normalize_timestamp_ms,
)
from ai_session_hub.usage import counter


class DshAdapter:
    """Adapter for DSH TUI sessions stored as zstd-compressed JSONL."""

    @staticmethod
    def _text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                block["text"] for block in content
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
            )
        return ""

    def discover(self, source: SourceSpec) -> Iterable[SessionRef]:
        root = source.canonical_root
        sessions_dir = root / "sessions"
        if not sessions_dir.is_dir():
            return

        # Path pattern: <root>/sessions/<encoded-cwd>/<session-id>/session*.jsonl.zstd
        # Search for session directories
        for sess_dir in sessions_dir.glob("*/*"):
            if not sess_dir.is_dir():
                continue

            session_id = sess_dir.name

            # Select version: session.v3.jsonl.zstd > session.v2.jsonl.zstd > session.jsonl.zstd
            selected_file = None
            for v_name in ["session.v3.jsonl.zstd", "session.v2.jsonl.zstd", "session.jsonl.zstd"]:
                candidate = sess_dir / v_name
                if candidate.is_file():
                    selected_file = candidate
                    break

            if not selected_file:
                # Check any .zstd file in dir
                zst_files = list(sess_dir.glob("*.zstd"))
                if zst_files:
                    selected_file = zst_files[0]

            if not selected_file:
                continue

            revision = "dsh-v3:" + (get_file_stat_stamp(selected_file) or "missing")

            yield SessionRef(
                key=canonical_session_key(source.tool, root, session_id),
                source=source,
                native_id=session_id,
                locators=(selected_file,),
                revision=revision,
            )

    @staticmethod
    def _usage_record(obj: dict[str, Any], line_no: int, warnings: list[str]) -> UsageRecord | None:
        data = obj.get("data")
        if not isinstance(data, dict):
            return None
        raw = data.get("usage")
        if raw is None and isinstance(data.get("stream"), list):
            for item in reversed(data["stream"]):
                chunk = item.get("chunk") if isinstance(item, dict) else None
                if isinstance(chunk, dict) and isinstance(chunk.get("usage"), dict):
                    raw = chunk["usage"]
                    break
        if raw is None:
            return None
        if not isinstance(raw, dict):
            warnings.append("DSH unsupported usage payload")
            return None
        names = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens", "totalTokens")
        values = {name: counter(raw.get(name)) for name in names}
        if any(raw.get(name) is not None and values[name] is None for name in names):
            warnings.append("DSH invalid native token counter; affected usage remains unknown")
        inputs = [values[name] for name in ("inputTokens", "cacheReadTokens", "cacheWriteTokens")]
        inclusive_input = sum(inputs) if all(value is not None for value in inputs) else None
        output = values["outputTokens"]
        total = values["totalTokens"]
        # Native total can establish inclusive input even if cache split is absent.
        if inclusive_input is None and total is not None and output is not None and total >= output:
            inclusive_input = total - output
        if inclusive_input is None and any(value is not None for value in inputs):
            warnings.append("DSH input/cache breakdown incomplete; inclusive input remains unknown")
        derived_total = inclusive_input + output if inclusive_input is not None and output is not None else None
        if total is not None and derived_total is not None and total != derived_total:
            warnings.append("DSH native total disagrees with components; preserving reported total")
        reasoning = values["reasoningTokens"]
        if reasoning is not None and output is not None and reasoning > output:
            warnings.append("DSH reasoning exceeds inclusive output")
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        source = message.get("source") if isinstance(message.get("source"), dict) else {}
        if obj.get("type") == "compaction/summary":
            source = data
        seq = counter(obj.get("seq"))
        identity = str(message["id"]) if message.get("id") else f"seq:{seq}" if seq is not None else f"line:{line_no}"
        if not message.get("id") and seq is None:
            warnings.append("DSH usage lacks native event identity; replay detection is limited")
        timestamp = counter(obj.get("time")) if obj.get("time") is not None else normalize_timestamp_ms(obj.get("timestamp"))
        if timestamp is None:
            warnings.append("DSH usage event has no usable native timestamp")
        if not isinstance(source.get("model"), str) or not source["model"]:
            warnings.append("DSH usage event has no recorded model route")
        return UsageRecord(
            source_id=f"{obj['type']}:{identity}",
            timestamp_ms=timestamp,
            model=source.get("model") if isinstance(source.get("model"), str) and source["model"] else None,
            provider=source.get("provider") if isinstance(source.get("provider"), str) and source["provider"] else None,
            input_tokens=inclusive_input, output_tokens=output,
            cache_read_tokens=values["cacheReadTokens"], cache_write_tokens=values["cacheWriteTokens"],
            reasoning_tokens=reasoning, total_tokens=total if total is not None else derived_total,
        )

    def read(self, ref: SessionRef, cancelled: Callable[[], bool]) -> SessionSnapshot:
        zst_path = ref.locators[0]
        warnings: list[str] = []

        if not zst_path.is_file():
            return SessionSnapshot(
                ref=ref,
                client="dsh",
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
                warnings=(f"DSH transcript file not found: {zst_path}",),
            )

        header: dict[str, Any] = {}
        title_val: str | None = None
        raw_messages: list[dict[str, Any]] = []
        started_ms = None
        updated_ms = None
        seen_msg_ids: set[str] = set()
        seed_length: int | None = None
        tagged_cut: int | None = None
        raw_usage: list[tuple[int | None, UsageRecord]] = []
        has_partial_frame = False

        dctx = zstd.ZstdDecompressor()
        try:
            with zst_path.open("rb") as f:
                with dctx.stream_reader(f, read_across_frames=True) as reader:
                    text_stream = io.TextIOWrapper(reader, encoding="utf-8", errors="replace")
                    line_no = 0
                    while True:
                        if cancelled():
                            break
                        try:
                            line = text_stream.readline()
                        except (zstd.ZstdError, OSError) as e:
                            warnings.append(f"Zstd stream error: {e}")
                            has_partial_frame = True
                            break
                        if not line:
                            break

                        line_no += 1
                        line_str = line.strip()
                        if not line_str:
                            continue

                        try:
                            obj = json.loads(line_str)
                        except json.JSONDecodeError as err:
                            warnings.append(f"Line {line_no}: Malformed JSON: {err}")
                            continue

                        if not isinstance(obj, dict):
                            warnings.append(f"Line {line_no}: Unsupported DSH record")
                            continue
                        msg_type = obj.get("type")
                        data = obj.get("data", {}) if isinstance(obj.get("data"), dict) else {}
                        seq = counter(obj.get("seq"))
                        if msg_type in ("assistant/message", "assistant/attempt", "compaction/summary"):
                            record = self._usage_record(obj, line_no, warnings)
                            if record is not None:
                                raw_usage.append((seq, record))

                        if msg_type == "session":
                            header = data or obj
                            ts = normalize_timestamp_ms(header.get("createdAt") or obj.get("createdAt"))
                            if ts:
                                started_ms = ts
                            seed_length = counter(header.get("seedLength"))
                            if header.get("version") not in (None, 0, 1, 2, 3):
                                warnings.append("DSH unsupported session version; known fields only")
                        elif msg_type == "session/title":
                            title_val = data.get("title")
                        elif msg_type == "user/message":
                            # Only treat as user message if source kind is user or absent
                            src_kind = data.get("source", {}).get("kind") if isinstance(data.get("source"), dict) else None
                            if src_kind is None or src_kind == "user":
                                m_id = data.get("id") or f"user-{line_no}"
                                content = self._text(data.get("content"))
                                if content and m_id not in seen_msg_ids:
                                    seen_msg_ids.add(m_id)
                                    m_ts = counter(obj.get("time")) if obj.get("time") is not None else normalize_timestamp_ms(obj.get("timestamp") or data.get("timestamp"))
                                    if m_ts:
                                        updated_ms = m_ts
                                    raw_messages.append({
                                        "source_id": m_id,
                                        "role": "user",
                                        "text": clean_display_text(content),
                                        "timestamp_ms": m_ts,
                                        "seq": seq,
                                    })
                        elif msg_type == "assistant/message":
                            # Attempts/compaction carry usage but not dialogue.
                            m_id = data.get("message", {}).get("id") if isinstance(data.get("message"), dict) else data.get("id")
                            m_id = m_id or f"asst-{line_no}"
                            content = self._text(data.get("content"))
                            if not content and isinstance(data.get("message"), dict):
                                content = self._text(data["message"].get("content"))

                            if content and m_id not in seen_msg_ids:
                                seen_msg_ids.add(m_id)
                                m_ts = counter(obj.get("time")) if obj.get("time") is not None else normalize_timestamp_ms(obj.get("timestamp") or data.get("timestamp"))
                                if m_ts:
                                    updated_ms = m_ts
                                raw_messages.append({
                                    "source_id": m_id,
                                    "role": "assistant",
                                    "text": clean_display_text(content),
                                    "timestamp_ms": m_ts,
                                    "seq": seq,
                                })
                        elif msg_type == "session/end-seed" and data.get("inherited") is True and seq is not None:
                            tagged_cut = seq
        except Exception as e:
            warnings.append(f"Decompression failure on {zst_path.name}: {e}")
            has_partial_frame = True

        # Exact native event cutoff; never count retained dialogue rows as events.
        seeded = seed_length is not None or header.get("isSeeded") is True
        inherited_cut = seed_length if seed_length is not None else tagged_cut if seeded else 0
        uncertain_seed = seeded and inherited_cut != 0
        if not header:
            warnings.append("DSH session header unavailable; usage ownership cannot be established")
        if uncertain_seed and (inherited_cut is None or any(seq is None for seq, _ in raw_usage) or any(m["seq"] is None for m in raw_messages)):
            warnings.append("DSH inherited usage cutoff/sequence is unknown; ambiguous usage is excluded, prose retained")
        usage: dict[str, UsageRecord] = {}
        for seq, record in raw_usage:
            if not header or (uncertain_seed and (inherited_cut is None or seq is None or seq < inherited_cut)):
                continue
            usage[record.source_id] = record

        # Check metadata cache if needed
        projcache = ref.source.canonical_root / "storages" / "session_projcache" / "sessions" / f"{ref.native_id}.json"
        cache_cwd = None
        cache_title = None
        if projcache.is_file():
            try:
                c_data = json.loads(projcache.read_text(encoding="utf-8"))
                cache_cwd = c_data.get("identity", {}).get("cwd")
                cache_title = c_data.get("rows", {}).get("title", {}).get("val")
            except Exception:
                pass

        # Build message records
        messages: list[MessageRecord] = []
        for i, m in enumerate(raw_messages, 1):
            unknown_owner = uncertain_seed and (inherited_cut is None or m["seq"] is None)
            inherited = uncertain_seed and not unknown_owner and m["seq"] < inherited_cut
            flags = ("inherited",) if inherited else (("partial",) if unknown_owner else ())
            messages.append(
                MessageRecord(
                    source_id=m["source_id"],
                    parent_id=None,
                    ordinal=i,
                    role=m["role"],
                    text=m["text"],
                    timestamp_ms=m["timestamp_ms"],
                    flags=flags,
                )
            )

        # Title resolution
        title = title_val or cache_title
        if not title:
            for m in messages:
                if m.role == "user" and m.text.strip():
                    first_text = m.text.strip().replace("\n", " ")
                    title = first_text[:96]
                    break
        if not title:
            title = ref.native_id

        # CWD resolution
        cwd_str = header.get("cwd") or cache_cwd
        cwd = Path(cwd_str).resolve() if cwd_str else None

        text_state = "native"
        if has_partial_frame:
            text_state = "partial"
        elif not messages:
            text_state = "metadata-only"

        return SessionSnapshot(
            ref=ref,
            client="dsh",
            title=title,
            project_id=str(cwd) if cwd else None,
            project_label=cwd.name if cwd else None,
            cwd=cwd,
            started_ms=started_ms,
            updated_ms=updated_ms,
            kind="subagent" if header.get("delegationDepth", 0) else "conversation",
            archived=False,
            text_state=text_state,
            messages=tuple(messages),
            usage=tuple(usage.values()),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def prepare_resume(self, ref: SessionRef) -> LaunchSpec | Unavailable:
        root = ref.source.canonical_root

        # Check native ID: must be nonempty, stripped, and exact match
        if not ref.native_id or ref.native_id != ref.native_id.strip():
            return Unavailable(
                "invalid_identity",
                f"Invalid or padded DSH session ID: '{ref.native_id}'",
            )

        # Profile check: launcher only supports 'dsh-tui'
        if ref.source.profile and ref.source.profile != "dsh-tui":
            return Unavailable(
                "unsupported_profile",
                f"DSH launcher only supports profile 'dsh-tui', requested '{ref.source.profile}'",
            )

        # The launcher delegates to the bin INSIDE the installed package. An
        # absent/unreadable/versionless package causes it to bootstrap instead.
        package = root / "profiles" / "dsh-tui" / "node_modules" / "@deepseek-harness-tui" / "dsh-tui"
        try:
            installed = json.loads((package / "package.json").read_text(encoding="utf-8"))
            with (package / "bin" / "dsh-tui.js").open("rb") as stream:
                stream.read(1)
            ready = isinstance(installed, dict) and isinstance(installed.get("version"), str) and bool(installed["version"])
        except (OSError, ValueError):
            ready = False
        if not ready:
            return Unavailable("unverified_compatibility", "DSH profile dsh-tui 的原生包/启动文件未初始化或不可读；拒绝触发自动安装。")

        exe = shutil.which("dsh-tui")
        if not exe:
            return Unavailable("missing_executable", "dsh-tui executable not found on PATH")
        if not shutil.which("dsh"):
            return Unavailable("missing_executable", "DSH 原生运行时 dsh 不在 PATH 中。")

        if not ref.locators or not ref.locators[0].is_file():
            return Unavailable("missing_source", "DSH 原生转录不存在。")
        path = ref.locators[0].resolve()
        if not path.is_relative_to(root / "sessions") or path.parent.name != ref.native_id:
            return Unavailable("invalid_identity", "DSH 原生转录路径与所选 ID 不一致。")
        try:
            with path.open("rb") as stream, zstd.ZstdDecompressor().stream_reader(stream, read_across_frames=True) as reader:
                text_stream = io.TextIOWrapper(reader, encoding="utf-8")
                obj = json.loads(text_stream.readline())
        except (OSError, ValueError, zstd.ZstdError):
            return Unavailable("invalid_identity", "DSH 原生转录会话头损坏，不能安全恢复。")
        if not isinstance(obj, dict) or obj.get("type") != "session":
            return Unavailable("invalid_identity", "DSH 原生转录没有有效会话头。")
        header = obj.get("data") if isinstance(obj.get("data"), dict) and obj["data"] else obj
        if header.get("id") != ref.native_id:
            return Unavailable("invalid_identity", "DSH 原生会话头与所选 ID 不一致；请刷新索引。")
        if header.get("delegationDepth", 0):
            return Unavailable("secondary_session", "DSH 委派记录不能作为顶级会话恢复。")
        raw_cwd = header.get("cwd")
        if not isinstance(raw_cwd, str) or not Path(raw_cwd).is_absolute():
            return Unavailable("unknown_cwd", "DSH 原生会话头没有绝对工作目录；项目缓存不能授权恢复。")
        cwd = Path(raw_cwd).resolve()
        if not cwd.is_dir():
            return Unavailable("missing_cwd", f"Session working directory does not exist: {cwd}")

        argv = (exe, f"--resume={ref.native_id}")
        env_overrides = {
            "DSH_HOME": str(root),
            "DSH_TUI_WORKSPACE_TARGET": str(cwd),
            "DSH_TUI_RESUME_SESSION": ref.native_id,
        }

        return LaunchSpec(
            argv=argv,
            cwd=cwd,
            env_overrides=env_overrides,
        )


register_adapter("dsh", DshAdapter())
