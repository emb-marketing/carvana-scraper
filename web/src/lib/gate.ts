/**
 * The site PIN gate.
 *
 * Vercel's own Deployment Protection would have been simpler, but password protection needs the
 * Advanced Deployment Protection add-on, and Vercel Authentication is not offered for production
 * on this plan — both refused at the API. So the gate lives in the app.
 *
 * Edge-safe on purpose: middleware runs on the edge runtime, where `node:crypto` is unavailable.
 * Everything here uses Web Crypto, which exists in both the edge and Node runtimes.
 *
 * The cookie carries no secret. It is an HMAC over a fixed marker plus an expiry, so a visitor
 * cannot mint one without SESSION_SECRET and cannot extend their own session by editing it.
 */

const COOKIE_NAME = "grid_pass";
const MARKER = "grid-v1";
/** Long enough that a shared link keeps working for weeks without re-entry. */
const TTL_SECONDS = 60 * 60 * 24 * 30;

const encoder = new TextEncoder();

async function hmac(message: string, secret: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(message));
  return Array.from(new Uint8Array(signature))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/** Length-safe, value-independent comparison. */
function sameSecret(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let difference = 0;
  for (let index = 0; index < a.length; index += 1) {
    difference |= a.charCodeAt(index) ^ b.charCodeAt(index);
  }
  return difference === 0;
}

export async function issueCookie(secret: string): Promise<string> {
  const expires = Date.now() + TTL_SECONDS * 1000;
  return `${expires}.${await hmac(`${MARKER}.${expires}`, secret)}`;
}

export async function cookieIsValid(value: string | undefined, secret: string): Promise<boolean> {
  if (!value) return false;
  const [expires, signature] = value.split(".");
  if (!expires || !signature) return false;
  if (!Number.isFinite(Number(expires)) || Number(expires) < Date.now()) return false;
  return sameSecret(signature, await hmac(`${MARKER}.${expires}`, secret));
}

/** Whether a submitted PIN matches, compared without leaking length or position via timing. */
export async function pinMatches(submitted: string, expected: string): Promise<boolean> {
  // Hashed first so the comparison is over fixed-length digests regardless of input length.
  const [a, b] = await Promise.all([hmac(submitted, "pin"), hmac(expected, "pin")]);
  return sameSecret(a, b);
}

export const gate = {
  COOKIE_NAME,
  TTL_SECONDS,
};
