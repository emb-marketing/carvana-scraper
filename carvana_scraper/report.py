"""Stage 4b — ranked terminal table, markdown report, and the run manifest.

**The manifest is the point.** The worst failure mode of a tool like this is not a crash — it is
printing a confident table of four cars when forty matched and thirty-six were silently lost to a
selector change or a mid-run block. You might buy one of those four. So every run reports its
stage-by-stage counts, and `manifest_exit_code` returns non-zero when they fail to reconcile.

Plain text by design: no `rich` dependency, and the same rendering feeds both the terminal and the
markdown file so they can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ScoredVehicle, SearchCriteria

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = PROJECT_ROOT / "out"

SORT_KEYS = ("score", "price", "cpm", "mileage")


@dataclass
class RunManifest:
    """Stage-by-stage counts for one run, plus whatever went wrong."""

    criteria: str = ""
    pages_loaded: int = 0
    raw_records: int = 0
    parsed_listings: int = 0
    filtered_out: int = 0
    matched_before_limit: int = 0
    dropped_by_limit: int = 0
    matched: int = 0
    shortlisted: int = 0
    autocheck_parsed: int = 0
    autocheck_blocked: int = 0
    carfax_attempted: int = 0
    carfax_parsed: int = 0
    carfax_blocked: int = 0
    history_unavailable: int = 0
    from_cache: int = 0
    disqualified: int = 0
    ranked: int = 0
    needs_carfax: int = 0
    conflicts: int = 0
    puzzles_solved: int = 0
    anchor_note: str = ""
    warnings: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        """Render the manifest as aligned text."""
        rows = [
            ("search pages loaded", self.pages_loaded),
            ("raw records extracted", self.raw_records),
            ("listings parsed", self.parsed_listings),
            ("filtered out by criteria", self.filtered_out),
            ("matched criteria", self.matched_before_limit or self.matched),
            ("dropped by --limit", self.dropped_by_limit),
            ("evaluated", self.matched),
            ("shortlisted for Carfax", self.shortlisted),
            ("AutoCheck reports parsed", self.autocheck_parsed),
            ("AutoCheck blocked (will retry)", self.autocheck_blocked),
            ("Carfax attempted", self.carfax_attempted),
            ("Carfax parsed", self.carfax_parsed),
            ("Carfax not obtained (will retry)", self.carfax_blocked),
            ("history unavailable", self.history_unavailable),
            ("served from cache", self.from_cache),
            ("vendor conflicts found", self.conflicts),
            ("disqualified", self.disqualified),
            ("RANKED (both reports)", self.ranked),
            ("held out — needs Carfax", self.needs_carfax),
        ]
        width = max(len(label) for label, _ in rows)
        out = [f"  {label.ljust(width)} : {value}" for label, value in rows]
        if self.anchor_note:
            out.append(f"  {'score anchoring'.ljust(width)} : {self.anchor_note}")
        return out

    def reconciliation_problems(self) -> list[str]:
        """Count mismatches that mean the ranking cannot be trusted at face value."""
        problems: list[str] = []
        if self.matched == 0:
            problems.append("zero vehicles matched the criteria — nothing was evaluated")
        if self.matched and self.autocheck_parsed == 0:
            problems.append(
                f"{self.matched} vehicles matched but no AutoCheck report parsed — "
                "the history stage produced nothing")
        if self.shortlisted and self.carfax_parsed == 0:
            problems.append(
                f"{self.shortlisted} vehicles were shortlisted but zero Carfax reports were "
                "parsed — the main ranking will be empty")
        if self.carfax_parsed < self.shortlisted:
            problems.append(
                f"only {self.carfax_parsed} of {self.shortlisted} shortlisted vehicles got a "
                f"Carfax report ({self.carfax_blocked} not obtained) — re-run to fill the gap, "
                "or paste the reports in")
        return problems


def manifest_exit_code(manifest: RunManifest) -> int:
    """0 when the run reconciles, 1 when it does not.

    A non-zero exit is deliberate: a partial run should be visibly partial, including to any
    wrapper script or cron that only checks the status code.
    """
    return 1 if manifest.reconciliation_problems() else 0


def _sort_key(sort_by: str):
    """Return a sort key function. Unrankable vehicles always sort last."""
    def key(vehicle: ScoredVehicle):
        if sort_by == "price":
            return (0, vehicle.listing.landed_price)
        if sort_by == "mileage":
            return (0, vehicle.listing.mileage)
        if sort_by == "cpm":
            return (0, vehicle.cost_per_remaining_mile or float("inf"))
        # score: highest first, and None last
        return (1 if vehicle.score is None else 0, -(vehicle.score or 0))
    return key


def _table(vehicles: list[ScoredVehicle], show_rank: bool = True) -> list[str]:
    """Render vehicles as an aligned plain-text table."""
    if not vehicles:
        return ["  (none)"]

    header = ["#", "SCORE", "YEAR MAKE MODEL TRIM", "LANDED", "MILES", "$/MI",
              "OWN", "ACC", "AUTOCHK", "FLAG"]
    rows: list[list[str]] = []
    for index, vehicle in enumerate(vehicles, start=1):
        listing, history = vehicle.listing, vehicle.history
        accident = ("no" if history.accident_reported is False
                    else "YES" if history.accident_reported else "?")
        autocheck = (f"{history.autocheck_score}"
                     + (f"/{history.autocheck_score_low}-{history.autocheck_score_high}"
                        if history.autocheck_score_high else "")
                     if history.autocheck_score is not None else "?")
        rows.append([
            str(index) if show_rank else "-",
            f"{vehicle.score:.1f}" if vehicle.score is not None else "--",
            listing.label[:38],
            f"${listing.landed_price:,.0f}",
            f"{listing.mileage:,}",
            f"{vehicle.cost_per_remaining_mile:.3f}" if vehicle.cost_per_remaining_mile else "-",
            str(history.owner_count) if history.owner_count is not None else "?",
            accident,
            autocheck,
            vehicle.completeness_marker or "",
        ])

    widths = [max(len(header[i]), max(len(row[i]) for row in rows)) for i in range(len(header))]
    lines = ["  " + "  ".join(header[i].ljust(widths[i]) for i in range(len(header)))]
    lines.append("  " + "  ".join("-" * widths[i] for i in range(len(header))))
    for row in rows:
        lines.append("  " + "  ".join(row[i].ljust(widths[i]) for i in range(len(header))))
    return lines


def _detail_block(vehicle: ScoredVehicle, rank: int) -> list[str]:
    """The "why" block shown under each top result."""
    listing = vehicle.listing
    lines = [f"  {rank}. {listing.label} — ${listing.landed_price:,.0f} landed, "
             f"{listing.mileage:,} mi"
             + (f", score {vehicle.score:.1f}" if vehicle.score is not None else "")]
    if vehicle.positives:
        lines.append(f"     + {'; '.join(vehicle.positives[:3])}")
    if vehicle.negatives:
        lines.append(f"     - {'; '.join(vehicle.negatives[:3])}")
    if vehicle.history.conflicts:
        lines.append(f"     ! vendor disagreement: {'; '.join(vehicle.history.conflicts[:2])}")
    lines.append(f"     listing: {listing.listing_url}")
    lines.append(f"     carfax:  {listing.carfax_url}")
    return lines


def render(
    scored: list[ScoredVehicle],
    criteria: SearchCriteria,
    manifest: RunManifest,
    sort_by: str = "score",
    top_detail: int = 3,
) -> str:
    """Render the full report — manifest, ranking, held-out and disqualified sections.

    The same string goes to the terminal and to the markdown file, so the two can never disagree.
    """
    ranked = sorted([v for v in scored if v.is_rankable], key=_sort_key(sort_by))
    needs_carfax = [v for v in scored if not v.is_disqualified and not v.is_rankable]
    disqualified = [v for v in scored if v.is_disqualified]

    manifest.ranked = len(ranked)
    manifest.needs_carfax = len(needs_carfax)
    manifest.disqualified = len(disqualified)
    manifest.conflicts = sum(1 for v in scored if v.history.conflicts)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    out: list[str] = [
        "=" * 78,
        f"CARVANA RANKING — {stamp}",
        f"criteria: {criteria.describe()}",
        "=" * 78,
        "",
        "RUN MANIFEST",
        *manifest.lines(),
    ]

    problems = manifest.reconciliation_problems()
    if problems:
        out += ["", "  !! INCOMPLETE RUN — read before trusting this ranking:"]
        out += [f"     - {problem}" for problem in problems]
    for warning in manifest.warnings:
        out.append(f"     - {warning}")

    out += ["", "-" * 78,
            f"RANKED — {len(ranked)} vehicle(s) with BOTH AutoCheck and Carfax "
            f"(sorted by {sort_by})",
            "-" * 78]
    out += _table(ranked)

    if ranked:
        out += ["", f"TOP {min(top_detail, len(ranked))} — why:"]
        for rank, vehicle in enumerate(ranked[:top_detail], start=1):
            out += _detail_block(vehicle, rank)
            out.append("")

    if needs_carfax:
        # Two different situations with two different remedies — do not conflate them.
        attempted = [v for v in needs_carfax if v.carfax_attempted]
        not_shortlisted = [v for v in needs_carfax if not v.carfax_attempted]

        out += ["-" * 78,
                f"NEEDS CARFAX — {len(needs_carfax)} vehicle(s) held out of the ranking",
                "  AutoCheck data only, so these are NOT scored: AutoCheck cannot establish",
                "  structural damage or airbag deployment, and it has been observed missing an",
                "  accident that Carfax reported.",
                "-" * 78]
        if attempted:
            out += ["", f"  Carfax was attempted but not obtained ({len(attempted)}) — "
                        "blocked or unparseable.",
                    "  Re-run to retry: blocked VINs are deliberately never cached."]
            out += _table(attempted, show_rank=False)
        if not_shortlisted:
            out += ["", f"  Outside the Carfax shortlist ({len(not_shortlisted)}) — no attempt "
                        "was made.",
                    f"  Raise --top-n to include more vehicles in the Carfax pass."]
            out += _table(not_shortlisted, show_rank=False)
        out.append("")

    if disqualified:
        out += ["-" * 78, f"DISQUALIFIED — {len(disqualified)} vehicle(s)", "-" * 78]
        for vehicle in disqualified:
            out.append(f"  {vehicle.listing.label} — ${vehicle.listing.landed_price:,.0f} — "
                       f"{'; '.join(vehicle.disqualifiers)}")
            out.append(f"     {vehicle.listing.listing_url}")
        out.append("")

    out += ["  FLAG legend: DQ = disqualified · ? = history not read · ! = needs manual review",
            ""]
    return "\n".join(out)


def write_markdown(
    body: str,
    out_dir: Path | str = DEFAULT_OUT_DIR,
    stamp: str | None = None,
) -> Path:
    """Write the rendered report to a timestamped markdown file.

    Returns:
        The path written.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y-%m-%d-%H%M")
    path = out_dir / f"carvana-{stamp}.md"
    path.write_text(f"# Carvana ranking — {stamp}\n\n```\n{body}\n```\n", encoding="utf-8")
    return path
