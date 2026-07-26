"use client";

import { useState } from "react";

import { miles, money, type ReportRef, type ScoredVehicle } from "@/lib/types";

/**
 * One ranked vehicle, as a grid position.
 *
 * Report prose loads only when opened — a twelve-car run holds a few hundred KB of it, and most
 * of it is never read.
 */
export function GridSlot({
  vehicle,
  position,
  runId,
  reports,
}: {
  vehicle: ScoredVehicle;
  position: number;
  runId: string;
  reports: ReportRef[];
}) {
  const { listing } = vehicle;
  const available = reports.filter((entry) => entry.vin === listing.vin);

  return (
    <div className={`slot p${position <= 3 ? position : ""}`}>
      <div className="position">{vehicle.is_disqualified ? "DQ" : `P${position}`}</div>

      <div className="slot-main">
        <div className="slot-name">
          <a href={listing.listing_url} target="_blank" rel="noreferrer noopener">
            {listing.label}
          </a>{" "}
          {vehicle.completeness_marker === "!" && <span className="badge warn">review</span>}
        </div>
        <div className="slot-meta">
          {money(listing.landed_price)} landed · {miles(listing.mileage)}
          {listing.price_vs_kbb !== null && (
            <> · {listing.price_vs_kbb > 0 ? "+" : ""}{money(listing.price_vs_kbb)} vs KBB</>
          )}
          {vehicle.cost_per_remaining_mile !== null && (
            <> · ${vehicle.cost_per_remaining_mile.toFixed(2)}/mi</>
          )}
        </div>

        {vehicle.disqualifiers.length > 0 && (
          <div className="slot-meta" style={{ color: "var(--flag-red)" }}>
            {vehicle.disqualifiers.join(" · ")}
          </div>
        )}
        {vehicle.negatives.length > 0 && !vehicle.is_disqualified && (
          <div className="slot-meta faint">{vehicle.negatives.slice(0, 3).join(" · ")}</div>
        )}

        {available.map((entry) => (
          <ReportDisclosure key={entry.vendor} runId={runId} entry={entry} />
        ))}
      </div>

      {!vehicle.is_disqualified && (
        <div className="score">
          <b>{vehicle.score === null ? "—" : vehicle.score.toFixed(0)}</b>
          <span>SCORE</span>
        </div>
      )}
    </div>
  );
}

function ReportDisclosure({ runId, entry }: { runId: string; entry: ReportRef }) {
  const [body, setBody] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    if (body !== null || busy) return;
    setBusy(true);
    try {
      const response = await fetch(
        `/api/runs/${runId}/report?vin=${encodeURIComponent(entry.vin)}&vendor=${entry.vendor}`,
      );
      const payload = await response.json();
      setBody(response.ok ? payload.body : `Could not load: ${payload.error}`);
    } catch {
      setBody("Could not load the report.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <details className="report" onToggle={load}>
      <summary>{entry.vendor} report</summary>
      <pre>{body ?? (busy ? "Loading…" : "")}</pre>
    </details>
  );
}
