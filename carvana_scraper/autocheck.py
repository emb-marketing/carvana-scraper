"""Parse an Experian AutoCheck report from its visible text.

AutoCheck is the *baseline* history source for this tool: served from Carvana's own API domain,
it returned 10/10 usable reports with no rate limit and no CAPTCHA, including for four vehicles
Carfax had already blocked.

It is emphatically **not** a replacement for Carfax. On a 2025 Tacoma, AutoCheck reported
"No Accidents or Damage Reported" for a vehicle Carfax showed had been in a towed accident. The
two are merged pessimistically in history.py for exactly this reason.

What AutoCheck adds that Carfax lacks: an auction-issue check, an insurance total-loss/transfer
check, and the AutoCheck Score — Experian's 1-100 composite plus the band comparable vehicles
fall in, which is the best normalized quality signal available anywhere in this pipeline.

Layout is "Label", then one or more value lines. Parsed from innerText, not the DOM.
"""

from __future__ import annotations

import re

from .models import STATUS_PARSED, HistoryReport

# Section label -> HistoryReport field. These are the "Vehicle History at a Glance" boxes.
GLANCE_SECTIONS: dict[str, str] = {
    "State Title Brand": "title_brand_problem",
    "Auction Brand / Issues": "auction_problem",
    "Accident / Damage": "accident_reported",
    "Open Recall Check": "open_recalls",
    "Insurance Loss / Transfer": "insurance_loss",
    "Odometer Check": "odometer_rollback",
}

# Labels that bound a section's value block.
BOUNDARY_LABELS: frozenset[str] = frozenset(
    set(GLANCE_SECTIONS) | {
        "Certified Pre-Owned", "Service / Repair", "Vehicle History at a Glance",
        "More Information", "Vehicle Usage", "AutoCheck Score", "Additional History",
        "Vehicle Use / Event History", "Owner 1", "Owner 2", "Owner 3", "Owner 4",
        "Full History Report", "Glossary", "Print Report",
    }
)

CLEAN_VERDICTS: frozenset[str] = frozenset({
    "clean", "no issue", "no issues", "no issues reported", "none",
    "no accidents or damage reported", "no accident or damage reported",
    "no open recalls", "no recalls", "no problem", "no problems",
    "no damage reported", "no accidents reported", "not reported",
})
PROBLEM_VERDICTS: frozenset[str] = frozenset({
    "issue", "issues", "issue reported", "issues reported", "problem", "problems",
    "salvage", "junk", "scrapped", "rebuilt", "rebuildable", "fire", "flood", "hail",
    "lemon", "not actual miles", "broken odometer", "exceeds mechanical limits",
    "mileage discrepancy", "suspect miles", "odometer rollback", "rollback",
    "total loss", "insurance loss", "theft", "abandoned", "grey market", "repossessed",
    "accident reported", "damage reported", "branded", "open recall", "open recalls reported",
})
# Informational values that carry no verdict.
NEUTRAL_VERDICTS: frozenset[str] = frozenset({
    "no cpo info available", "cpo", "certified pre-owned", "view vehicle label",
    "more information", "n/a", "not available", "unknown",
})

_MAX_VERDICT_WORDS = 7

_USE_TYPE_WORDS = ("personal", "lease", "fleet", "rental", "commercial", "government",
                   "taxi", "police", "livery", "driver education")


def _normalize(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip().lower().rstrip(".")


def classify_verdict(normalized: str) -> bool | None:
    """Classify one normalized value: True = problem, False = clean, None = unknown.

    Clean checks run first so that "no accidents or damage reported" is not caught by the
    "damage reported" problem token.
    """
    if normalized in CLEAN_VERDICTS:
        return False
    if normalized in NEUTRAL_VERDICTS:
        return None
    if normalized in PROBLEM_VERDICTS:
        return True
    if normalized.startswith(("no ", "none", "not reported")):
        return False
    # "1 Accident Reported", "2 Issues Reported", "3 Record(s) Reported" -> problem if it names one
    match = re.match(r"^(\d+)\s+(accident|issue|damage|problem|recall)", normalized)
    if match:
        return int(match.group(1)) > 0
    if any(token in normalized for token in PROBLEM_VERDICTS):
        return True
    return None


def _section_values(lines: list[str], label: str) -> tuple[list[str], list[str]]:
    """Collect the value lines belonging to one glance section."""
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == label)
    except StopIteration:
        return [], []

    recognized: list[str] = []
    unrecognized: list[str] = []
    for raw in lines[start + 1:]:
        line = raw.strip()
        if line in BOUNDARY_LABELS and line != label:
            break
        if not line or "\t" in raw:
            continue  # blank, or a glossary row (glossary lines are tab-separated)
        normalized = _normalize(line)
        if not normalized or len(normalized.split()) > _MAX_VERDICT_WORDS:
            continue
        if re.match(r"^last reported odometer", normalized):
            continue  # the odometer value line, handled separately
        if re.match(r"^[\d,]+\s*\(\d{2}/\d{2}/\d{4}\)$", normalized):
            continue  # "54,817 (02/24/2025)"
        verdict = classify_verdict(normalized)
        if verdict is None:
            if normalized not in NEUTRAL_VERDICTS:
                unrecognized.append(normalized)
        else:
            recognized.append(normalized)
    return recognized, unrecognized


def _decide(recognized: list[str]) -> bool | None:
    """Any problem verdict wins; otherwise clean if anything said so; else unknown."""
    verdicts = [classify_verdict(value) for value in recognized]
    if any(verdict is True for verdict in verdicts):
        return True
    if any(verdict is False for verdict in verdicts):
        return False
    return None


def _value_after(lines: list[str], label: str, max_ahead: int = 4) -> str | None:
    """First non-empty line after `label`, within `max_ahead` lines."""
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == label)
    except StopIteration:
        return None
    for raw in lines[start + 1:start + 1 + max_ahead]:
        line = raw.strip()
        if line:
            return line
    return None


def _parse_score(lines: list[str], text: str) -> tuple[int | None, int | None, int | None]:
    """AutoCheck Score plus the range comparable vehicles fall in.

    The layout is the score then the two range bounds on their own lines; the prose sentence
    "Similar vehicles usually range between 76 and 86" is the reliable cross-check.
    """
    score = None
    value = _value_after(lines, "AutoCheck Score")
    if value and re.fullmatch(r"\d{1,3}", value):
        score = int(value)

    low = high = None
    match = re.search(r"range between\s+(\d{1,3})\s+and\s+(\d{1,3})", text, re.I)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
    return score, low, high


def _parse_owner_count(text: str) -> int | None:
    """Highest "Owner N" heading present, which is AutoCheck's owner count."""
    owners = re.findall(r"^\s*Owner\s+(\d+)\s*$", text, re.M)
    if owners:
        return max(int(n) for n in owners)
    return None


def _parse_use_types(text: str) -> list[str]:
    """Vehicle usage, from the summary field and any "<Type> Use" event rows."""
    found: list[str] = []
    match = re.search(r"^Vehicle Usage\s*\n+\s*(.+)$", text, re.M)
    if match:
        for part in re.split(r"[,/]", match.group(1)):
            cleaned = part.strip().lower()
            if cleaned and cleaned not in ("no issue", "none"):
                found.append(cleaned)
    for word in _USE_TYPE_WORDS:
        if re.search(rf"^{re.escape(word.title())} Use\b", text, re.M | re.I):
            found.append(word)
    return list(dict.fromkeys(found))


def _parse_odometer(text: str) -> int | None:
    """Last reported odometer reading."""
    match = re.search(r"Last Reported Odometer:\s*\n?\s*([\d,]+)", text, re.I)
    if not match:
        match = re.search(r"last reported odometer:\s*\n?\s*([\d,]+)", text, re.I)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_autocheck_text(text: str, vin: str = "", source_url: str | None = None) -> HistoryReport:
    """Parse an AutoCheck report's visible text into a HistoryReport.

    Args:
        text: The report document's `document.body.innerText`.
        vin: VIN to attach; read from the report header when omitted.
        source_url: URL the text came from, recorded for auditability.

    Returns:
        A HistoryReport with status `parsed` and vendor `autocheck`. Unknown fields stay None;
        sections with unfamiliar values are listed in `unrecognized_sections`.

    Note:
        `structural_damage` and `airbag_deployment` are intentionally left None — AutoCheck does
        not report them as standalone checks. Only Carfax establishes those, which is why an
        AutoCheck-only vehicle can never satisfy the completeness rule on its own.
    """
    lines = text.splitlines()

    if not vin:
        match = re.search(r"VIN:\s*\n?\s*([A-HJ-NPR-Z0-9]{17})", text, re.I)
        vin = match.group(1).upper() if match else ""

    report = HistoryReport(
        vin=vin.upper(),
        status=STATUS_PARSED,
        vendor="autocheck",
        source_url=source_url,
        sources=["autocheck"],
    )

    for label, field_name in GLANCE_SECTIONS.items():
        recognized, unrecognized = _section_values(lines, label)
        verdict = _decide(recognized)
        setattr(report, field_name, verdict)
        if verdict is None and unrecognized:
            report.unrecognized_sections.append(f"{label}: {unrecognized[:3]}")

    # An insurance total-loss/transfer is the closest AutoCheck equivalent of Carfax's total loss.
    if report.insurance_loss is not None:
        report.total_loss = report.insurance_loss

    # Title brands AutoCheck checks in the State Title Brand box, per its own glossary.
    if any(line.strip() == "State Title Brand" for line in lines):
        report.title_brands = ["Fire", "Hail", "Flood", "Junk/Scrapped", "Lemon", "Salvage",
                               "Rebuilt/Rebuildable", "Odometer Brands"]

    report.owner_count = _parse_owner_count(text)
    report.use_types = _parse_use_types(text)
    report.odometer_reading = _parse_odometer(text)
    report.service_record_count = _first_int(r"(\d+)\s+Service Record\(?s?\)?\s+Reported", text)
    report.damage_reported = report.accident_reported

    score, low, high = _parse_score(lines, text)
    report.autocheck_score = score
    report.autocheck_score_low = low
    report.autocheck_score_high = high

    return report


def _first_int(pattern: str, text: str) -> int | None:
    """First integer captured by `pattern`, thousands separators removed."""
    match = re.search(pattern, text, re.I)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except (ValueError, IndexError):
        return None
