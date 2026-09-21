from __future__ import annotations

import os
import re
import shutil
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
    get_file_stat_stamp,
    iter_jsonl,
    normalize_timestamp_ms,
)
from ai_session_hub.usage import amount, counter


class OmpAdapter:
    """Adapter for Oh My Pi (OMP) CLI sessions (JSONL transcripts)."""

    @staticmethod
    def _header(path: Path) -> dict[str, Any]:
        for _, obj, _ in iter_jsonl(path, lambda: False):
            if obj.get("type") == "title":
                continue
            return obj if obj.get("type") == "session" else {}
        return {}

    @staticmethod
    def _kind(path: Path, sessions_dir: Path) -> str:
        # Native advisors use reserved filenames, never arbitrary project names.
        if path.name == "__advisor.jsonl" or (path.name.startswith("__advisor.") and path.suffix == ".jsonl"):
            return "advisor"
        # Ordinary sessions live in a cwd bucket. Task transcripts instead live
        # in <parent-session-without-.jsonl>/<agent-id>.jsonl.
        for parent in path.resolve().parents:
            if parent == sessions_dir.resolve():
                break
            if parent.with_name(parent.name + ".jsonl").is_file():
                return "subagent"
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}T[^/]+_[0-9a-f-]{36}", parent.name, re.I):
                return "subagent"
        return "conversation"

    def discover(self, source: SourceSpec) -> Iterable[SessionRef]:
        root = source.canonical_root
        sessions_dir = source.canonical_sessions_dir or (root / "sessions" if (root / "sessions").is_dir() else root)

        if not sessions_dir.exists():
            return

        entries = [(path, self._header(path)) for path in sessions_dir.glob("**/*.jsonl")]
        self._session_paths: dict[str, list[Path]] = {}
        for path, header in entries:
            if header.get("id"):
                self._session_paths.setdefault(str(header["id"]), []).append(path)
        for jsonl_path, header in entries:
            native_id = header.get("id") or jsonl_path.stem
            parent_path = self._parent_path(header, sessions_dir)
            parent_stamp = get_file_stat_stamp(parent_path) if parent_path else "unresolved"
            revision = "omp-v3:" + (get_file_stat_stamp(jsonl_path) or "missing") + ":" + (parent_stamp or "missing")

            yield SessionRef(
                key=canonical_session_key(source.tool, root, str(jsonl_path)),
                source=source,
                native_id=native_id,
                locators=(jsonl_path,),
                revision=revision,
            )

    def _parent_path(self, header: dict[str, Any], sessions_dir: Path) -> Path | None:
        parent = header.get("parentSession")
        if not isinstance(parent, str) or not parent:
            return None
        candidate = Path(parent)
        if candidate.is_absolute():
            return candidate if candidate.is_file() and candidate.resolve().is_relative_to(sessions_dir.resolve()) else None
        paths = [path for path in getattr(self, "_session_paths", {}).get(parent, []) if path.resolve().is_relative_to(sessions_dir.resolve())]
        if not paths:
            paths = [path for path in sessions_dir.glob("**/*.jsonl") if self._header(path).get("id") == parent]
        return paths[0] if len(paths) == 1 else None

    @staticmethod
    def _usage_record(source_id: str, timestamp: int | None, message: dict[str, Any],
                      warnings: list[str]) -> UsageRecord | None:
        raw = message.get("usage")
        if not isinstance(raw, dict):
            warnings.append("OMP unsupported native usage record")
            return None
        names = ("input", "output", "cacheRead", "cacheWrite", "totalTokens", "reasoningTokens")
        values = {name: counter(raw.get(name)) for name in names}
        if any(raw.get(name) is not None and values[name] is None for name in names):
            warnings.append("OMP invalid native token counter; affected usage remains unknown")
        inputs = [values[name] for name in ("input", "cacheRead", "cacheWrite")]
        inclusive_input = sum(inputs) if all(value is not None for value in inputs) else None
        if inclusive_input is None and any(value is not None for value in inputs):
            warnings.append("OMP input/cache breakdown incomplete; inclusive input remains unknown")
        output = values["output"]
        derived_total = inclusive_input + output if inclusive_input is not None and output is not None else None
        total = values["totalTokens"]
        if total is not None and derived_total is not None and total != derived_total:
            warnings.append("OMP native total disagrees with input/output components; preserving reported total")
        cost = amount(raw["cost"].get("total")) if isinstance(raw.get("cost"), dict) else None
        if isinstance(raw.get("orchestration"), dict):
            warnings.append("OMP total includes provider orchestration; conversation component breakdown excludes orchestration")
        if output is not None and values["reasoningTokens"] is not None and values["reasoningTokens"] > output:
            warnings.append("OMP reasoning exceeds inclusive output")
        if timestamp is None:
            warnings.append("OMP usage event has no usable native timestamp")
        if not isinstance(message.get("model"), str) or not message["model"]:
            warnings.append("OMP usage event has no recorded model route")
        # OMP's cost is a native catalog-rate calculation, not an invoice.
        return UsageRecord(
            source_id=source_id, timestamp_ms=timestamp,
            model=message.get("model") if isinstance(message.get("model"), str) and message["model"] else None,
            provider=message.get("provider") if isinstance(message.get("provider"), str) and message["provider"] else None,
            input_tokens=inclusive_input, output_tokens=output,
            cache_read_tokens=values["cacheRead"], cache_write_tokens=values["cacheWrite"],
            reasoning_tokens=values["reasoningTokens"],
            total_tokens=total if total is not None else derived_total,
            cost_usd=cost, cost_kind="estimated" if cost is not None else "unknown",
        )

    def read(self, ref: SessionRef, cancelled: Callable[[], bool]) -> SessionSnapshot:
        jsonl_path = ref.locators[0]
        warnings: list[str] = []

        if not jsonl_path.is_file():
            return SessionSnapshot(
                ref=ref,
                client="omp",
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
                warnings=(f"Session file not found: {jsonl_path}",),
            )

        title_slot: str | None = None
        session_header: dict[str, Any] = {}
        messages: list[MessageRecord] = []
        seen_message_ids: set[str] = set()
        started_ms = None
        updated_ms = None

        usage: dict[str, UsageRecord] = {}
        inherited_ids: set[str] = set()
        unresolved_parent = False
        top_dir = ref.source.canonical_sessions_dir or (ref.source.canonical_root / "sessions" if (ref.source.canonical_root / "sessions").is_dir() else ref.source.canonical_root)
        kind = self._kind(jsonl_path, top_dir)
        for line_no, obj, line_warnings in iter_jsonl(jsonl_path, cancelled):
            warnings.extend(line_warnings)
            entry_type = obj.get("type")

            if entry_type == "title":
                t = obj.get("title")
                if t:
                    title_slot = str(t).strip()
            elif entry_type == "session":
                session_header = obj
                ts = normalize_timestamp_ms(obj.get("timestamp"))
                if ts:
                    started_ms = ts
                if obj.get("parentSession"):
                    parent_path = self._parent_path(obj, top_dir)
                    if parent_path and parent_path.resolve() != jsonl_path.resolve():
                        try:
                            before = get_file_stat_stamp(parent_path)
                            for _, parent_entry, parent_warnings in iter_jsonl(parent_path, cancelled):
                                if parent_warnings:
                                    unresolved_parent = True
                                if parent_entry.get("id") and parent_entry.get("type") != "session":
                                    inherited_ids.add(str(parent_entry["id"]))
                            if before != get_file_stat_stamp(parent_path):
                                unresolved_parent = True
                        except OSError:
                            unresolved_parent = True
                    else:
                        unresolved_parent = True
                    if unresolved_parent:
                        warnings.append("OMP parent lineage unavailable/partial; pre-fork or undated usage is excluded")
            elif entry_type == "session_init" and kind == "conversation":
                # OMP persists this contract only for task/subagent sessions.
                kind = "subagent"
            elif entry_type == "message":
                msg_data = obj.get("message", {})
                if not isinstance(msg_data, dict):
                    warnings.append("OMP malformed message record")
                    continue
                entry_id = str(obj.get("id") or f"msg-{line_no}")
                entry_time = normalize_timestamp_ms(obj.get("timestamp") or msg_data.get("timestamp"))
                inherited = entry_id in inherited_ids
                uncertain = unresolved_parent and (entry_time is None or started_ms is None or entry_time <= started_ms)
                if msg_data.get("role") == "assistant" and "usage" in msg_data and not inherited and not uncertain:
                    record = self._usage_record(entry_id, entry_time, msg_data, warnings)
                    if record is not None:
                        usage[entry_id] = record
                # task toolResult usage is a delegated roll-up, not another call.
                role = msg_data.get("role")
                if role in ("user", "assistant"):
                    content = msg_data.get("content")
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
                    if text and entry_id not in seen_message_ids:
                        seen_message_ids.add(entry_id)
                        m_ts = normalize_timestamp_ms(obj.get("timestamp") or msg_data.get("timestamp"))
                        if m_ts:
                            updated_ms = m_ts

                        source_id = str(obj.get("id") or f"msg-{line_no}")
                        parent_id = str(obj.get("parentId")) if obj.get("parentId") else None

                        messages.append(
                            MessageRecord(
                                source_id=source_id,
                                parent_id=parent_id,
                                ordinal=len(messages) + 1,
                                role=role,
                                text=clean_display_text(text),
                                timestamp_ms=m_ts,
                                flags=("inherited",) if inherited else (("partial",) if uncertain else ()),
                            )
                        )
            elif entry_type == "model_usage":
                entry_id = str(obj.get("id") or f"usage-{line_no}")
                entry_time = normalize_timestamp_ms(obj.get("timestamp"))
                uncertain = unresolved_parent and (entry_time is None or started_ms is None or entry_time <= started_ms)
                if entry_id not in inherited_ids and not uncertain:
                    record = self._usage_record(entry_id, entry_time, obj, warnings)
                    if record is not None:
                        usage[entry_id] = record

        # Title resolution: title_slot > header title > first user text > native_id
        title = title_slot or session_header.get("title")
        if not title:
            for m in messages:
                if m.role == "user" and m.text.strip():
                    first_text = m.text.strip().replace("\n", " ")
                    title = first_text[:96]
                    break
        if not title:
            title = ref.native_id

        # CWD resolution
        cwd_str = session_header.get("cwd")
        cwd = Path(cwd_str).resolve() if cwd_str else None


        return SessionSnapshot(
            ref=ref,
            client="omp",
            title=title,
            project_id=str(cwd) if cwd else None,
            project_label=cwd.name if cwd else None,
            cwd=cwd,
            started_ms=started_ms,
            updated_ms=updated_ms,
            kind=kind,
            archived=False,
            text_state="native" if messages else "partial",
            messages=tuple(messages),
            usage=tuple(usage.values()),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def prepare_resume(self, ref: SessionRef) -> LaunchSpec | Unavailable:
        root = ref.source.canonical_root
        jsonl_path = ref.locators[0]

        if not jsonl_path.is_file():
            return Unavailable("missing_source", f"OMP session transcript file not found: {jsonl_path}")

        header = self._header(jsonl_path)
        if not ref.native_id or header.get("id") != ref.native_id:
            return Unavailable("invalid_identity", "OMP 原生会话头与所选 ID 不一致；请刷新索引。")
        if header.get("version") not in (None, 1, 2, 3):
            return Unavailable("unverified_compatibility", "不支持此 OMP 会话格式版本。")
        raw_cwd = header.get("cwd")
        if not isinstance(raw_cwd, str) or not Path(raw_cwd).is_absolute():
            return Unavailable("unknown_cwd", "OMP 原生会话头未记录绝对工作目录。")

        snapshot = self.read(ref, cancelled=lambda: False)

        # Reject secondary records
        if snapshot.kind in ("advisor", "subagent"):
            return Unavailable(
                "secondary_session",
                f"Secondary OMP {snapshot.kind} session is searchable but cannot be resumed as a top-level conversation.",
            )

        exe = shutil.which("omp")
        if not exe:
            return Unavailable("missing_executable", "OMP executable 'omp' not found on PATH")

        if not snapshot.cwd or not snapshot.cwd.is_dir():
            return Unavailable("missing_cwd", f"Session working directory does not exist: {snapshot.cwd}")

        named_layout = root.name == "agent" and root.parent.parent.name == "profiles"
        profile = ref.source.profile or (root.parent.name if named_layout else "default")
        profile = profile.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", profile) or profile.endswith("."):
            return Unavailable("unsupported_profile", f"无效 OMP profile：{profile}")
        config_root = root.parent.parent.parent if named_layout else root.parent
        effective_root = config_root / "profiles" / profile / "agent" if profile != "default" else root
        if effective_root.resolve() != root or (named_layout and profile == "default"):
            return Unavailable("unsupported_profile", "OMP profile 与所选会话存储目录不一致。")

        transcript_root = ref.source.canonical_sessions_dir or (root / "sessions" if (root / "sessions").is_dir() else root)

        argv = (
            exe,
            "--profile",
            profile,
            "--cwd",
            str(snapshot.cwd),
            "--session-dir",
            str(transcript_root),
            "--resume",
            str(jsonl_path.resolve()),
        )

        # Node path.join(home, PI_CONFIG_DIR) does not replace home for absolute
        # values; a relative spelling also represents relocated config roots.
        env_overrides = {"PI_CONFIG_DIR": os.path.relpath(config_root, Path.home())}
        if profile == "default":
            env_overrides["PI_CODING_AGENT_DIR"] = str(root)

        env_remove = ("OMP_PROFILE", "PI_PROFILE", "PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR")

        return LaunchSpec(
            argv=argv,
            cwd=snapshot.cwd,
            env_overrides=env_overrides,
            env_remove=env_remove,
        )


register_adapter("omp", OmpAdapter())
