"""Parse a Carfax vehicle-history report from its visible text.

Deliberately parses `document.body.innerText`, not the DOM: Carfax's text layout is stable and
human-readable, while its class names are generated and change without notice.

**Why the verdict column and not the prose.** Each check section carries a narrative line and a
short verdict token, and they can disagree. On a 2025 Tacoma with an accident, Structural Damage
read "CARFAX recommends that you have this vehicle inspected by a collision repair specialist."
while its verdict was "No Issues Reported" — the prose was contextual advice about the accident,
not a structural-damage finding. The verdict token is authoritative; the narrative is not.

**Calibration honesty.** The verdict vocabulary below was derived from real clean and
accident-bearing reports. Any verdict token this module does not recognize is recorded in
`unrecognized_sections` and the field is left None — never silently treated as clean. An
unfamiliar verdict means a human should read that report.
"""

from __future__ import annotations

import re

from .models import STATUS_PARSED, HistoryReport

# Section label -> HistoryReport field it decides. Labels appear as standalone lines.
CHECK_SECTIONS: dict[str, str] = {
    "Total Loss": "total_loss",
    "Structural Damage": "structural_damage",
    "Airbag Deployment": "airbag_deployment",
    "Odometer Check": "odometer_rollback",
    "Accident / Damage": "accident_reported",
    "Manufacturer Recall": "open_recalls",
    "Damage Brands": "title_brand_problem",
}

# Labels that terminate a preceding section's verdict block.
BOUNDARY_LABELS: frozenset[str] = frozenset(
    set(CHECK_SECTIONS) | {
        "Odometer Brands", "Basic Warranty", "Title History", "Ownership History",
        "Additional History", "Damage Severity Scale", "Service History",
        "Detailed History", "Accident / Damage History", "Recent Service Highlights",
        "Future Reliability", "Vehicle Details",
    }
)

# Exact (normalized) verdict tokens. Matched by equality, never substring — "No Issues Reported"
# contains "issues reported", so substring matching would invert the meaning.
CLEAN_VERDICTS: frozenset[str] = frozenset({
    "no issues reported", "no issues indicated", "no recalls reported", "no problem",
    "no damage reported", "no accidents reported", "none reported", "no problems reported",
})
PROBLEM_VERDICTS: frozenset[str] = frozenset({
    "accident reported", "accidents reported", "damage reported", "issues reported",
    "issue reported", "issues indicated", "recall reported", "recalls reported",
    "open recall", "open recalls", "total loss reported", "structural damage reported",
    "airbag deployment reported", "problem", "problem reported", "problems reported",
    "not actual mileage", "exceeds mechanical limits", "salvage", "junk", "rebuilt",
    "fire", "flood", "hail", "lemon", "odometer rollback", "rollback indicated",
    "title problem", "branded title",
})
# Tokens carrying no verdict of their own — they qualify an adjacent one or are informational.
NEUTRAL_VERDICTS: frozenset[str] = frozenset({
    "guaranteed", "warranty active", "warranty expired", "warranty unknown",
    "owner 1", "owner 2", "owner 3", "owner 4", "owner 5",
})

USE_TYPE_WORDS: tuple[str, ...] = ("personal", "lease", "fleet", "rental", "commercial",
                                   "government", "taxi", "police", "driver education")

# A verdict token is short; anything longer is narrative prose and must be ignored.
_MAX_VERDICT_WORDS = 5


def _normalize(line: str) -> str:
    """Lowercase, collapse whitespace, drop a trailing period."""
    return re.sub(r"\s+", " ", line).strip().lower().rstrip(".")


def _section_verdicts(lines: list[str], label: str) -> tuple[list[str], list[str]]:
    """Collect the verdict tokens belonging to one check section.

    Args:
        lines: The report's text, split into stripped lines.
        label: Exact section label, e.g. "Total Loss".

    Returns:
        (recognized, unrecognized) — normalized verdict tokens that are in the known vocabulary,
        and short candidate lines that are not.
    """
    try:
        start = next(i for i, line in enumerate(lines) if line == label)
    except StopIteration:
        return [], []

    recognized: list[str] = []
    unrecognized: list[str] = []
    for line in lines[start + 1:]:
        if line in BOUNDARY_LABELS and line != label:
            break
        if not line:
            continue
        normalized = _normalize(line)
        if not normalized or len(normalized.split()) > _MAX_VERDICT_WORDS:
            continue  # narrative prose
        if "|" in line:
            continue  # the brand list, e.g. "Salvage | Junk | Rebuilt | ..."
        if normalized in NEUTRAL_VERDICTS:
            continue
        if classify_verdict(normalized) is None:
            unrecognized.append(normalized)
        else:
            recognized.append(normalized)
    return recognized, unrecognized


# Prefix rules for verdicts that embed a date or severity, e.g. "Damage reported: 07/11/2024"
# or "Minor damage". Applied only after exact matching fails.
_PROBLEM_PREFIXES: tuple[str, ...] = (
    "accident reported", "damage reported", "total loss reported",
    "structural damage reported", "airbag deployment reported", "recall reported",
    "open recall", "title problem",
)
_PROBLEM_SUFFIXES: tuple[str, ...] = ("damage",)
_CLEAN_PREFIXES: tuple[str, ...] = ("no new issues", "no issues", "no accidents", "no damage",
                                    "no recalls", "no problem", "no additional")


def classify_verdict(normalized: str) -> bool | None:
    """Classify one normalized verdict token: True = problem, False = clean, None = unknown.

    Exact matching first, then prefix rules. Clean prefixes are checked BEFORE problem ones so
    that "no new issues reported" is not caught by the "issues reported" problem token, and
    "no damage reported" is not caught by the "damage" suffix rule.
    """
    if normalized in CLEAN_VERDICTS:
        return False
    if normalized in PROBLEM_VERDICTS:
        return True
    if normalized.startswith(_CLEAN_PREFIXES):
        return False
    if normalized.startswith(_PROBLEM_PREFIXES):
        return True
    if normalized.endswith(_PROBLEM_SUFFIXES):
        return True  # "minor damage", "minor to moderate damage"
    return None


def _decide(recognized: list[str]) -> bool | None:
    """Resolve a section's verdict tokens into a tri-state flag.

    Any problem verdict wins — a report listing a problem for one owner and none for another
    still describes a vehicle with that problem.
    """
    verdicts = [classify_verdict(token) for token in recognized]
    if any(verdict is True for verdict in verdicts):
        return True
    if any(verdict is False for verdict in verdicts):
        return False
    return None


def _first_int(pattern: str, text: str) -> int | None:
    """Return the first integer captured by `pattern`, with thousands separators removed."""
    match = re.search(pattern, text, re.I)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except (ValueError, IndexError):
        return None


def _parse_owner_count(text: str) -> int | None:
    """Owner count, which Carfax phrases two different ways.

    "2 Previous Owners" on multi-owner reports; "CARFAX 1-Owner Vehicle" on single-owner ones.
    """
    count = _first_int(r"(\d+)\s+Previous Owners?", text)
    if count is not None:
        return count
    return _first_int(r"CARFAX\s+(\d+)-Owner Vehicle", text)


def _parse_use_types(text: str) -> list[str]:
    """Ownership use types, from either the list form or the single-owner form.

    "Types of Owners: Personal Lease, Personal" or a standalone "Personal Vehicle".
    """
    found: list[str] = []
    match = re.search(r"Types of Owners:\s*(.+)", text, re.I)
    if match:
        for part in match.group(1).split(","):
            cleaned = part.strip().lower()
            if cleaned:
                found.append(cleaned)
    else:
        for word in USE_TYPE_WORDS:
            if re.search(rf"\b{re.escape(word)}\s+vehicle\b", text, re.I):
                found.append(word)
    # De-duplicate while preserving order.
    return list(dict.fromkeys(found))


def _parse_accidents(text: str, lines: list[str]) -> tuple[int | None, str | None]:
    """Accident count and severity.

    Count comes from the "Event N" entries in the accident history section; the header's
    "NO ACCIDENTS REPORTED" banner establishes a definite zero.
    """
    if any(_normalize(line) == "no accidents reported" for line in lines):
        return 0, None

    events = re.findall(r"^Event\s+(\d+)$", text, re.I | re.M)
    count = max((int(n) for n in events), default=None)
    if count is None and re.search(r"^ACCIDENT$", text, re.I | re.M):
        count = 1  # banner says accident but no enumerated events

    severity = None
    match = re.search(r"[Aa]ccident reported:\s*([a-z ]+?)\s*damage", text)
    if match:
        severity = match.group(1).strip().lower()
    else:
        match = re.search(r"^((?:Minor|Moderate|Severe)(?: to (?:Minor|Moderate|Severe))?)\s+Damage$",
                          text, re.M)
        if match:
            severity = match.group(1).strip().lower()
    return count, severity


def _parse_odometer(text: str) -> int | None:
    """Odometer reading from the report header.

    Anchored to the start of a line because the header runs the mileage straight into the VIN
    ("61,395 miVIN: JTEZU5JR0G5129158"), so a `\\bmi\\b` word-boundary pattern misses it and then
    matches some later service-record mileage instead — silently reporting a wrong odometer.
    """
    match = re.search(r"^([\d,]{3,})\s*mi(?![a-z])", text, re.M)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_reliability(text: str) -> str | None:
    """Carfax's 3-year reliability forecast: fair, good or great."""
    match = re.search(r"^(FAIR|GOOD|GREAT)\s+RELIABILITY$", text, re.M)
    if match:
        return match.group(1).lower()
    match = re.search(r"3-Year Reliability Forecast\s*\n\s*(Fair|Good|Great)", text, re.I)
    return match.group(1).lower() if match else None


def parse_carfax_text(text: str, vin: str = "", source_url: str | None = None) -> HistoryReport:
    """Parse a Carfax report's visible text into a HistoryReport.

    Args:
        text: The report page's `document.body.innerText`.
        vin: VIN to attach; if empty, the VIN is read from the report header.
        source_url: The URL the text came from, recorded for auditability.

    Returns:
        A HistoryReport with status `parsed`. Fields the report does not establish are left
        None, and sections with unfamiliar verdicts are listed in `unrecognized_sections`.
    """
    lines = [line.strip() for line in text.splitlines()]

    if not vin:
        match = re.search(r"VIN:\s*([A-HJ-NPR-Z0-9]{17})", text, re.I)
        vin = match.group(1).upper() if match else ""

    report = HistoryReport(
        vin=vin.upper(),
        status=STATUS_PARSED,
        vendor="carfax",
        source_url=source_url,
    )

    for label, field_name in CHECK_SECTIONS.items():
        recognized, unrecognized = _section_verdicts(lines, label)
        verdict = _decide(recognized)
        setattr(report, field_name, verdict)
        if verdict is None and unrecognized:
            report.unrecognized_sections.append(f"{label}: {unrecognized[:3]}")

    # Title brands: the brand list is fixed, so record which ones the section covers.
    if any(line == "Damage Brands" for line in lines):
        brand_line = next(
            (line for line in lines if "|" in line and re.search(r"salvage", line, re.I)), "")
        report.title_brands = [part.strip() for part in brand_line.split("|") if part.strip()]

    report.owner_count = _parse_owner_count(text)
    report.use_types = _parse_use_types(text)
    report.service_record_count = _first_int(r"(\d+)\s+Service History Records?", text)
    report.detailed_record_count = _first_int(r"(\d+)\s+Detailed Records? Available", text)
    report.odometer_reading = _parse_odometer(text)
    report.reliability_forecast = _parse_reliability(text)
    report.avg_annual_repair_cost = _first_int(r"\$([\d,]+)\s*avg per year", text)

    accident_count, severity = _parse_accidents(text, lines)
    report.accident_count = accident_count
    if severity:
        report.notes.append(f"accident severity: {severity}")
    # The header banner is a definite zero; trust it over an unparsed check section.
    if accident_count == 0 and report.accident_reported is None:
        report.accident_reported = False
    elif accident_count and report.accident_reported is None:
        report.accident_reported = True
    report.damage_reported = report.accident_reported

    return report
