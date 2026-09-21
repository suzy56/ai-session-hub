from __future__ import annotations

from typing import Callable, Iterable

from ai_session_hub.adapters import register_adapter
from ai_session_hub.models import (
    LaunchSpec,
    SessionRef,
    SessionSnapshot,
    SourceSpec,
    Unavailable,
    canonical_session_key,
)
from ai_session_hub.source_io import (
    get_db_and_wal_stamp,
    normalize_timestamp_ms,
    open_ro_sqlite,
)


class CursorIdeAdapter:
    """Adapter for Cursor IDE conversation search database."""

    def discover(self, source: SourceSpec) -> Iterable[SessionRef]:
        db_path = source.root / "conversation-search.db"
        if not db_path.is_file():
            return

        db_rev = get_db_and_wal_stamp(db_path)
        if db_rev is None:
            return

        conn = open_ro_sqlite(db_path)
        try:
            cursor = conn.execute("SELECT id, updated_at FROM conversations;")
            for row in cursor:
                conv_id = str(row["id"])
                updated_at = row["updated_at"]
                yield SessionRef(
                    key=canonical_session_key("cursor-ide", source.canonical_root, conv_id),
                    source=source,
                    native_id=conv_id,
                    locators=(db_path,),
                    revision=f"{db_rev}:{updated_at}",
                )
        finally:
            conn.close()

    def read(self, ref: SessionRef, cancelled: Callable[[], bool]) -> SessionSnapshot:
        db_path = ref.locators[0]
        conn = open_ro_sqlite(db_path)
        try:
            cursor = conn.execute(
                "SELECT id, title, updated_at, is_archived, source, scope FROM conversations WHERE id = ?;",
                (ref.native_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return SessionSnapshot(
                    ref=ref,
                    client="cursor-ide",
                    title=ref.native_id,
                    project_id=None,
                    project_label=None,
                    cwd=None,
                    started_ms=None,
                    updated_ms=None,
                    kind="ide",
                    archived=False,
                    text_state="metadata-only",
                    messages=(),
                    warnings=("Conversation not found in database",),
                )

            conv_id = str(row["id"])
            title = row["title"] or conv_id
            scope = row["scope"] or None
            updated_ms = normalize_timestamp_ms(row["updated_at"])
            archived = bool(row["is_archived"])

            return SessionSnapshot(
                ref=ref,
                client="cursor-ide",
                title=title,
                project_id=scope,
                project_label=scope,
                cwd=None,
                started_ms=None,
                updated_ms=updated_ms,
                kind="ide",
                archived=archived,
                text_state="metadata-only",
                messages=(),
            )
        finally:
            conn.close()

    def prepare_resume(self, ref: SessionRef) -> LaunchSpec | Unavailable:
        return Unavailable(
            "unsupported_resume",
            "Cursor IDE conversations are metadata-only in this milestone and cannot be resumed via CLI",
        )


register_adapter("cursor-ide", CursorIdeAdapter())
