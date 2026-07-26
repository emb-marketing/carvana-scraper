/**
 * A finished job: either a result or a failure.
 *
 * Report prose arrives here alongside the result but is stored in its own table. A twelve-car run
 * carries a few hundred KB of Carfax and AutoCheck text; keeping it out of `runs.result` is what
 * lets the run view poll every two seconds without re-shipping all of it.
 */
import { NextRequest } from "next/server";

import { error, json, workerFromRequest } from "@/lib/auth";
import { sql } from "@/lib/db";

export const dynamic = "force-dynamic";

const MAX_REPORT_CHARS = 200_000;
const VENDORS = new Set(["carfax", "autocheck"]);

type ReportRow = { vin: string; vendor: string; body: string };

/** Keep only well-formed rows — a malformed one must not fail an otherwise good run. */
function usableReports(input: unknown): ReportRow[] {
  if (!Array.isArray(input)) return [];
  return input.flatMap((row) => {
    const vin = typeof row?.vin === "string" ? row.vin.trim().toUpperCase() : "";
    const vendor = typeof row?.vendor === "string" ? row.vendor.trim().toLowerCase() : "";
    const body = typeof row?.body === "string" ? row.body : "";
    if (!vin || !VENDORS.has(vendor) || !body) return [];
    return [{ vin, vendor, body: body.slice(0, MAX_REPORT_CHARS) }];
  });
}

export async function POST(request: NextRequest) {
  const worker = await workerFromRequest(request);
  if (!worker) return error("unknown worker token", 401);

  const body = await request.json().catch(() => null);
  const runId = body?.run_id;
  if (typeof runId !== "string") return error("run_id is required", 400);

  const failed = Boolean(body?.error);
  const status = failed ? "failed" : "done";

  const rows = await sql`
    update runs set
      status = ${status},
      finished_at = now(),
      result = ${body?.result ? JSON.stringify(body.result) : null}::jsonb,
      progress = coalesce(${body?.result ? JSON.stringify(body.result) : null}::jsonb, progress),
      error = ${body?.error ? JSON.stringify(body.error) : null}::jsonb
    where id = ${runId}::uuid and worker_id = ${worker.id}
    returning id
  `;
  if (rows.length === 0) return error("no such run for this worker", 404);

  const reports = usableReports(body?.reports);
  for (const report of reports) {
    // One statement per row rather than a bulk insert: the neon() tagged template does not
    // interpolate arrays as multi-row VALUES, and a dozen small writes on a finished run is not
    // worth a query builder.
    await sql`
      insert into run_reports (run_id, vin, vendor, body)
      values (${runId}::uuid, ${report.vin}, ${report.vendor}, ${report.body})
      on conflict (run_id, vin, vendor) do update set body = excluded.body
    `;
  }

  return json({ ok: true, status, reports: reports.length });
}
