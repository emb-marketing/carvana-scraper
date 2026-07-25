"""Extract vehicle records from Carvana's React Server Components flight payload.

Carvana's search results page is a Next.js App Router document: there is no __NEXT_DATA__ blob
and no client XHR carrying the inventory. The vehicle records arrive server-rendered inside the
RSC flight stream, emitted as a series of `self.__next_f.push([1, "<json-escaped chunk>"])`
calls. Concatenating and unescaping those chunks reconstructs the stream, and the records can
then be recovered from it directly.

This is deliberately the chosen ingestion path: it reads exactly what a normal page load
delivers, so there is no private API contract to drift out from under us.

Pure functions over an HTML string — no browser, no network. That makes them directly testable
against the saved fixtures in fixtures/.
"""

from __future__ import annotations

import json
import re

# Matches `self.__next_f.push([1,"…"])`, capturing the JSON string literal (including quotes)
# so json.loads can handle the unescaping.
_FLIGHT_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,\s*("(?:[^"\\]|\\.)*")\]\)')

# Fields a usable vehicle record must carry. Absence means the payload shape changed and the
# caller must fail loudly rather than return a thinner result set.
REQUIRED_FIELDS: tuple[str, ...] = ("vin", "vehicleId", "year", "make", "model", "mileage", "price")


class PayloadShapeError(RuntimeError):
    """Raised when the RSC payload no longer yields usable vehicle records.

    Carried deliberately as a hard failure: a silently empty or truncated result set would
    produce a confident ranking over cars that were never actually considered.
    """


def decode_flight_stream(html: str) -> str:
    """Reconstruct the RSC flight stream from a Carvana HTML document.

    Args:
        html: Full HTML of a Carvana search results or detail page.

    Returns:
        The concatenated, unescaped flight stream. Empty string if the page contains no chunks.
    """
    chunks = _FLIGHT_CHUNK_RE.findall(html)
    decoded: list[str] = []
    for chunk in chunks:
        try:
            decoded.append(json.loads(chunk))
        except json.JSONDecodeError:
            continue  # a malformed chunk should not discard the rest of the stream
    return "".join(decoded)


def _enclosing_object(text: str, position: int) -> str | None:
    """Return the smallest JSON object literal in `text` that encloses `position`.

    Walks backwards to the opening brace at depth zero, then forwards to its match. The flight
    stream is not valid JSON as a whole, so brace matching is how individual records are
    recovered from it.
    """
    depth, start = 0, None
    index = position
    while index >= 0:
        char = text[index]
        if char == "}":
            depth += 1
        elif char == "{":
            if depth == 0:
                start = index
                break
            depth -= 1
        index -= 1
    if start is None:
        return None

    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def extract_objects_with_key(stream: str, key: str) -> list[dict]:
    """Recover every JSON object in the flight stream that defines the given key.

    Args:
        stream: Decoded flight stream from decode_flight_stream.
        key: Object key to anchor on, e.g. "vin".

    Returns:
        Successfully parsed objects, de-duplicated by their source text span.
    """
    objects: list[dict] = []
    seen_spans: set[str] = set()
    for match in re.finditer(r'"%s"\s*:' % re.escape(key), stream):
        literal = _enclosing_object(stream, match.start())
        if not literal or literal in seen_spans:
            continue
        seen_spans.add(literal)
        try:
            parsed = json.loads(literal)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)
    return objects


def extract_vehicle_records(html: str, *, require_records: bool = True) -> list[dict]:
    """Extract complete vehicle records from a Carvana search results page.

    Args:
        html: Full HTML of a Carvana search results page.
        require_records: When True (the default), raise if no complete record is recovered.
            Pass False only when an empty page is a legitimate outcome, e.g. paging past the
            last page of results.

    Returns:
        Vehicle records, de-duplicated by VIN, in page order.

    Raises:
        PayloadShapeError: If the page yields no flight stream, or no record carrying every
            required field. Both mean the extraction contract broke and the run must stop.
    """
    stream = decode_flight_stream(html)
    if not stream:
        if not require_records:
            return []
        raise PayloadShapeError(
            "No RSC flight chunks found in the page "
            f"(html={len(html)} bytes, expected `self.__next_f.push([1,\"…\"])`). "
            "Carvana may have changed its rendering strategy — re-run docs/RECON.md discovery."
        )

    candidates = extract_objects_with_key(stream, "vin")
    records: list[dict] = []
    seen_vins: set[str] = set()
    incomplete = 0
    for candidate in candidates:
        missing = [field for field in REQUIRED_FIELDS if candidate.get(field) is None]
        if missing:
            incomplete += 1
            continue
        vin = str(candidate["vin"]).upper()
        if vin in seen_vins:
            continue
        seen_vins.add(vin)
        records.append(candidate)

    if not records and require_records:
        raise PayloadShapeError(
            f"Recovered {len(candidates)} objects containing a 'vin' key from a "
            f"{len(stream)}-byte flight stream, but none carried all required fields "
            f"{REQUIRED_FIELDS} ({incomplete} were incomplete). The record schema changed."
        )
    return records
