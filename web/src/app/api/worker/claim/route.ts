/**
 * Hand a worker the next job in the queue.
 *
 * Two kinds of job, in priority order:
 *
 *   1. addressed to **this** machine, because the browser that submitted it claimed this one;
 *   2. unaddressed, from someone who has installed nothing.
 *
 * Own work first, so a person who set up their own laptop is never stuck behind a stranger's
 * search. `worker_id is null` second, so the site still works for everyone else.
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
      where status = 'queued' and (worker_id = ${worker.id} or worker_id is null)
      order by (worker_id is null), created_at
      limit 1
      for update skip locked
    )
    returning id, options, criteria
  `) as ClaimedRun[];

  return json({ run: rows[0] ?? null });
}
