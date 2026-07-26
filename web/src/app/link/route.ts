import { NextRequest, NextResponse } from "next/server";

import { OWNER_COOKIE, hashSecret, newSecret } from "@/lib/auth";
import { sql } from "@/lib/db";

export const dynamic = "force-dynamic";

/**
 * Claim a machine for this browser, from the one-time link its worker printed.
 *
 * A GET that opens straight from the terminal, so there is no code to read off one screen and
 * type into another. The token is single-use and short-lived, and it is cleared in the same
 * statement that checks it, so two browsers racing the same link cannot both claim the machine.
 *
 * Behind the PIN gate: the middleware runs first, so an unauthenticated visitor is bounced to
 * /gate?next=/link?t=… and lands back here after entering the PIN.
 */
export async function GET(request: NextRequest) {
  const token = request.nextUrl.searchParams.get("t")?.trim();
  const home = new URL("/", request.nextUrl);

  if (!token) {
    home.searchParams.set("link", "invalid");
    return NextResponse.redirect(home);
  }

  const ownerKey = newSecret();
  const rows = (await sql`
    update workers set
      owner_key_hash = ${hashSecret(ownerKey)},
      link_token = null,
      link_expires_at = null
    where link_token = ${token} and link_expires_at > now()
    returning id, label
  `) as { id: string; label: string }[];

  if (rows.length === 0) {
    home.searchParams.set("link", "expired");
    return NextResponse.redirect(home);
  }

  home.searchParams.set("link", "ok");
  home.searchParams.set("machine", rows[0].label);

  const response = NextResponse.redirect(home);
  response.cookies.set(OWNER_COOKIE, ownerKey, {
    path: "/",
    maxAge: 60 * 60 * 24 * 365,
    httpOnly: true,
    secure: true,
    sameSite: "lax",
  });
  return response;
}
