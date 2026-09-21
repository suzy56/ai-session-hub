from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

import sqlite3
import zstandard as zstd

from ai_session_hub.adapters.omp import OmpAdapter
from pathlib import Path

from ai_session_hub.models import SessionRef, SourceSpec, Unavailable
from ai_session_hub.resume import prepare_resume, run_foreground


def make_shim(bin_dir: Path, name: str, recorder_file: Path) -> Path:
    shim_path = bin_dir / name
    content = f"""#!/bin/sh
python3 -c "
import sys, os, json
data = {{
    'argv': sys.argv,
    'cwd': os.getcwd(),
    'env': dict(os.environ),
}}
with open('{recorder_file}', 'w') as f:
    json.dump(data, f)
" "$@"
"""
    shim_path.write_text(content, encoding="utf-8")
    shim_path.chmod(shim_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return shim_path


class TestResume(unittest.TestCase):
    """Test resume validation, launch specifications, and foreground execution safety."""

    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.recorder_file = self.root / "launch_record.json"

        # Create shims
        self.codex_shim = make_shim(self.bin_dir, "codex", self.recorder_file)
        self.claude_shim = make_shim(self.bin_dir, "claude", self.recorder_file)
        self.hermes_shim = make_shim(self.bin_dir, "hermes", self.recorder_file)
        self.omp_shim = make_shim(self.bin_dir, "omp", self.recorder_file)
        make_shim(self.bin_dir, "dsh", self.recorder_file)
        self.dsh_shim = make_shim(self.bin_dir, "dsh-tui", self.recorder_file)

        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin_dir}:{self.old_path}"

    def tearDown(self) -> None:
        os.environ["PATH"] = self.old_path
        self.td.cleanup()

    def test_codex_launch_spec_and_execution(self) -> None:
        # Codex with cwd containing spaces and shell metacharacters: /tmp/my dir; rm -rf/
        safe_cwd = self.root / "dir with spaces; and $SPECIAL"
        safe_cwd.mkdir()

        # Create mock session JSONL
        sess_dir = self.root / "codex_root" / "sessions"
        sess_dir.mkdir(parents=True)
        sess_file = sess_dir / "sess.jsonl"
        valid_uuid = "12345678-1234-1234-1234-123456789abc"
        sess_file.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": valid_uuid, "cwd": str(safe_cwd)}}) + "\n",
            encoding="utf-8",
        )

        source = SourceSpec(tool="codex", root=self.root / "codex_root", profile="work")
        ref = SessionRef(
            key="codex-key",
            source=source,
            native_id=valid_uuid,
            locators=(sess_file,),
            revision="r1",
        )

        spec = prepare_resume(ref)
        self.assertFalse(isinstance(spec, Unavailable))
        self.assertEqual(spec.argv[0], str(self.codex_shim.resolve()))
        self.assertIn("resume", spec.argv)
        self.assertIn("--cd", spec.argv)
        self.assertIn(str(safe_cwd.resolve()), spec.argv)
        self.assertIn("work", spec.argv)
        self.assertIn(valid_uuid, spec.argv)
        self.assertEqual(spec.env_overrides.get("CODEX_HOME"), str((self.root / "codex_root").resolve()))

        # Run foreground shim and verify actual execution
        ret = run_foreground(spec)
        self.assertEqual(ret, 0)
        self.assertTrue(self.recorder_file.exists())
        record = json.loads(self.recorder_file.read_text(encoding="utf-8"))
        self.assertEqual(record["cwd"], str(safe_cwd.resolve()))

    def test_claude_launch_spec(self) -> None:
        proj_dir = self.root / "claude_root" / "projects"
        proj_dir.mkdir(parents=True)
        sess_file = proj_dir / "test.jsonl"
        valid_uuid = "abcdef12-1234-1234-1234-abcdef123456"
        sess_file.write_text(
            json.dumps({"type": "user", "sessionId": valid_uuid, "cwd": str(self.root), "message": {"content": "hi"}}) + "\n",
            encoding="utf-8",
        )

        source = SourceSpec(tool="claude", root=self.root / "claude_root")
        ref = SessionRef(
            key="claude-key",
            source=source,
            native_id=valid_uuid,
            locators=(sess_file,),
            revision="r1",
        )

        spec = prepare_resume(ref)
        self.assertFalse(isinstance(spec, Unavailable))
        self.assertIn("--resume", spec.argv)
        self.assertIn(valid_uuid, spec.argv)
        self.assertEqual(spec.env_overrides.get("CLAUDE_CONFIG_DIR"), str((self.root / "claude_root").resolve()))

    def test_omp_launch_spec_and_env_cleanup(self) -> None:
        sess_dir = self.root / "omp_root" / "sessions"
        sess_dir.mkdir(parents=True)
        sess_file = sess_dir / "test.jsonl"
        sess_file.write_text(
            json.dumps({"type": "session", "id": "omp-test", "cwd": str(self.root)}) + "\n",
            encoding="utf-8",
        )

        # Set conflicting environment variables that must be cleared
        os.environ["OMP_PROFILE"] = "conflicting_omp"
        os.environ["PI_PROFILE"] = "conflicting_pi"

        source = SourceSpec(tool="omp", root=self.root / "omp_root")
        ref = SessionRef(
            key="omp-key",
            source=source,
            native_id="omp-test",
            locators=(sess_file,),
            revision="r1",
        )

        spec = prepare_resume(ref)
        self.assertFalse(isinstance(spec, Unavailable))
        self.assertIn("--resume", spec.argv)
        self.assertIn(str(sess_file.resolve()), spec.argv)
        self.assertIn("OMP_PROFILE", spec.env_remove)
        self.assertIn("PI_PROFILE", spec.env_remove)

        ret = run_foreground(spec)
        self.assertEqual(ret, 0)
        record = json.loads(self.recorder_file.read_text(encoding="utf-8"))
        self.assertNotIn("OMP_PROFILE", record["env"])
        self.assertNotIn("PI_PROFILE", record["env"])

    def test_dsh_validation_and_unsupported_id(self) -> None:
        dsh_root = self.root / "dsh_root"
        # Profile initialized
        profile_dir = dsh_root / "profiles" / "dsh-tui"
        pkg_json = profile_dir / "node_modules" / "@deepseek-harness-tui" / "dsh-tui" / "package.json"
        bin_js = pkg_json.parent / "bin" / "dsh-tui.js"
        bin_js.parent.mkdir(parents=True)
        pkg_json.write_text("{}", encoding="utf-8")
        bin_js.write_text("// js", encoding="utf-8")

        # White-space or padded ID rejected
        ref_padded = SessionRef(
            key="dsh-pad",
            source=SourceSpec(tool="dsh", root=dsh_root),
            native_id=" padded_id ",
            locators=(self.root / "missing.zstd",),
            revision="r1",
        )
        res = prepare_resume(ref_padded)
        self.assertTrue(isinstance(res, Unavailable))
        self.assertEqual(res.code, "invalid_identity")

    def test_unavailable_reasons_and_safeguards(self) -> None:
        # 1. Missing executable
        old_path = os.environ["PATH"]
        os.environ["PATH"] = ""
        try:
            sess_file = self.root / "test.jsonl"
            sess_file.write_text(json.dumps({"type": "session", "id": "1", "cwd": str(self.root)}) + "\n", encoding="utf-8")
            ref = SessionRef(
                key="k", source=SourceSpec(tool="omp", root=self.root), native_id="1", locators=(sess_file,), revision="r"
            )
            res = prepare_resume(ref)
            self.assertTrue(isinstance(res, Unavailable))
            self.assertEqual(res.code, "missing_executable")
        finally:
            os.environ["PATH"] = old_path

        # 2. Non-existent working directory
        sess_file = self.root / "test_bad_cwd.jsonl"
        sess_file.write_text(json.dumps({"type": "session", "id": "2", "cwd": "/nonexistent/directory/12345"}) + "\n", encoding="utf-8")
        ref_bad_cwd = SessionRef(
            key="k2", source=SourceSpec(tool="omp", root=self.root), native_id="2", locators=(sess_file,), revision="r"
        )
        res_cwd = prepare_resume(ref_bad_cwd)
        self.assertTrue(isinstance(res_cwd, Unavailable))
        self.assertEqual(res_cwd.code, "missing_cwd")

        # 3. Cursor IDE metadata-only record
        ref_cursor = SessionRef(
            key="c-k", source=SourceSpec(tool="cursor-ide", root=self.root), native_id="c1", locators=(), revision="r"
        )
        res_cursor = prepare_resume(ref_cursor)
        self.assertTrue(isinstance(res_cursor, Unavailable))
        self.assertEqual(res_cursor.code, "unsupported_resume")

    def test_omp_project_bucket_and_artifacts_remain_distinct(self) -> None:
        root = self.root / "omp"
        bucket = root / "sessions" / "-Desktop-task-advisor-project"
        bucket.mkdir(parents=True)
        main = bucket / "2026-09-21T01-18-50-266Z_session.jsonl"
        main.write_text(json.dumps({"type": "session", "id": "main", "cwd": str(self.root), "version": 3}) + "\n")
        artifacts = main.with_suffix("")
        artifacts.mkdir()
        for name in ("Researcher.jsonl", "__advisor.security.jsonl"):
            (artifacts / name).write_text(json.dumps({"type": "session", "id": name, "cwd": str(self.root)}) + "\n")
        adapter = OmpAdapter()
        refs = {ref.native_id: ref for ref in adapter.discover(SourceSpec("omp", root))}
        self.assertEqual(adapter.read(refs["main"], lambda: False).kind, "conversation")
        spec = prepare_resume(refs["main"])
        self.assertNotIsInstance(spec, Unavailable)
        self.assertEqual(run_foreground(spec), 0)
        record = json.loads(self.recorder_file.read_text())
        self.assertEqual(record["argv"][-1], str(main.resolve()))
        for name in ("Researcher.jsonl", "__advisor.security.jsonl"):
            with self.subTest(name=name):
                self.assertEqual(prepare_resume(refs[name]).code, "secondary_session")
        main.write_text(json.dumps({"type": "session", "id": "replacement", "cwd": str(self.root)}) + "\n")
        self.assertEqual(prepare_resume(refs["main"]).code, "invalid_identity")

    def test_omp_named_profile_preserves_selected_store(self) -> None:
        base = self.root / "relocated-omp"
        root = base / "profiles" / "work" / "agent"
        root.mkdir(parents=True)
        transcript = root / "session.jsonl"
        transcript.write_text(json.dumps({"type": "session", "id": "named", "cwd": str(self.root)}) + "\n")
        ref = SessionRef("named", SourceSpec("omp", root), "named", (transcript,), "r")
        with patch.dict(os.environ, {"OMP_PROFILE": "other", "PI_CODING_AGENT_DIR": str(self.root / "other"), "PI_CONFIG_DIR": "wrong"}):
            spec = prepare_resume(ref)
            self.assertNotIsInstance(spec, Unavailable)
            self.assertEqual(run_foreground(spec), 0)
        record = json.loads(self.recorder_file.read_text())
        profile = record["argv"][record["argv"].index("--profile") + 1]
        effective = Path.home() / record["env"]["PI_CONFIG_DIR"] / "profiles" / profile / "agent"
        self.assertEqual(effective.resolve(), root.resolve())
        wrong = SessionRef("wrong", SourceSpec("omp", root, "other"), "named", (transcript,), "r")
        self.assertEqual(prepare_resume(wrong).code, "unsupported_profile")

    def test_claude_rejects_replaced_and_secondary_transcripts(self) -> None:
        root = self.root / "claude"
        root.mkdir()
        transcript = root / "session.jsonl"
        native_id = "12345678-1234-1234-1234-123456789abc"
        ref = SessionRef("claude", SourceSpec("claude", root), native_id, (transcript,), "r")
        transcript.write_text(json.dumps({"type": "user", "sessionId": "87654321-1234-1234-1234-123456789abc", "cwd": str(self.root)}) + "\n")
        self.assertEqual(prepare_resume(ref).code, "invalid_identity")
        transcript.write_text(json.dumps({"type": "user", "sessionId": native_id, "cwd": str(self.root), "isSidechain": True}) + "\n")
        self.assertEqual(prepare_resume(ref).code, "secondary_session")

    def test_dsh_installed_package_bin_and_exact_header(self) -> None:
        root = self.root / "dsh"
        package = root / "profiles/dsh-tui/node_modules/@deepseek-harness-tui/dsh-tui"
        (package / "bin").mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"name": "@deepseek-harness-tui/dsh-tui", "version": "0.9.3"}))
        (package / "bin/dsh-tui.js").write_text("// initialized fixture\n")
        transcript = root / "sessions" / "project" / "exact-id" / "session.v3.jsonl.zstd"
        transcript.parent.mkdir(parents=True)
        header = {"type": "session", "id": "exact-id", "cwd": str(self.root), "version": 3}
        transcript.write_bytes(zstd.ZstdCompressor().compress((json.dumps(header) + "\n").encode()))
        ref = SessionRef("dsh", SourceSpec("dsh", root), "exact-id", (transcript,), "r")
        spec = prepare_resume(ref)
        self.assertNotIsInstance(spec, Unavailable)
        self.assertEqual(run_foreground(spec), 0)
        record = json.loads(self.recorder_file.read_text())
        self.assertEqual(record["argv"][-1], "--resume=exact-id")
        self.assertEqual(record["env"]["DSH_TUI_RESUME_SESSION"], "exact-id")
        header["id"] = "replacement"
        transcript.write_bytes(zstd.ZstdCompressor().compress((json.dumps(header) + "\n").encode()))
        self.assertEqual(prepare_resume(ref).code, "invalid_identity")
        (package / "package.json").write_text("{}")
        self.assertEqual(prepare_resume(ref).code, "unverified_compatibility")

    def test_hermes_continuation_uses_native_branch_markers_and_tip_cwd(self) -> None:
        root = self.root / "hermes" / "profiles" / "work"
        root.mkdir(parents=True)
        db = root / "state.db"
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, parent_session_id TEXT, end_reason TEXT, ended_at REAL, started_at REAL, last_activity_at REAL, source TEXT, model_config TEXT)")
            conn.execute("CREATE TABLE messages (session_id TEXT, timestamp REAL)")
            conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", [
                ("parent", str(self.root), None, "compression", 10, 1, 10, "cli", "{}"),
                ("branch", str(self.root), "parent", None, None, 40, 100, "cli", '{"_branched_from":"parent"}'),
                ("older-heartbeat", str(self.root), "parent", None, None, 20, 20, "cli", "{}"),
                ("tip", str(self.root), "parent", None, None, 15, 15, "cli", "{}"),
            ])
            conn.execute("INSERT INTO messages VALUES ('tip', 90)")
            conn.commit()
        ref = SessionRef("hermes", SourceSpec("hermes", root, "work"), "parent", (db,), "r")
        spec = prepare_resume(ref)
        self.assertNotIsInstance(spec, Unavailable)
        self.assertEqual(spec.argv[-1], "tip")
        effective_home = Path(spec.env_overrides["HERMES_HOME"]) / "profiles" / "work"
        self.assertEqual(effective_home.resolve(), root.resolve())
        wrong = SessionRef("wrong", SourceSpec("hermes", root, "other"), "parent", (db,), "r")
        self.assertEqual(prepare_resume(wrong).code, "unsupported_profile")
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("UPDATE sessions SET cwd=NULL WHERE id='tip'")
            conn.commit()
        self.assertEqual(prepare_resume(ref).code, "unknown_cwd")


if __name__ == "__main__":
    unittest.main()
