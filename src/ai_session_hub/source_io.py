from __future__ import annotations

import datetime
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Generator

# ANSI / terminal escape sequence patterns
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ESC_RE = re.compile(r"\x1b[@-Z\\-_]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_display_text(text: str) -> str:
    """Strip terminal control, ANSI color, and OSC sequences while preserving Unicode and formatting."""
    if not text:
        return ""
    # Strip OSC sequences first, then CSI, then remaining 2-byte escape sequences
    text = _OSC_RE.sub("", text)
    text = _CSI_RE.sub("", text)
    text = _ESC_RE.sub("", text)
    # Strip unprintable control characters except \t, \n, \r
    text = _CONTROL_RE.sub("", text)
    return text


def open_ro_sqlite(path: Path | str, timeout: float = 5.0) -> sqlite3.Connection:
    """Open an SQLite database in strictly read-only mode with query_only enabled."""
    target_path = Path(path).resolve()
    if not target_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {target_path}")

    # Use file URI with mode=ro. Never use immutable=1 on live WAL databases.
    uri = f"{target_path.as_uri()}?mode=ro"
    conn = sqlite3.connect(
        uri,
        uri=True,
        timeout=timeout,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    # Ensure query_only is enforced
    conn.execute("PRAGMA query_only = ON;")
    # Set short busy timeout in ms
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)};")
    return conn


def get_file_stat_stamp(path: Path) -> str | None:
    """Return a composite stamp (inode, size, mtime_ns) for change detection."""
    try:
        st = path.stat()
        return f"{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"
    except (FileNotFoundError, PermissionError):
        return None


def get_db_and_wal_stamp(db_path: Path) -> str | None:
    """Return composite stamp covering both the main SQLite DB and its WAL file."""
    db_stamp = get_file_stat_stamp(db_path)
    if db_stamp is None:
        return None
    wal_path = db_path.with_name(db_path.name + "-wal")
    wal_stamp = get_file_stat_stamp(wal_path) or "no-wal"
    return f"{db_stamp}|{wal_stamp}"


def iter_jsonl(
    path: Path,
    cancelled: Callable[[], bool] | None = None,
) -> Generator[tuple[int, dict[str, Any], list[str]], None, None]:
    """Iterate over lines in a JSONL file safely.

    Handles complete lines, tolerates malformed lines with warnings, and permits
    a valid final line without trailing newline. If an incomplete final line cannot
    be decoded as JSON, a warning is recorded without failing the whole file.
    """
    if not path.is_file():
        return

    with path.open("rb") as f:
        line_no = 0
        accumulated_warnings: list[str] = []
        while True:
            if cancelled is not None and cancelled():
                return
            raw_line = f.readline()
            if not raw_line:
                break
            line_no += 1

            # Check if line was empty or just newline
            stripped = raw_line.strip()
            if not stripped:
                continue

            try:
                decoded = stripped.decode("utf-8")
            except UnicodeDecodeError as e:
                try:
                    decoded = stripped.decode("utf-8", errors="replace")
                    accumulated_warnings.append(f"Line {line_no}: Unicode decode error: {e}")
                except Exception:
                    continue

            try:
                obj = json.loads(decoded)
                if isinstance(obj, dict):
                    current_warnings = list(accumulated_warnings)
                    accumulated_warnings.clear()
                    yield line_no, obj, current_warnings
                else:
                    accumulated_warnings.append(f"Line {line_no}: Top-level JSON is not an object ({type(obj).__name__})")
            except json.JSONDecodeError as err:
                accumulated_warnings.append(f"Line {line_no}: Malformed JSON: {err}")

def normalize_timestamp_ms(val: Any) -> int | None:
    """Normalize various timestamp representations to UTC integer milliseconds."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        # If smaller than 1e11, assume seconds (e.g. 1.7e9)
        if val < 1e11:
            return int(val * 1000)
        return int(val)
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        # Try pure numeric string
        try:
            num = float(val)
            if num < 1e11:
                return int(num * 1000)
            return int(num)
        except ValueError:
            pass
        # Try ISO datetime parsing
        try:
            # Replace trailing Z with +00:00 for fromisoformat
            iso_str = val
            if iso_str.endswith("Z"):
                iso_str = iso_str[:-1] + "+00:00"
            dt = datetime.datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    if isinstance(val, (datetime.datetime, datetime.date)):
        if isinstance(val, datetime.date) and not isinstance(val, datetime.datetime):
            val = datetime.datetime(val.year, val.month, val.day, tzinfo=datetime.timezone.utc)
        elif val.tzinfo is None:
            val = val.replace(tzinfo=datetime.timezone.utc)
        return int(val.timestamp() * 1000)
    return None
