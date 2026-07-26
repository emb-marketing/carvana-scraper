/**
 * A machine announcing that it is running the scraper.
 *
 * Called on every worker start, so it is an upsert rather than a create: restarting must not
 * orphan anything. There is no pairing step — jobs go to the queue, not to a named machine, so a
 * worker only has to say "I exist and I am alive" for the site to show a green light and start
 * handing it work.
 */
import { NextRequest } from "next/server";

import { bearerToken, error, hashSecret, json } from "@/lib/auth";
import { sql } from "@/lib/db";

export const dynamic = "force-dynamic";

type Row = { id: string; label: string };

export async function POST(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return error("missing worker token", 401);

  let label = "unnamed machine";
  try {
    const body = await request.json();
    if (typeof body?.label === "string" && body.label.trim()) {
      label = body.label.trim().slice(0, 60);
    }
  } catch {
    // A bodyless register is fine; the label is only a display name.
  }

  const rows = (await sql`
    insert into workers (token_hash, label, last_seen_at)
    values (${hashSecret(token)}, ${label}, now())
    on conflict (token_hash) do update set
      label = excluded.label,
      last_seen_at = now()
    returning id, label
  `) as Row[];

  const queued = (await sql`
    select count(*)::int as n from runs where status = 'queued'
  `) as { n: number }[];

  return json({ worker_id: rows[0].id, label: rows[0].label, queued: queued[0].n });
}
