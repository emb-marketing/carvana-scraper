import { NextRequest } from "next/server";

import { gate, issueCookie, pinMatches } from "@/lib/gate";

export const dynamic = "force-dynamic";

/**
 * Exchange the site PIN for a session cookie.
 *
 * Deliberately slow to brute-force from one client: a wrong PIN sleeps briefly before answering.
 * That is not real rate limiting, but it turns a fast online guessing loop into a slow one, and
 * the PIN is long enough that slow is enough.
 */
const WRONG_PIN_DELAY_MS = 700;

export async function POST(request: NextRequest) {
  const expected = process.env.SITE_PIN;
  const secret = process.env.SESSION_SECRET;
  if (!expected || !secret) {
    return Response.json({ error: "GRID is not configured." }, { status: 503 });
  }

  const body = await request.json().catch(() => null);
  const submitted = typeof body?.pin === "string" ? body.pin.trim() : "";

  if (!submitted || !(await pinMatches(submitted, expected))) {
    await new Promise((resolve) => setTimeout(resolve, WRONG_PIN_DELAY_MS));
    return Response.json({ error: "That PIN is not right." }, { status: 401 });
  }

  const response = Response.json({ ok: true });
  response.headers.append(
    "Set-Cookie",
    [
      `${gate.COOKIE_NAME}=${await issueCookie(secret)}`,
      "Path=/",
      `Max-Age=${gate.TTL_SECONDS}`,
      "HttpOnly",
      "Secure",
      "SameSite=Lax",
    ].join("; "),
  );
  return response;
}
