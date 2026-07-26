/**
 * List recent runs, and queue a new one.
 *
 * Finished runs are visible to everyone past the site password — that shared pool is the whole
 * point of the site over everyone just running the local app. Submitting, by contrast, requires a
 * paired browser, because a job has to be addressed to a specific machine.
 */
import { NextRequest } from "next/server";

import { error, json, ownerKeyFrom, workerFromOwnerKey } from "@/lib/auth";
import { sql } from "@/lib/db";
import { OptionsError, buildOptions, describeOptions } from "@/lib/options";

export const dynamic = "force-dynamic";

const RECENT_LIMIT = 30;
/** One at a time per machine: the browser profile is single-instance, so a backlog cannot drain. */
const MAX_QUEUED_PER_WORKER = 1;

export async function GET() {
  const runs = await sql`
    select r.id, r.status, r.criteria, r.created_at, r.finished_at,
           w.label as worker_label,
           w.last_seen_at > now() - interval '30 seconds' as worker_online
    from runs r join workers w on w.id = r.worker_id
    order by r.created_at desc
    limit ${RECENT_LIMIT}
  `;
  return json({ runs });
}

export async function POST(request: NextRequest) {
  const body = await request.json().catch(() => null);
  if (!body) return error("expected a JSON body", 400);

  const worker = await workerFromOwnerKey(ownerKeyFrom(request, body));
  if (!worker) {
    return error("This browser is not paired with a machine.", 401,
      "Start the worker on your laptop and enter the code it prints.");
  }

  let options;
  try {
    options = buildOptions(body.options ?? {});
  } catch (exc) {
    if (exc instanceof OptionsError) return error(exc.message, 400);
    throw exc;
  }

  const queued = (await sql`
    select count(*)::int as n from runs
    where worker_id = ${worker.id} and status in ('queued', 'running')
  `) as { n: number }[];

  if (queued[0].n >= MAX_QUEUED_PER_WORKER) {
    return error("Your machine already has a run in flight.", 409,
      "Wait for it to finish — one browser profile can only run one search at a time.");
  }

  const rows = (await sql`
    insert into runs (worker_id, options, criteria)
    values (${worker.id}, ${JSON.stringify(options)}::jsonb, ${describeOptions(options)})
    returning id
  `) as { id: string }[];

  return json({ id: rows[0].id, criteria: describeOptions(options) }, 201);
}
