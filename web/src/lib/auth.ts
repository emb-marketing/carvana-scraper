/**
 * Worker identity.
 *
 * The site PIN says a *person* is allowed in. It cannot authenticate a *machine*, and every
 * visitor shares it — so without a second secret anyone past the PIN could publish fabricated
 * results as a worker. Hence the worker token: held only by a machine running the scraper, sent
 * as `Authorization: Bearer …`.
 *
 * Stored as sha256 only, so a leaked database does not let anyone act as a worker. The lookup is
 * by hash against a unique index rather than a fetch-then-compare, which is why there is no
 * constant-time comparison here: there is no secret-to-secret comparison to make, and a 32-byte
 * random token is not guessable.
 */
import { createHash, randomBytes, timingSafeEqual } from "node:crypto";

import { sql } from "./db";

export type Worker = {
  id: string;
  label: string;
};

/** How long the one-time link a worker prints stays usable. */
export const LINK_TTL_MINUTES = 30;
export const OWNER_COOKIE = "grid_owner";

export function hashSecret(secret: string): string {
  return createHash("sha256").update(secret).digest("hex");
}

export function newSecret(): string {
  return randomBytes(32).toString("base64url");
}

/**
 * Whether a machine presented the shared enrollment key.
 *
 * Registration is the one worker route that cannot authenticate with a worker token, because the
 * token is the thing it is establishing. While the source was private, "nobody knows the protocol"
 * stood in for that check. It does not any more: the repo is public, so anyone who learns the
 * deployment URL could otherwise self-enrol, claim pool jobs, read the searches people submitted
 * and publish fabricated results.
 *
 * The key is shared by every worker rather than per-machine, because it answers a coarser question
 * — may this machine join at all — and it is delivered the same way the URL is, inside the tarball
 * behind the PIN. Compared over fixed-length digests so neither length nor first-difference
 * position leaks by timing.
 */
export function enrollmentMatches(submitted: string, expected: string): boolean {
  return timingSafeEqual(
    Buffer.from(hashSecret(submitted), "hex"),
    Buffer.from(hashSecret(expected), "hex"),
  );
}

/**
 * The machine this browser has claimed, if any.
 *
 * A browser with no claim is not an error — its searches go to the pool, which is what lets
 * someone who has installed nothing still use the site.
 */
export async function workerForOwner(ownerKey: string | undefined): Promise<Worker | null> {
  if (!ownerKey) return null;
  const rows = (await sql`
    select id, label from workers where owner_key_hash = ${hashSecret(ownerKey)}
  `) as Worker[];
  return rows[0] ?? null;
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
 * Also stamps `last_seen_at`, which is what drives the "ready / no machine running" badge. That
 * badge matters more than it looks: "I submitted and nothing happened" is the most common
 * confusion here, and the answer is almost always that no machine is running the scraper.
 */
export async function workerFromRequest(request: Request): Promise<Worker | null> {
  const token = bearerToken(request);
  if (!token) return null;

  const rows = (await sql`
    update workers set last_seen_at = now()
    where token_hash = ${hashSecret(token)}
    returning id, label
  `) as Worker[];
  return rows[0] ?? null;
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
