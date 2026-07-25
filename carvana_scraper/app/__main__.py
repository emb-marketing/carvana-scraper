"""Entry point: `python3 -m carvana_scraper.app`."""

from __future__ import annotations

import argparse
import sys

from .server import DEFAULT_PORT, serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m carvana_scraper.app",
        description="Local browser UI for the Carvana ranker.",
    )
    # None, not 0: 0 is a meaningful explicit value ("let the OS choose"), so it cannot double
    # as "unset" or it would override the fixed default port.
    parser.add_argument("--port", type=int, default=None,
                        help=f"port to bind (default {DEFAULT_PORT}, "
                             "falling back to any free port if it is busy)")
    parser.add_argument("--no-open", action="store_true",
                        help="do not open a browser window automatically")
    args = parser.parse_args(argv)

    serve(port=args.port, open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    sys.exit(main())
