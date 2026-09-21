from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_session_hub.config import load_config
from ai_session_hub.models import SessionRef, SessionSnapshot, SourceSpec, UsageRecord
from ai_session_hub.store import Store


class TestUsageConfig(unittest.TestCase):
    def test_local_pricing_reaches_session_accounting_without_external_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_file = root / "config.toml"
            config_file.write_text('''discover_defaults = false
[[pricing]]
model = "fixture"
input_per_million = 2.0
output_per_million = 6.0
cache_read_per_million = 0.5
''')
            config = load_config(config_file, root / "index")
            store = Store(config.data_dir / "index.sqlite3")
            try:
                ref = SessionRef("native", SourceSpec("omp", root / "source"), "n", (), "1")
                store.apply(SessionSnapshot(ref, "omp", "Native", None, None, root, None, None,
                    "conversation", False, "native", usage=(UsageRecord("call", model="fixture",
                    input_tokens=1000, output_tokens=100, cache_read_tokens=200, cache_write_tokens=0,
                    total_tokens=1100),)))
                summary = store.session_usage(["native"], pricing=config.pricing)["native"]
                self.assertAlmostEqual(summary.cost_usd, 0.0023)
                self.assertEqual(summary.estimated_records, 1)
                self.assertEqual(config.sources, [])
            finally:
                store.close()

    def test_invalid_rates_and_retired_source_fail_before_index_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            for text in (
                '[[sources]]\ntool="token-monitor"\nroot="/nonexistent"',
                '[[pricing]]\nmodel="m"\ninput_per_million=-1',
                '[[pricing]]\nmodel="m"\ninput_per_million=nan',
                '[[pricing]]\nmodel="m"\ninput_per_million=true',
                '[[pricing]]\nmodel="m"\n[[pricing]]\nmodel="m"',
            ):
                with self.subTest(text=text):
                    config.write_text(text)
                    with self.assertRaises(ValueError):
                        load_config(config, root / "index")
                    self.assertFalse((root / "index").exists())
