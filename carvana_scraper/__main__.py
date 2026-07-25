"""Package entry point so the tool runs as `python3 -m carvana_scraper`."""

import sys

from .cli import main

if __name__ == "__main__":
    # sys.exit is load-bearing: main() returns 0/1/2/130, and without this the process always
    # exited 0. That silently defeated invariant 7 — a run whose counts did not reconcile still
    # looked successful to any wrapper, cron or `&&` chain checking the status code.
    sys.exit(main())
