/**
 * Redeem a pairing code, binding this browser to the machine that printed it.
 *
 * The code is single-use and short-lived. It is cleared on redemption in the same statement that
 * checks it, so two people racing the same code cannot both end up paired to one worker.
 */
import { NextRequest } from "next/server";

import { error, hashSecret, json, newOwnerKey } from "@/lib/auth";
import { sql } from "@/lib/db";

export const dynamic = "force-dynamic";

type Row = { id: string; label: string };

export async function POST(request: NextRequest) {
  const body = await request.json().catch(() => null);
  const code = typeof body?.code === "string" ? body.code.trim().toUpperCase() : "";
  if (!code) return error("Enter the code your worker printed.", 400);

  const ownerKey = newOwnerKey();

  const rows = (await sql`
    update workers set
      owner_key_hash = ${hashSecret(ownerKey)},
      pairing_code = null,
      pairing_expires_at = null
    where pairing_code = ${code}
      and pairing_expires_at > now()
    returning id, label
  `) as Row[];

  if (rows.length === 0) {
    return error("That code is not valid, or it has expired.", 404,
      "Restart the worker on your machine to print a fresh code.");
  }

  return json({ owner_key: ownerKey, worker_id: rows[0].id, label: rows[0].label });
}
