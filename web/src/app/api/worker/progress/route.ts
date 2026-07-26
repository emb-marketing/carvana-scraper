/**
 * A live progress snapshot from a running job.
 *
 * The body is `AppState.snapshot()` verbatim — the same payload the local app's own page polls —
 * so the run view renders one contract regardless of which front end produced it.
 */
import { NextRequest } from "next/server";

import { error, json, workerFromRequest } from "@/lib/auth";
import { sql } from "@/lib/db";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest) {
  const worker = await workerFromRequest(request);
  if (!worker) return error("unknown worker token", 401);

  const body = await request.json().catch(() => null);
  const runId = body?.run_id;
  if (typeof runId !== "string") return error("run_id is required", 400);

  // Scoped to this worker so a token cannot write progress onto someone else's run.
  const rows = await sql`
    update runs set progress = ${JSON.stringify(body.progress ?? {})}::jsonb
    where id = ${runId}::uuid and worker_id = ${worker.id}
    returning id
  `;
  if (rows.length === 0) return error("no such run for this worker", 404);

  return json({ ok: true });
}
