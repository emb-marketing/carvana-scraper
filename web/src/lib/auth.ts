/**
 * Identity for the two kinds of caller.
 *
 * Vercel's Deployment Protection password establishes that *someone* is allowed in. It cannot say
 * *which laptop* a job belongs to, and it is shared by every visitor — so a caller past the
 * password could otherwise claim other people's jobs or publish fabricated results. Two secrets
 * supply the missing identity:
 *
 *   - the **worker token**, held by a machine, presented as `Authorization: Bearer …`
 *   - the **owner key**, held by a browser, issued when that browser redeems a pairing code
 *
 * Both are stored only as sha256. A leaked database therefore does not let anyone act as a
 * worker. Lookups are by hash against a unique index rather than a fetch-then-compare, which is
 * why there is no constant-time comparison here: there is no secret-to-secret comparison to make,
 * and a 32-byte random token is not guessable.
 */
import { createHash, randomBytes, randomInt } from "node:crypto";

import { sql } from "./db";

/** Ambiguous glyphs removed: these codes get read off a terminal and typed into a phone. */
const PAIRING_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";
const PAIRING_CODE_LENGTH = 6;
export const PAIRING_TTL_MINUTES = 15;

export type Worker = {
  id: string;
  label: string;
  owner_key_hash: string | null;
};

export function hashSecret(secret: string): string {
  return createHash("sha256").update(secret).digest("hex");
}

export function newOwnerKey(): string {
  return randomBytes(32).toString("base64url");
}

export function newPairingCode(): string {
  let code = "";
  for (let index = 0; index < PAIRING_CODE_LENGTH; index += 1) {
    code += PAIRING_ALPHABET[randomInt(PAIRING_ALPHABET.length)];
  }
  return code;
}

/** The bearer token from an Authorization header, or null when absent or malformed. */
export function bearerToken(request: Request): string | null {
  const header = request.headers.get("authorization") ?? "";
  const match = /^Bearer\s+(.+)$/i.exec(header.trim());
  return match ? match[1].trim() : null;
}

/**
 * Resolve the worker making this request, or null when the token is missing or unknown.
 *
 * Also stamps `last_seen_at`, which is what drives the "your worker is online" badge. That badge
 * matters more than it looks: "I submitted and nothing happened" is the most common confusion in
 * this design, and the answer is almost always that the worker is not running.
 */
export async function workerFromRequest(request: Request): Promise<Worker | null> {
  const token = bearerToken(request);
  if (!token) return null;

  const rows = (await sql`
    update workers set last_seen_at = now()
    where token_hash = ${hashSecret(token)}
    returning id, label, owner_key_hash
  `) as Worker[];
  return rows[0] ?? null;
}

/** Resolve the worker a browser is paired with, from its owner key. */
export async function workerFromOwnerKey(ownerKey: string | null): Promise<Worker | null> {
  if (!ownerKey) return null;
  const rows = (await sql`
    select id, label, owner_key_hash from workers
    where owner_key_hash = ${hashSecret(ownerKey)}
  `) as Worker[];
  return rows[0] ?? null;
}

/** The owner key a browser sent, from either the header or a JSON body field. */
export function ownerKeyFrom(request: Request, body?: { owner_key?: unknown }): string | null {
  const header = request.headers.get("x-owner-key");
  if (header) return header;
  return typeof body?.owner_key === "string" ? body.owner_key : null;
}

export function json(payload: unknown, status = 200): Response {
  return Response.json(payload, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}

export function error(message: string, status: number, hint = ""): Response {
  return json({ error: message, hint }, status);
}
