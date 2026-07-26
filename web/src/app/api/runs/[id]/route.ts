/**
 * One run's live state.
 *
 * `progress` and `result` are both `AppState.snapshot()` payloads. While a run is in flight the
 * worker overwrites `progress`; on completion the final snapshot lands in both, so the page can
 * read a single field and not branch on status.
 */
import { NextRequest } from "next/server";

import { error, json } from "@/lib/auth";
import { sql } from "@/lib/db";

export const dynamic = "force-dynamic";

export async function GET(_request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;

  const rows = await sql`
    select r.id, r.status, r.criteria, r.created_at, r.claimed_at, r.finished_at,
           r.options, r.progress, r.result, r.error,
           w.label as worker_label,
           w.last_seen_at > now() - interval '30 seconds' as worker_online
    from runs r join workers w on w.id = r.worker_id
    where r.id = ${id}::uuid
  `;

  if (rows.length === 0) return error("no such run", 404);

  // Which VINs have prose, so the page can offer a "read the report" control without fetching a
  // few hundred KB it may never show.
  const reports = await sql`
    select vin, vendor from run_reports where run_id = ${id}::uuid order by vin, vendor
  `;

  return json({ run: rows[0], reports });
}
