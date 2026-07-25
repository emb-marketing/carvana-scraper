"""Entry point: `python3 -m carvana_scraper.app`."""

from __future__ import annotations

import argparse
import sys

from .server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m carvana_scraper.app",
        description="Local browser UI for the Carvana ranker.",
    )
    parser.add_argument("--port", type=int, default=0,
                        help="port to bind (default: let the OS choose a free one)")
    parser.add_argument("--no-open", action="store_true",
                        help="do not open a browser window automatically")
    args = parser.parse_args(argv)

    serve(port=args.port, open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    sys.exit(main())
