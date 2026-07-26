/**
 * Hand a worker its next job.
 *
 * The claim is a single atomic statement. `for update skip locked` matters for the case of one
 * person running two machines on the same token: without it both could read the same queued row
 * and run the same search twice through two browser profiles.
 */
import { NextRequest } from "next/server";

import { error, json, workerFromRequest } from "@/lib/auth";
import { sql } from "@/lib/db";

export const dynamic = "force-dynamic";

type ClaimedRun = { id: string; options: Record<string, unknown>; criteria: string | null };

export async function POST(request: NextRequest) {
  const worker = await workerFromRequest(request);
  if (!worker) return error("unknown worker token", 401);

  const rows = (await sql`
    update runs set status = 'running', claimed_at = now()
    where id = (
      select id from runs
      where worker_id = ${worker.id} and status = 'queued'
      order by created_at
      limit 1
      for update skip locked
    )
    returning id, options, criteria
  `) as ClaimedRun[];

  return json({ run: rows[0] ?? null });
}
