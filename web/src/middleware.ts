import { NextRequest, NextResponse } from "next/server";

import { cookieIsValid, gate } from "@/lib/gate";

/**
 * Gate every page and browser-facing API route behind the site PIN.
 *
 * Worker routes are exempt: they carry their own bearer token and are called by machines that
 * have no browser session. That is not a hole — `workerFromRequest` refuses any token it does not
 * recognise, so those routes are authenticated, just not by the PIN.
 *
 * Fails **closed**. If SITE_PIN is unset the site is unreachable rather than open, because the
 * failure mode of the alternative is a public site nobody notices is public.
 */
export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|carvana-taxonomy.json).*)"],
};

const EXEMPT_PREFIXES = ["/api/worker/", "/gate", "/api/gate"];

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (EXEMPT_PREFIXES.some((prefix) => pathname.startsWith(prefix))) {
    return NextResponse.next();
  }

  const pin = process.env.SITE_PIN;
  const secret = process.env.SESSION_SECRET;
  if (!pin || !secret) {
    return new NextResponse(
      "GRID is not configured: SITE_PIN and SESSION_SECRET must be set.",
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }

  if (await cookieIsValid(request.cookies.get(gate.COOKIE_NAME)?.value, secret)) {
    return NextResponse.next();
  }

  // An unauthenticated API call gets JSON, not a redirect into an HTML page it cannot parse.
  if (pathname.startsWith("/api/")) {
    return NextResponse.json(
      { error: "This site is PIN protected.", hint: "Open the site and enter the PIN." },
      { status: 401, headers: { "Cache-Control": "no-store" } },
    );
  }

  const target = request.nextUrl.clone();
  target.pathname = "/gate";
  // Preserve where they were headed so a shared deep link survives the PIN prompt.
  target.search = pathname === "/" ? "" : `?next=${encodeURIComponent(pathname)}`;
  return NextResponse.redirect(target);
}
