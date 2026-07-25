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

from . import browser, history, report as report_mod, scoring, search, vdp
from .cache import connect, stats as cache_stats
from .models import STATUS_BLOCKED, STATUS_PARSED, Listing, SearchCriteria


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
                         help="delivery zip (default 89002). Recorded and compared against the "
                              "zip Carvana actually prices with, but it CANNOT force that zip — "
                              "set the real one in Carvana's location picker during --login")

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


def _print_listings(listings: list[Listing]) -> None:
    """Compact inventory dump for --search-only."""
    print(f"\n{len(listings)} matching listing(s):\n")
    for index, listing in enumerate(listings, start=1):
        kbb = (f"  kbb ${listing.kbb_value:,.0f}"
               f" ({listing.price_vs_kbb:+,.0f})" if listing.kbb_value else "")
        print(f"  {index:>3}. {listing.label[:42]:<42} ${listing.landed_price:>9,.0f}"
              f"  {listing.mileage:>7,} mi{kbb}")
        print(f"       {listing.vin}  {listing.listing_url}")


def _provisional_rank(
    listings: list[Listing],
    histories: dict[str, object],
    criteria: SearchCriteria,
    config: dict,
) -> list[Listing]:
    """Order matches by an AutoCheck-only score to choose who gets a Carfax fetch.

    Uses the same scoring machinery as the final ranking, so the shortlist is chosen on the same
    basis it will later be judged on — just without the Carfax-only signals.
    """
    pairs = [(listing, histories[listing.vin], None) for listing in listings
             if listing.vin in histories]
    scored, _ = scoring.score_all(pairs, criteria, config)

    def provisional_key(vehicle):
        # score is None for incomplete histories (always, at this stage), so rank on the
        # component average instead — it reflects the signals we do have.
        components = vehicle.components
        average = sum(components.values()) / len(components) if components else 0.0
        return (-average, vehicle.listing.landed_price)

    ordered = sorted(scored, key=provisional_key)
    return [vehicle.listing for vehicle in ordered]


def run(args: argparse.Namespace) -> int:
    """Execute a full ranking run. Returns a process exit code."""
    criteria = SearchCriteria(
        make=args.make, model=args.model,
        year_min=args.year_min, year_max=args.year_max,
        max_price=args.max_price, max_miles=args.max_miles,
        zip_code=args.zip_code, top_n=args.top_n, max_reports=args.max_reports,
    )
    config = scoring.load_config()
    manifest = report_mod.RunManifest(criteria=criteria.describe())
    connection = connect()

    histories: dict[str, object] = {}
    imperfection_counts: dict[str, int | None] = {}

    with browser.session() as context:
        page = context.pages[0] if context.pages else context.new_page()

        # ---- stage 1: inventory ----
        print(f"\n[1/4] searching: {criteria.describe()}")
        listings, search_stats = search.collect_listings(
            page, criteria, max_pages=args.max_pages, hard_limit=args.limit)
        manifest.pages_loaded = search_stats["pages_loaded"]
        manifest.raw_records = search_stats["raw_records"]
        manifest.parsed_listings = search_stats["parsed_listings"]
        manifest.filtered_out = search_stats["filtered_out"]
        manifest.matched = search_stats["matched"]
        manifest.matched_before_limit = search_stats.get("matched_before_limit", 0)
        manifest.dropped_by_limit = search_stats.get("dropped_by_limit", 0)
        if manifest.dropped_by_limit:
            manifest.warnings.append(
                f"--limit {args.limit} dropped {manifest.dropped_by_limit} of "
                f"{manifest.matched_before_limit} matching vehicles before any history was read")
        if search_stats.get("pagination_effective") is False:
            manifest.warnings.append(
                f"pagination stopped early: {search_stats.get('stopped_because')}")
        if search_stats.get("priced_zip") and search_stats["priced_zip"] != criteria.zip_code:
            manifest.warnings.append(
                f"prices reflect zip {search_stats['priced_zip']}, not {criteria.zip_code}")
        print(f"      {manifest.matched} matched of {manifest.parsed_listings} parsed")

        if args.search_only:
            _print_listings(listings)
            return 0
        if not listings:
            print("\nNo vehicles matched. Nothing to rank.")
            print("\n".join(manifest.lines()))
            return 1

        # ---- stage 2: AutoCheck for every match ----
        if args.no_history:
            print("\n[2/4] --no-history: skipping all report fetches")
        else:
            budget = min(len(listings), args.max_reports)
            print(f"\n[2/4] AutoCheck for {budget} vehicle(s) "
                  f"(Carvana-hosted, no rate limit observed)")
            for index, listing in enumerate(listings[:budget], start=1):
                print(f"  [{index}/{budget}] {listing.label} {listing.vin}")
                report, from_cache = history.get_or_fetch(
                    context, page, listing, connection, want_carfax=False,
                    allow_manual_assist=False, verbose=True)
                histories[listing.vin] = report
                if from_cache:
                    manifest.from_cache += 1
                if getattr(report, "status", None) == STATUS_PARSED:
                    manifest.autocheck_parsed += 1
                elif getattr(report, "status", None) == STATUS_BLOCKED:
                    manifest.carfax_blocked += 1
                else:
                    manifest.history_unavailable += 1

                if not args.no_imperfections:
                    imperfections = vdp.fetch_imperfections(page, listing.stock_number)
                    imperfection_counts[listing.vin] = (
                        len(imperfections) if imperfections is not None else None)
                    if imperfections:
                        print(f"    [vdp] imperfections: "
                              f"{vdp.summarize_imperfections(imperfections)}")
                browser.human_pause(2.0, 5.0)

        # ---- stage 3: Carfax for the shortlist ----
        shortlist_vins: set[str] = set()
        if not args.no_history and not args.no_carfax and histories:
            shortlist = _provisional_rank(listings, histories, criteria, config)[:args.top_n]
            shortlist_vins = {listing.vin for listing in shortlist}
            manifest.shortlisted = len(shortlist)
            print(f"\n[3/4] Carfax for the top {len(shortlist)} "
                  f"(DataDome allows ~6/session; "
                  f"{'unattended — will defer blocks' if args.unattended else 'will pause for puzzles'})")
            for index, listing in enumerate(shortlist, start=1):
                print(f"  [{index}/{len(shortlist)}] {listing.label} {listing.vin}")
                manifest.carfax_attempted += 1
                report, from_cache = history.get_or_fetch(
                    context, page, listing, connection, want_carfax=True,
                    allow_manual_assist=not args.unattended,
                    assist_timeout_s=args.assist_timeout, verbose=True)
                histories[listing.vin] = report
                sources = getattr(report, "sources", []) or []
                if "carfax" in sources or getattr(report, "vendor", "") == "carfax":
                    manifest.carfax_parsed += 1
                elif getattr(report, "status", None) == STATUS_BLOCKED:
                    manifest.carfax_blocked += 1
                browser.human_pause()
        elif args.no_carfax:
            print("\n[3/4] --no-carfax: skipping Carfax; nothing will enter the main ranking")

    # ---- stage 4: score and report ----
    print("\n[4/4] scoring")
    pairs = [(listing, histories[listing.vin], imperfection_counts.get(listing.vin))
             for listing in listings if listing.vin in histories]
    scored, anchor_info = scoring.score_all(pairs, criteria, config)
    for vehicle in scored:
        vehicle.carfax_attempted = vehicle.listing.vin in shortlist_vins
    manifest.anchor_note = anchor_info["note"]
    if not anchor_info["anchored"]:
        manifest.warnings.append(anchor_info["note"])

    body = report_mod.render(scored, criteria, manifest, sort_by=args.sort)
    print("\n" + body)
    path = report_mod.write_markdown(body)
    print(f"report written: {path}")
    print(f"history cache: {cache_stats(connection)}")

    return report_mod.manifest_exit_code(manifest)


def main() -> int:
    """Parse arguments, run the pipeline, and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args()

    if args.login:
        return browser.login()
    if not any([args.make, args.model, args.year_min, args.year_max,
                args.max_price, args.max_miles]):
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
