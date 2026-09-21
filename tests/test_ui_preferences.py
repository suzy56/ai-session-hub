from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_session_hub.i18n import load_language, save_language


class LanguagePreferenceTests(unittest.TestCase):
    def test_explicit_choice_survives_locale_change_and_replacement_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"LC_ALL": "zh_CN.UTF-8"}):
                self.assertEqual(load_language(root), ("zh-CN", None))
                save_language(root, "en")
                self.assertEqual(load_language(root), ("en", None))
            path = root / "ui-preferences.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch("ai_session_hub.i18n.os.replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    save_language(root, "zh-CN")
            self.assertEqual(load_language(root), ("en", None))
            self.assertEqual(list(root.iterdir()), [path])

    def test_invalid_preferences_are_visible_without_rewriting_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "ui-preferences.json"
            path.write_text('{"language":"unsupported"}')
            with patch.dict(os.environ, {"LC_ALL": "en_US.UTF-8"}):
                language, warning = load_language(root)
            self.assertEqual(language, "en")
            self.assertIsNotNone(warning)
            self.assertEqual(path.read_text(), '{"language":"unsupported"}')
            with self.assertRaises(ValueError):
                save_language(root, "unsupported")

    def test_save_replaces_symlink_not_external_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "external.json"
            external.write_text('{"language":"en"}')
            owned = root / "owned"
            owned.mkdir()
            (owned / "ui-preferences.json").symlink_to(external)
            save_language(owned, "zh-CN")
            self.assertEqual(load_language(owned), ("zh-CN", None))
            self.assertEqual(external.read_text(), '{"language":"en"}')
