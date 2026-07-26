/**
 * A worker announcing itself.
 *
 * Called on every worker start, so it is an upsert rather than a create: restarting a paired
 * machine must not orphan its jobs. An unpaired worker gets a fresh pairing code each time, which
 * is also the recovery path when a code expires — restart the worker.
 */
import { NextRequest } from "next/server";

import {
  PAIRING_TTL_MINUTES,
  bearerToken,
  error,
  hashSecret,
  json,
  newPairingCode,
} from "@/lib/auth";
import { sql } from "@/lib/db";

export const dynamic = "force-dynamic";

type Row = { id: string; label: string; owner_key_hash: string | null; pairing_code: string | null };

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

  const tokenHash = hashSecret(token);
  const code = newPairingCode();

  // A paired worker keeps its owner_key_hash and gets no new code. An unpaired one — new machine,
  // or a code that expired before anyone typed it — gets a fresh code and a fresh window.
  const rows = (await sql`
    insert into workers (token_hash, label, pairing_code, pairing_expires_at, last_seen_at)
    values (${tokenHash}, ${label}, ${code},
            now() + ${`${PAIRING_TTL_MINUTES} minutes`}::interval, now())
    on conflict (token_hash) do update set
      label = excluded.label,
      last_seen_at = now(),
      pairing_code = case when workers.owner_key_hash is null
                          then excluded.pairing_code else null end,
      pairing_expires_at = case when workers.owner_key_hash is null
                                then excluded.pairing_expires_at else null end
    returning id, label, owner_key_hash, pairing_code
  `) as Row[];

  const worker = rows[0];
  const paired = worker.owner_key_hash !== null;

  return json({
    worker_id: worker.id,
    label: worker.label,
    paired,
    pairing_code: paired ? null : worker.pairing_code,
    pairing_ttl_minutes: PAIRING_TTL_MINUTES,
  });
}
