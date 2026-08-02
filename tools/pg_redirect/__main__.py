"""CLI: python3 -m pg_redirect --config ... --map ..."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .server import run_from_files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PGClockMG subscription URL redirect server")
    parser.add_argument(
        "-c", "--config",
        default="/etc/pg-redirect/config.json",
        help="Path to config.json",
    )
    parser.add_argument(
        "-m", "--map",
        default="/etc/pg-redirect/mapping.json",
        help="Path to subscription mapping JSON",
    )
    args = parser.parse_args(argv)

    cfg = Path(args.config)
    mapping = Path(args.map)
    if not cfg.is_file():
        print(f"config not found: {cfg}", file=sys.stderr)
        return 2
    if not mapping.is_file():
        print(f"mapping not found: {mapping}", file=sys.stderr)
        return 2

    run_from_files(cfg, mapping)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
