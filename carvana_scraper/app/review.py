"""Read the report prose the scorer discards, using the local Claude CLI. No API key.

**Scope is deliberately narrow.** The deterministic scorer already handles the numeric tradeoff, and
it does so reproducibly — invariant 6 anchors scores to --max-price/--max-miles precisely so an
identical car scores identically run to run. Letting a model reorder that would trade a real
guarantee for nothing.

What the scorer structurally cannot do is read the reports. `scoring.py` sees a handful of extracted
booleans and integers; `cache/raw/*.txt` holds the full 6-15 KB of narrative per vendor — damage
severity wording, rental/fleet/lease history, service-gap patterns, and the detail behind a
`conflicts` entry that the pipeline currently only prints. That is this module's entire job.

**The ranking is never touched.** The table is rendered from `ScoredVehicle` independently; this
returns commentary alongside it. Disqualified vehicles are not even included in the input, so
nothing here can resurrect one, and findings naming a VIN outside the supplied set are dropped in
code rather than merely discouraged in the prompt.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..cache import DEFAULT_RAW_DIR
from ..models import ScoredVehicle

# Reviewing more than a handful is not worth the context: realistically only the top few are
# candidates, and each one costs two full reports of text.
DEFAULT_REVIEW_COUNT = 5
REVIEW_TIMEOUT_S = 300

# Never haiku, per the workspace model policy. Opus is the default because this is judgment work.
DEFAULT_MODEL = "opus"
ALLOWED_MODELS: tuple[str, ...] = ("opus", "sonnet")

SEVERITIES: frozenset[str] = frozenset({"warn", "info", "good"})

# Guard against one pathological report bloating the prompt.
MAX_REPORT_CHARS = 20_000

REVIEWER_ROLE = (
    "You are a used-car history-report analyst. You read raw Carfax and AutoCheck report text and "
    "surface what structured fields miss. You never invent facts: every finding must quote the "
    "report verbatim as evidence. You do not compute or output scores, and you do not reorder "
    "vehicles — a deterministic scorer already ranks them and your output is shown beside it. "
    "You reply with a single JSON object and nothing else: no prose, no markdown, no code fences."
)

_INSTRUCTIONS = """\
Below are the top {count} vehicles from a Carvana search, already ranked by a deterministic scorer.
For each you get its structured summary and the full text of its vehicle-history reports.

The scorer already accounts for: price, mileage, KBB delta, accident severity, AutoCheck score,
owner count, use type, service density, repair cost, reliability, imperfections, recalls. Do NOT
restate those or argue about the ordering.

Your job is what the scorer cannot see, because it only reads extracted fields:
1. Detail in the report prose that the structured fields flatten or omit entirely — damage severity
   wording, rental/fleet/lease/commercial use, ownership-length patterns, service gaps, repeated
   repairs to the same area, branded-title nuance, anything a buyer would want to know.
2. Where the two vendors disagree, which reading to believe and why. Quote both.
3. Which ONE of these vehicles you would buy, and why, given they are already close on the numbers.

Reply with exactly this JSON object:

{{
  "pick_vin": "<VIN of your choice, from the list below>",
  "pick_reason": "<2-3 sentences>",
  "findings": [
    {{"vin": "<VIN>", "severity": "warn|info|good",
      "claim": "<one sentence>",
      "evidence": "<verbatim quote from that vehicle's report>"}}
  ],
  "conflict_resolutions": [
    {{"vin": "<VIN>", "field": "<e.g. accident_reported>",
      "resolution": "<which vendor to believe>", "reasoning": "<why, quoting both>"}}
  ]
}}

Rules:
- Use only the VINs listed below. A finding about any other VIN is discarded.
- "evidence" must be a literal substring of that vehicle's report text. If you cannot quote it,
  do not claim it.
- Omit "conflict_resolutions" entries where the vendors agree.
- Output the JSON object only.

=== VEHICLES ===
{vehicles}
"""


class ReviewError(RuntimeError):
    """The review could not be produced. Never fatal to a run."""


def _read_report(vin: str, vendor: str, raw_dir: Path) -> str:
    """Read an archived report, tolerating both naming schemes in cache/raw/.

    `cache.archive_raw` writes `{VIN}.{vendor}.txt`; the recon probes left `{vendor}_{VIN}.txt`.
    Reading both means a paste-completed vehicle and a scraped one look the same here.
    """
    for name in (f"{vin}.{vendor}.txt", f"{vendor}_{vin}.txt"):
        path = raw_dir / name
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            return text[:MAX_REPORT_CHARS]
    return ""


def _vehicle_block(index: int, vehicle: ScoredVehicle, raw_dir: Path) -> str:
    """Format one vehicle's structured summary plus its raw report text."""
    listing, history = vehicle.listing, vehicle.history
    lines = [
        f"--- VEHICLE {index} ---",
        f"VIN: {listing.vin}",
        f"Vehicle: {listing.label}",
        f"Deterministic score: {vehicle.score}",
        f"Landed price: ${listing.landed_price:,.0f}   Mileage: {listing.mileage:,}",
        f"Miles/year: {listing.miles_per_year:,.0f}" if listing.miles_per_year else "",
        f"KBB delta: {listing.price_vs_kbb:+,.0f}" if listing.price_vs_kbb is not None else "",
        f"Owners: {history.owner_count}   AutoCheck score: {history.autocheck_score}",
        f"Use types: {', '.join(history.use_types) or 'unknown'}",
        f"Vendors read: {history.vendor}",
    ]
    if vehicle.positives:
        lines.append(f"Scorer positives: {'; '.join(vehicle.positives)}")
    if vehicle.negatives:
        lines.append(f"Scorer negatives: {'; '.join(vehicle.negatives)}")
    if history.conflicts:
        lines.append("VENDOR DISAGREEMENTS (resolve these): " + "; ".join(history.conflicts))

    for vendor in ("carfax", "autocheck"):
        text = _read_report(listing.vin, vendor, raw_dir)
        if text:
            lines.append(f"\n### {vendor.upper()} REPORT TEXT for {listing.vin}\n{text}")
        else:
            lines.append(f"\n### {vendor.upper()} REPORT TEXT for {listing.vin}: (not archived)")

    return "\n".join(line for line in lines if line)


def build_dossier(vehicles: list[ScoredVehicle], raw_dir: Path | str = DEFAULT_RAW_DIR) -> str:
    """Build the full prompt for the reviewer."""
    raw_dir = Path(raw_dir)
    blocks = [_vehicle_block(index, vehicle, raw_dir)
              for index, vehicle in enumerate(vehicles, start=1)]
    return _INSTRUCTIONS.format(count=len(vehicles), vehicles="\n\n".join(blocks))


def select_vehicles(scored: list[ScoredVehicle], sort_key,
                    count: int = DEFAULT_REVIEW_COUNT) -> list[ScoredVehicle]:
    """Take the top N rankable vehicles.

    Disqualified and incomplete vehicles are excluded, so the reviewer is never in a position to
    argue one back into contention.
    """
    ranked = sorted([v for v in scored if v.is_rankable], key=sort_key)
    return ranked[:count]


def _extract_json_object(text: str) -> dict:
    """Parse the model's reply, tolerating code fences or surrounding prose.

    Raises:
        ReviewError: If no JSON object can be recovered.
    """
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.S)
    if fenced:
        candidate = fenced.group(1)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        brace = re.search(r"\{.*\}", candidate, re.S)
        if not brace:
            raise ReviewError("the reviewer returned no JSON object")
        try:
            parsed = json.loads(brace.group(0))
        except json.JSONDecodeError as exc:
            raise ReviewError(f"the reviewer's JSON did not parse: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ReviewError("the reviewer returned JSON that was not an object")
    return parsed


def validate_response(payload: dict, allowed_vins: set[str]) -> dict[str, Any]:
    """Enforce the review's limits in code.

    The prompt asks for these; this makes them true. A model cannot introduce a vehicle, cannot
    emit a score or an ordering that reaches the UI, and cannot make a claim about a car that was
    not in its input.

    Returns:
        The cleaned review, plus a `dropped` list naming anything discarded so the UI can show that
        filtering happened rather than hiding it.
    """
    dropped: list[str] = []

    pick_vin = payload.get("pick_vin")
    if isinstance(pick_vin, str):
        pick_vin = pick_vin.strip().upper()
    if pick_vin not in allowed_vins:
        if pick_vin:
            dropped.append(f"pick_vin {pick_vin} is not one of the reviewed vehicles")
        pick_vin = None

    findings: list[dict[str, Any]] = []
    for raw in payload.get("findings") or []:
        if not isinstance(raw, dict):
            continue
        vin = str(raw.get("vin", "")).strip().upper()
        if vin not in allowed_vins:
            dropped.append(f"finding about unknown VIN {vin or '(blank)'}")
            continue
        claim = str(raw.get("claim", "")).strip()
        if not claim:
            continue
        severity = str(raw.get("severity", "info")).strip().lower()
        findings.append({
            "vin": vin,
            "severity": severity if severity in SEVERITIES else "info",
            "claim": claim,
            "evidence": str(raw.get("evidence", "")).strip(),
        })

    resolutions: list[dict[str, Any]] = []
    for raw in payload.get("conflict_resolutions") or []:
        if not isinstance(raw, dict):
            continue
        vin = str(raw.get("vin", "")).strip().upper()
        if vin not in allowed_vins:
            dropped.append(f"conflict resolution about unknown VIN {vin or '(blank)'}")
            continue
        resolutions.append({
            "vin": vin,
            "field": str(raw.get("field", "")).strip(),
            "resolution": str(raw.get("resolution", "")).strip(),
            "reasoning": str(raw.get("reasoning", "")).strip(),
        })

    # Any ordering or score the model volunteered is discarded here rather than passed through.
    for key in ("ranking", "order", "scores", "score", "ranked_vins"):
        if key in payload:
            dropped.append(f"ignored '{key}': the deterministic ranking is authoritative")

    return {
        "pick_vin": pick_vin,
        "pick_reason": str(payload.get("pick_reason", "")).strip(),
        "findings": findings,
        "conflict_resolutions": resolutions,
        "dropped": dropped,
        "reviewed_vins": sorted(allowed_vins),
    }


def _review_via_claude(dossier: str, model: str, timeout_s: int) -> str:
    """Invoke the local Claude CLI as a pure text-in / text-out call.

    Every flag here was verified against claude 2.1.163:

    - `--mcp-config '{"mcpServers":{}}'` with `--strict-mcp-config` loads no MCP servers. The
      config needs the `mcpServers` key; a bare `{}` is rejected outright.
    - `--allowedTools ""` leaves the model no tools, so it answers from the text alone.
    - `cwd` outside the repo is load-bearing, not tidiness: run from inside carvana-scraper and the
      project's CLAUDE.md is injected into the reviewer's context. Verified by asking a probe
      whether its context mentioned Carvana — "Yes" from inside the repo, "No" from a temp dir.
    """
    command = [
        "claude", "-p", "--output-format", "json",
        "--model", model,
        "--allowedTools", "",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--append-system-prompt", REVIEWER_ROLE,
    ]
    with tempfile.TemporaryDirectory(prefix="carvana-review-") as workdir:
        try:
            completed = subprocess.run(
                command, input=dossier, capture_output=True, text=True,
                timeout=timeout_s, cwd=workdir, check=False,
            )
        except FileNotFoundError as exc:
            raise ReviewError(
                "the `claude` CLI is not on PATH — install Claude Code to use the reviewer"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ReviewError(f"the reviewer timed out after {timeout_s}s") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:400]
        raise ReviewError(f"claude exited {completed.returncode}: {detail}")

    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ReviewError(f"claude's --output-format json envelope did not parse: {exc}") from exc

    if envelope.get("is_error"):
        raise ReviewError(f"claude reported an error: {str(envelope.get('result'))[:400]}")
    result = envelope.get("result")
    if not isinstance(result, str) or not result.strip():
        raise ReviewError("claude returned an empty result")
    return result


def _review_via_codex(dossier: str, model: str, timeout_s: int) -> str:
    """Not implemented: the codex CLI is broken on this machine.

        $ codex exec --help
        Error: spawn .../@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/codex ENOENT

    The launcher is installed but its vendored binary is missing, so nothing here could be tested.
    Shipping an unverified second backend would be worse than shipping one.
    """
    raise ReviewError(
        "the codex backend is not wired up: the installed codex CLI cannot launch its own binary "
        "(ENOENT on codex-darwin-arm64). Reinstall codex, then this backend can be implemented "
        "and verified."
    )


BACKENDS = {"claude": _review_via_claude, "codex": _review_via_codex}


def run_review(
    vehicles: list[ScoredVehicle],
    backend: str = "claude",
    model: str = DEFAULT_MODEL,
    timeout_s: int = REVIEW_TIMEOUT_S,
    raw_dir: Path | str = DEFAULT_RAW_DIR,
) -> dict[str, Any]:
    """Review the given vehicles' reports and return validated commentary.

    Raises:
        ReviewError: On any failure. Callers must surface it and leave the ranking untouched.
    """
    if not vehicles:
        raise ReviewError("no vehicles with both reports to review")
    if model not in ALLOWED_MODELS:
        raise ReviewError(f"unsupported model {model!r}; choose one of {list(ALLOWED_MODELS)}")
    invoke = BACKENDS.get(backend)
    if invoke is None:
        raise ReviewError(f"unknown backend {backend!r}")

    dossier = build_dossier(vehicles, raw_dir)
    raw_reply = invoke(dossier, model, timeout_s)
    review = validate_response(_extract_json_object(raw_reply),
                              {vehicle.listing.vin for vehicle in vehicles})
    review.update({"backend": backend, "model": model, "raw": raw_reply})
    return review
