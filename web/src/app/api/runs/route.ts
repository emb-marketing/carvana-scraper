/**
 * List recent runs, and queue a new one.
 *
 * Submitting needs nothing but the site PIN, which the middleware has already checked by the time
 * a request reaches here. A queued job is addressed to the pool rather than to a machine, so a
 * visitor installs nothing: whichever laptop is running a worker picks it up.
 */
import { NextRequest } from "next/server";

import { error, json } from "@/lib/auth";
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

export async function GET() {
  const runs = await sql`
    select r.id, r.status, r.criteria, r.created_at, r.finished_at,
           coalesce(w.label, 'unassigned') as worker_label
    from runs r left join workers w on w.id = r.worker_id
    order by r.created_at desc
    limit ${RECENT_LIMIT}
  `;
  return json({ runs, pool: await poolStatus() });
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

  const rows = (await sql`
    insert into runs (options, criteria)
    values (${JSON.stringify(options)}::jsonb, ${describeOptions(options)})
    returning id
  `) as { id: string }[];

  const pool = await poolStatus();
  return json({ id: rows[0].id, criteria: describeOptions(options), pool }, 201);
}
