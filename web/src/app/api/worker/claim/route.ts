/**
 * Hand a worker the next job in the queue.
 *
 * Jobs belong to the pool, not to a machine. Whichever worker is running takes the next one, which
 * is what lets a visitor use the site with nothing installed — they submit, and the laptop that is
 * actually running does the browser work.
 *
 * The claim is a single atomic statement. `for update skip locked` is what makes several workers
 * safe: without it two could read the same queued row and run the same search twice.
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
    update runs set status = 'running', claimed_at = now(), worker_id = ${worker.id}
    where id = (
      select id from runs
      where status = 'queued'
      order by created_at
      limit 1
      for update skip locked
    )
    returning id, options, criteria
  `) as ClaimedRun[];

  return json({ run: rows[0] ?? null });
}
