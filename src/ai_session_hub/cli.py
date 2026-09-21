from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-session-hub",
        description="Local AI session browser and resume TUI.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to configuration TOML file.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Path to data directory for index storage.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 0.1.0",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    from ai_session_hub.ui import run_app
    return run_app(config_path=args.config, data_dir=args.data_dir)
