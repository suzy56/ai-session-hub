from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# Kinds of session records
SessionKind = Literal[
    "conversation",
    "subagent",
    "advisor",
    "background-review",
    "ide",
    "unknown",
]

# Text availability states
TextState = Literal[
    "native",
    "cached",
    "partial",
    "metadata-only",
    "unavailable",
]

# Message flags
MessageFlag = Literal[
    "inherited",
    "historical-branch",
    "compacted",
    "partial",
    "ancestry-incomplete",
]


@dataclass(frozen=True)
class SourceSpec:
    tool: str
    root: Path
    profile: str | None = None
    sessions_dir: Path | None = None

    @property
    def canonical_root(self) -> Path:
        return self.root.resolve()

    @property
    def canonical_sessions_dir(self) -> Path | None:
        return self.sessions_dir.resolve() if self.sessions_dir is not None else None

    @property
    def source_key(self) -> str:
        parts = [self.tool, str(self.canonical_root)]
        if self.profile:
            parts.append(self.profile)
        if self.sessions_dir:
            parts.append(str(self.canonical_sessions_dir))
        return json.dumps(parts, separators=(",", ":"))


@dataclass(frozen=True)
class SessionRef:
    key: str
    source: SourceSpec
    native_id: str
    locators: tuple[Path, ...]
    revision: str


@dataclass(frozen=True)
class MessageRecord:
    source_id: str
    parent_id: str | None
    ordinal: int
    role: Literal["user", "assistant"]
    text: str
    timestamp_ms: int | None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class UsageRecord:
    """One non-overlapping native charge; input/output include their subsets.

    Cache read/write are subsets of input; reasoning is a subset of output.
    None means unobserved, never an inferred zero. Coarse counters have no date.
    """

    source_id: str
    timestamp_ms: int | None = None
    model: str | None = None
    provider: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    cost_kind: Literal["actual", "estimated", "unknown"] = "unknown"
    granularity: Literal["event", "session"] = "event"


@dataclass(frozen=True)
class SessionSnapshot:
    ref: SessionRef
    client: str
    title: str
    project_id: str | None
    project_label: str | None
    cwd: Path | None
    started_ms: int | None
    updated_ms: int | None
    kind: str
    archived: bool
    text_state: str
    messages: tuple[MessageRecord, ...] = ()
    usage: tuple[UsageRecord, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class LaunchSpec:
    argv: tuple[str, ...]
    cwd: Path
    env_overrides: dict[str, str] = field(default_factory=dict)
    env_remove: tuple[str, ...] = ()


@dataclass(frozen=True)
class Unavailable:
    code: str
    reason: str


def canonical_session_key(tool: str, canonical_root: Path, native_id: str) -> str:
    """Generate canonical session key for native sessions."""
    return json.dumps([tool, str(canonical_root), native_id], separators=(",", ":"))


