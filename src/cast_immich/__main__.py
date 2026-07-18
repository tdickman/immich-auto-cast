from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .app import run_from_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Rotate Immich photos on an idle Chromecast")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="TOML configuration path (default: config.toml)",
    )
    parser.add_argument("--web-host", default="127.0.0.1", help="management bind host")
    parser.add_argument("--web-port", type=int, default=8080, help="management bind port")
    args = parser.parse_args()
    asyncio.run(run_from_path(args.config, web_host=args.web_host, web_port=args.web_port))


if __name__ == "__main__":
    main()
