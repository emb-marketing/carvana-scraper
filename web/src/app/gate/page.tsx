"use client";

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";

/**
 * Reduce `?next=` to a same-origin path, or fall back to the root.
 *
 * The middleware only ever writes a pathname here, but the query string is attacker-controllable
 * in a hand-crafted link. Unchecked, `window.location.href = next` accepts an absolute URL — so
 * "open GRID" could land a PIN-holder on a lookalike right after they authenticate — and it
 * accepts `javascript:`, which is script execution rather than merely a redirect.
 *
 * Rejected: absolute URLs, protocol-relative `//host`, and the `/\host` form some browsers
 * normalise into one. Anything not rooted at a single slash goes to `/`.
 */
function safeNext(raw: string | null): string {
  if (!raw || !raw.startsWith("/")) return "/";
  if (raw.startsWith("//") || raw.startsWith("/\\")) return "/";
  return raw;
}

/** The PIN prompt. The only thing an unauthenticated visitor can reach. */
export default function GatePage() {
  return (
    <Suspense fallback={null}>
      <GateForm />
    </Suspense>
  );
}

function GateForm() {
  const next = safeNext(useSearchParams().get("next"));
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setProblem("");
    try {
      const response = await fetch("/api/gate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pin }),
      });
      if (!response.ok) {
        setProblem((await response.json()).error ?? "That PIN is not right.");
        return;
      }
      // Full navigation, not a router push: the cookie has to be attached by the browser on the
      // next request for middleware to see it.
      window.location.href = next;
    } catch {
      setProblem("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={{ maxWidth: 380, margin: "14vh auto 0", textAlign: "center" }}>
      <div className="lights go" style={{ justifyContent: "center", marginBottom: 26 }}>
        {[0, 1, 2, 3, 4].map((index) => (
          <span className="lamp" key={index} />
        ))}
      </div>

      <h1 style={{ fontSize: 30, letterSpacing: "-0.04em" }}>Enter the PIN</h1>
      <p className="muted small" style={{ marginTop: 6 }}>
        GRID ranks live Carvana inventory by price, mileage and vehicle history.
      </p>

      <form onSubmit={submit} className="card" style={{ marginTop: 26 }}>
        <input
          value={pin}
          onChange={(event) => setPin(event.target.value)}
          type="password"
          autoFocus
          autoComplete="current-password"
          aria-label="Site PIN"
          placeholder="••••••••"
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 20,
            letterSpacing: "0.16em",
            textAlign: "center",
          }}
        />
        <button className="btn" type="submit" disabled={busy || !pin} style={{ width: "100%", marginTop: 14 }}>
          {busy ? "Checking…" : "Go"}
        </button>
        {problem && (
          <p className="hint" style={{ color: "var(--flag-red)", marginTop: 12 }}>
            {problem}
          </p>
        )}
      </form>
    </main>
  );
}
