/**
 * A machine announcing that it is running the scraper.
 *
 * Called on every worker start, so it is an upsert rather than a create: restarting must not
 * orphan anything. Registering is all a machine has to do to start serving the queue.
 *
 * An **unclaimed** machine also gets a one-time link. Opening it in a browser makes that browser's
 * searches run here rather than on whoever else is online — the difference between "somebody's
 * laptop does it" and "my laptop does it". It is a link rather than a code so there is nothing to
 * read off one screen and type into another.
 */
import { NextRequest } from "next/server";

import { LINK_TTL_MINUTES, bearerToken, error, hashSecret, json, newSecret } from "@/lib/auth";
import { sql } from "@/lib/db";

export const dynamic = "force-dynamic";

type Row = { id: string; label: string; owner_key_hash: string | null; link_token: string | null };

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

  // A machine already claimed by a browser keeps its owner and gets no new link. An unclaimed one
  // gets a fresh link each start, which is also the recovery path when an old one expires.
  const link = newSecret();
  const rows = (await sql`
    insert into workers (token_hash, label, last_seen_at, link_token, link_expires_at)
    values (${hashSecret(token)}, ${label}, now(), ${link},
            now() + ${`${LINK_TTL_MINUTES} minutes`}::interval)
    on conflict (token_hash) do update set
      label = excluded.label,
      last_seen_at = now(),
      link_token = case when workers.owner_key_hash is null
                        then excluded.link_token else null end,
      link_expires_at = case when workers.owner_key_hash is null
                             then excluded.link_expires_at else null end
    returning id, label, owner_key_hash, link_token
  `) as Row[];

  const queued = (await sql`
    select count(*)::int as n from runs
    where status = 'queued' and (worker_id = ${rows[0].id} or worker_id is null)
  `) as { n: number }[];

  return json({
    worker_id: rows[0].id,
    label: rows[0].label,
    claimed: rows[0].owner_key_hash !== null,
    link_path: rows[0].link_token ? `/link?t=${rows[0].link_token}` : null,
    link_ttl_minutes: LINK_TTL_MINUTES,
    queued: queued[0].n,
  });
}
