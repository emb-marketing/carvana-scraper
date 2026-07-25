"""SQLite cache, deliberately split by data volatility.

- **History data: cached by VIN.** Report contents do not change, and every avoided fetch is one
  less gated page load — which is the whole reason report fetching stays sustainable.
- **Listing price / mileage / availability: never cached.** Carvana drops prices and cars sell
  within days; serving a stale price would rank a car that can no longer be bought.

TTL varies by outcome, which matters more than it looks:

| status                | TTL      | why |
|-----------------------|----------|-----|
| `parsed`              | 30 days  | a real report, stable |
| `history_unavailable` | 7 days   | may be a transient parse failure worth retrying |
| `history_blocked`     | not cached | a bot challenge must always be retried, never remembered as fact |
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "cache" / "carvana.db"
DEFAULT_RAW_DIR = PROJECT_ROOT / "cache" / "raw"

# Per-status freshness windows, in days. A status absent from this map is never cached.
TTL_DAYS: dict[str, int] = {
    "parsed": 30,
    "history_unavailable": 7,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    vin         TEXT PRIMARY KEY,
    vendor      TEXT,
    status      TEXT NOT NULL,
    payload     TEXT,
    source_url  TEXT,
    fetched_at  TEXT NOT NULL
);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the cache database, creating the file and schema if needed."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    connection.commit()
    return connection


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_history(
    connection: sqlite3.Connection,
    vin: str,
    ttl_days: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    """Return a cached history entry for the VIN, or None if absent or stale.

    Args:
        connection: Open cache connection.
        vin: Vehicle identification number.
        ttl_days: Optional override of the per-status freshness windows.

    Returns:
        A dict with keys vin, vendor, status, payload (decoded), source_url, fetched_at,
        age_days — or None when there is no usable entry.
    """
    windows = ttl_days or TTL_DAYS
    row = connection.execute(
        "SELECT vin, vendor, status, payload, source_url, fetched_at FROM history WHERE vin = ?",
        (vin.upper(),),
    ).fetchone()
    if row is None:
        return None

    ttl = windows.get(row["status"])
    if ttl is None:
        return None  # this status is never served from cache

    try:
        fetched_at = datetime.fromisoformat(row["fetched_at"])
    except ValueError:
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)

    age = _now() - fetched_at
    if age > timedelta(days=ttl):
        return None

    payload = None
    if row["payload"]:
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            payload = None

    return {
        "vin": row["vin"],
        "vendor": row["vendor"],
        "status": row["status"],
        "payload": payload,
        "source_url": row["source_url"],
        "fetched_at": row["fetched_at"],
        "age_days": round(age.total_seconds() / 86400, 2),
    }


def put_history(
    connection: sqlite3.Connection,
    vin: str,
    status: str,
    vendor: str | None = None,
    payload: dict[str, Any] | None = None,
    source_url: str | None = None,
) -> bool:
    """Store a history outcome for the VIN.

    A `history_blocked` outcome is intentionally NOT persisted: a bot challenge is a transient
    condition, and remembering it would silently keep a vehicle out of future rankings.

    Returns:
        True if the entry was written, False if the status is deliberately not cached.
    """
    if status not in TTL_DAYS:
        return False
    connection.execute(
        """INSERT INTO history (vin, vendor, status, payload, source_url, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(vin) DO UPDATE SET
               vendor=excluded.vendor, status=excluded.status, payload=excluded.payload,
               source_url=excluded.source_url, fetched_at=excluded.fetched_at""",
        (vin.upper(), vendor, status,
         json.dumps(payload) if payload is not None else None,
         source_url, _now().isoformat()),
    )
    connection.commit()
    return True


def archive_raw(
    vin: str,
    content: str,
    extension: str = "txt",
    raw_dir: Path | str = DEFAULT_RAW_DIR,
) -> Path:
    """Archive a raw report payload so parser work stays offline and re-runnable.

    Returns:
        The path written.
    """
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{vin.upper()}.{extension.lstrip('.')}"
    path.write_text(content, encoding="utf-8")
    return path


def stats(connection: sqlite3.Connection) -> dict[str, int]:
    """Return cached history entry counts by status, for the run manifest."""
    rows = connection.execute(
        "SELECT status, COUNT(*) AS n FROM history GROUP BY status").fetchall()
    return {row["status"]: row["n"] for row in rows}
