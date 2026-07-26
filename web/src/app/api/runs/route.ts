/**
 * List recent runs, and queue a new one.
 *
 * Submitting needs nothing but the site PIN, which the middleware has already checked by the time
 * a request reaches here.
 *
 * Where the job runs depends on whether this browser has claimed a machine. If it has, the run is
 * addressed to it, so your searches use your laptop and your friend's use theirs. If it has not,
 * the run goes to the pool unaddressed and any online worker takes it — which is what lets someone
 * who has installed nothing use the site at all.
 */
import { NextRequest } from "next/server";

import { OWNER_COOKIE, error, json, workerForOwner } from "@/lib/auth";
import { sql } from "@/lib/db";
import { OptionsError, buildOptions, describeOptions } from "@/lib/options";

export const dynamic = "force-dynamic";

const RECENT_LIMIT = 30;
/**
 * Depth cap for the whole queue. A browser profile is single-instance, so a worker drains one job
 * at a time — a long backlog is a queue nobody's search ever escapes, not throughput.
 */
const MAX_QUEUE_DEPTH = 5;
/** A worker that has not polled within this window is treated as gone. */
const ONLINE_WINDOW = "45 seconds";

/**
 * Whether any machine is currently running a worker, and what to call it.
 *
 * Note the cast rather than `interval '${...}'`: an interpolation becomes a bound parameter, and a
 * parameter inside a quoted literal is not substituted — Postgres would read the placeholder text.
 */
async function poolStatus(): Promise<{ online: boolean; workers: string[] }> {
  const rows = (await sql`
    select label from workers
    where last_seen_at > now() - ${ONLINE_WINDOW}::interval
    order by last_seen_at desc
  `) as { label: string }[];
  return { online: rows.length > 0, workers: rows.map((row) => row.label) };
}

export async function GET(request: NextRequest) {
  const mine = await workerForOwner(request.cookies.get(OWNER_COOKIE)?.value);
  const runs = await sql`
    select r.id, r.status, r.criteria, r.created_at, r.finished_at,
           coalesce(w.label, 'unassigned') as worker_label
    from runs r left join workers w on w.id = r.worker_id
    order by r.created_at desc
    limit ${RECENT_LIMIT}
  `;
  return json({
    runs,
    pool: await poolStatus(),
    mine: mine ? { label: mine.label } : null,
  });
}

export async function POST(request: NextRequest) {
  const body = await request.json().catch(() => null);
  if (!body) return error("expected a JSON body", 400);

  let options;
  try {
    options = buildOptions(body.options ?? {});
  } catch (exc) {
    if (exc instanceof OptionsError) return error(exc.message, 400);
    throw exc;
  }

  const queued = (await sql`
    select count(*)::int as n from runs where status in ('queued', 'running')
  `) as { n: number }[];

  if (queued[0].n >= MAX_QUEUE_DEPTH) {
    return error("The queue is full right now.", 409,
      `${queued[0].n} searches are already waiting. Runs take a few minutes each — try shortly.`);
  }

  const mine = await workerForOwner(request.cookies.get(OWNER_COOKIE)?.value);

  const rows = (await sql`
    insert into runs (options, criteria, worker_id)
    values (${JSON.stringify(options)}::jsonb, ${describeOptions(options)}, ${mine?.id ?? null})
    returning id
  `) as { id: string }[];

  return json({
    id: rows[0].id,
    criteria: describeOptions(options),
    assigned_to: mine?.label ?? null,
    pool: await poolStatus(),
  }, 201);
}
