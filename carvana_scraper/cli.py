"""Command-line interface — per-run search criteria as flags, no interactive prompts.

The flag surface is fully declarative so a run can be assembled straight from a plain-language
request. `--login` is the one command that waits on a human, by design.

    python3 -m carvana_scraper --login
    python3 -m carvana_scraper --make Toyota --model 4Runner --year-min 2018 --year-max 2023 \\
        --max-price 38000 --max-miles 70000 --zip 89002 --top-n 12

**Pipeline shape**, which follows directly from what the sites allow:

1. Search and filter — one page load per results page, no per-vehicle cost.
2. AutoCheck for **every** match — Carvana-hosted, no rate limit, gives every car a baseline.
3. Provisional rank on AutoCheck + price/mileage/KBB, then take the top N.
4. Carfax for **only** those N — DataDome allows ~6 per session, and one solved puzzle restored
   access for 7+ consecutive fetches, so a 12-car shortlist costs roughly one or two puzzles.
5. Merge pessimistically, score, report.

Only vehicles with both reports enter the main ranking; the rest are held out under "needs Carfax"
and fill in on later runs, since blocked VINs are deliberately never cached.
"""

from __future__ import annotations

import argparse
import sys

from . import browser, pipeline, report as report_mod


def build_parser() -> argparse.ArgumentParser:
    """Define the full flag surface."""
    parser = argparse.ArgumentParser(
        prog="python3 -m carvana_scraper",
        description="Rank Carvana inventory by price, mileage and vehicle history.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python3 -m carvana_scraper --login\n"
            "  python3 -m carvana_scraper --make Toyota --model 4Runner --year-min 2018 \\\n"
            "      --max-price 45000 --max-miles 80000 --zip 89002 --top-n 12\n"
        ),
    )
    parser.add_argument("--login", action="store_true",
                       help="open Chrome once to establish the dedicated profile, then exit")

    criteria = parser.add_argument_group("search criteria (supplied per run)")
    criteria.add_argument("--make", help="e.g. Toyota")
    criteria.add_argument("--model", help="e.g. 4Runner")
    criteria.add_argument("--year-min", type=int)
    criteria.add_argument("--year-max", type=int)
    criteria.add_argument("--max-price", type=float,
                         help="max LANDED price (vehicle + shipping); also the scoring anchor")
    criteria.add_argument("--max-miles", type=int,
                         help="max odometer; also the scoring anchor")
    criteria.add_argument("--zip", dest="zip_code", default="89002",
                         help="delivery zip (default 89002). Compared against the zip Carvana "
                              "actually prices with. To make Carvana USE it, run --login once and "
                              "set the zip in Carvana's own location picker: that location is "
                              "captured and replayed on every later run")

    run = parser.add_argument_group("run control")
    run.add_argument("--top-n", type=int, default=12,
                    help="how many vehicles get a Carfax report (default 12)")
    run.add_argument("--max-reports", type=int, default=40,
                    help="hard cap on report fetches this run (default 40)")
    run.add_argument("--limit", type=int, help="cap how many matching listings to consider")
    run.add_argument("--max-pages", type=int, default=8,
                    help="how many search result pages to load (default 8)")
    run.add_argument("--sort", choices=report_mod.SORT_KEYS, default="score")
    run.add_argument("--search-only", action="store_true",
                    help="list matching inventory and stop; no history fetches")
    run.add_argument("--no-history", action="store_true",
                    help="skip all report fetches, score on listing data only")
    run.add_argument("--no-carfax", action="store_true",
                    help="AutoCheck only; nothing will enter the main ranking")
    run.add_argument("--no-imperfections", action="store_true",
                    help="skip Carvana's cosmetic imperfection lookup")
    run.add_argument("--unattended", action="store_true",
                    help="never pause for a puzzle; blocked reports defer to a later run")
    run.add_argument("--assist-timeout", type=int, default=240,
                    help="seconds to wait for a human to solve a puzzle (default 240)")
    run.add_argument("--debug", action="store_true")
    return parser


def _print_event(event: dict) -> None:
    """Print a pipeline event.

    Every event that corresponds to CLI output carries a `text` key holding the exact string this
    used to print inline, so the terminal output is byte-identical to the pre-refactor version.
    Events without `text` are structured-only and exist for the GUI.
    """
    text = event.get("text")
    if text is not None:
        print(text)


def run(args: argparse.Namespace) -> int:
    """Execute a full ranking run. Returns a process exit code."""
    result = pipeline.execute(pipeline.RunOptions.from_namespace(args), emit=_print_event)
    return result.exit_code


def main() -> int:
    """Parse arguments, run the pipeline, and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args()

    if args.login:
        return browser.login()
    if not pipeline.RunOptions.from_namespace(args).has_criterion():
        parser.error("give at least one search criterion, or --login. See --help.")

    try:
        return run(args)
    except browser.ProfileLockedError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Progress is cached; re-run to continue where this left off.",
              file=sys.stderr)
        return 130
