from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from ai_session_hub.adapters import get_adapter
from ai_session_hub.models import SourceSpec
from ai_session_hub.store import Store


@dataclass
class IndexProgress:
    status: str
    source_tool: str | None = None
    source_root: str | None = None
    scanned_sessions: int = 0
    updated_sessions: int = 0
    errors: list[str] = field(default_factory=list)


class Indexer:
    """Incremental indexing engine across all configured sources."""

    def __init__(self, store: Store, sources: list[SourceSpec]):
        self.store = store
        self.sources = list(sources)
        self._is_indexing = False

    def scan_all(
        self,
        cancelled: Callable[[], bool],
        progress_callback: Callable[[IndexProgress], None] | None = None,
    ) -> None:
        """Scan each source incrementally without coupling source failures."""
        if self._is_indexing:
            return
        self._is_indexing = True

        progress = IndexProgress(status="Starting scan...")
        last_report = 0.0

        def report(force: bool = False) -> None:
            nonlocal last_report
            now = time.monotonic()
            if progress_callback and (force or (now - last_report >= 0.1)):
                last_report = now
                progress_callback(progress)

        report(force=True)

        try:
            current_seen_keys: set[str] = set()
            successfully_scanned_sources: list[SourceSpec] = []

            for source in self.sources:
                if cancelled():
                    break

                progress.source_tool = source.tool
                progress.source_root = str(source.canonical_root)
                progress.status = f"Scanning {source.tool} ({source.canonical_root.name})..."
                report(force=True)

                self.store.update_source_status(
                    source_key=source.source_key,
                    tool=source.tool,
                    root=str(source.canonical_root),
                    profile=source.profile,
                    status="scanning",
                )

                adapter = get_adapter(source.tool)
                if not adapter:
                    err = f"No adapter registered for tool '{source.tool}'"
                    progress.errors.append(err)
                    self.store.update_source_status(
                        source_key=source.source_key,
                        tool=source.tool,
                        root=str(source.canonical_root),
                        profile=source.profile,
                        status="error",
                        error=err,
                    )
                    continue

                source_had_root_error = False
                try:
                    if not source.canonical_root.exists():
                        # Normal status for absent default roots
                        self.store.update_source_status(
                            source_key=source.source_key,
                            tool=source.tool,
                            root=str(source.canonical_root),
                            profile=source.profile,
                            status="absent",
                        )
                        continue

                    refs = list(adapter.discover(source))
                except Exception as e:
                    err = f"Discovery error on {source.canonical_root}: {e}"
                    progress.errors.append(err)
                    self.store.update_source_status(
                        source_key=source.source_key,
                        tool=source.tool,
                        root=str(source.canonical_root),
                        profile=source.profile,
                        status="error",
                        error=err,
                    )
                    source_had_root_error = True
                    refs = []

                if not source_had_root_error:
                    successfully_scanned_sources.append(source)

                for ref in refs:
                    if cancelled():
                        break

                    current_seen_keys.add(ref.key)
                    progress.scanned_sessions += 1

                    # Check revision in store
                    row = self.store.conn.execute(
                        "SELECT revision FROM sessions WHERE key = ?;",
                        (ref.key,),
                    ).fetchone()

                    if row is not None and row["revision"] == ref.revision:
                        # Unchanged: skip reading
                        report()
                        continue

                    # Read session
                    try:
                        snapshot = adapter.read(ref, cancelled)
                        if not cancelled():
                            self.store.apply(snapshot)
                            progress.updated_sessions += 1
                    except Exception as e:
                        err = f"Error reading session {ref.native_id}: {e}"
                        progress.errors.append(err)
                        self.store.mark_session_unavailable(ref.key, err)

                    report()

                if not source_had_root_error:
                    self.store.update_source_status(
                        source_key=source.source_key,
                        tool=source.tool,
                        root=str(source.canonical_root),
                        profile=source.profile,
                        status="ready",
                    )

            if not cancelled():
                # Check for removed sessions in successfully scanned sources
                for source in successfully_scanned_sources:
                    existing_rows = self.store.conn.execute(
                        "SELECT key FROM sessions WHERE tool = ? AND source_root = ?;",
                        (source.tool, str(source.canonical_root)),
                    ).fetchall()
                    for r in existing_rows:
                        k = r["key"]
                        if k not in current_seen_keys:
                            self.store.remove_session(k)

                progress.status = f"Ready ({progress.scanned_sessions} scanned, {progress.updated_sessions} updated)"
                report(force=True)
            else:
                progress.status = "Scan cancelled"
                report(force=True)

        finally:
            self._is_indexing = False
