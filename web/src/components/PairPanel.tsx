"use client";

import { useState } from "react";

/**
 * Pair this browser with the machine that will do the scraping.
 *
 * The site password proves someone is allowed in; it cannot say which laptop a job belongs to.
 * The worker prints a short code, the operator types it here once, and this browser gets a
 * long-lived owner key. The worker token itself never touches the browser.
 */
export function PairPanel({ onPaired }: { onPaired: (key: string, label: string) => void }) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<{ error: string; hint?: string } | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      const response = await fetch("/api/pair", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      const payload = await response.json();
      if (!response.ok) {
        setProblem(payload);
        return;
      }
      onPaired(payload.owner_key, payload.label);
    } catch {
      setProblem({ error: "Could not reach the server. Check your connection." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <div className="card-title">Pair your machine</div>
      <p className="muted small" style={{ marginTop: 0 }}>
        GRID queues searches, but the scraping runs on <strong>your</strong> laptop — your Chrome
        profile, your IP, and you clear your own puzzles. Start the worker there and it prints a
        six-character code.
      </p>

      <form onSubmit={submit} className="inline" style={{ marginTop: 16 }}>
        <input
          value={code}
          onChange={(event) => setCode(event.target.value.toUpperCase())}
          placeholder="ABC123"
          maxLength={6}
          autoComplete="off"
          spellCheck={false}
          aria-label="Pairing code"
          style={{
            maxWidth: 200,
            fontFamily: "var(--font-mono)",
            fontSize: 22,
            letterSpacing: "0.3em",
            textAlign: "center",
          }}
        />
        <button className="btn" type="submit" disabled={busy || code.length < 6}>
          {busy ? "Pairing…" : "Pair"}
        </button>
      </form>

      {problem && (
        <div className="alert bad" style={{ marginTop: 16 }}>
          <h3>{problem.error}</h3>
          {problem.hint && <p>{problem.hint}</p>}
        </div>
      )}

      <p className="hint" style={{ marginTop: 18 }}>
        No worker yet? Clone the repo and run <code className="mono">./setup.command</code> — it
        installs what is needed, opens Chrome once so Carvana trusts the profile, and starts the
        worker. See <code className="mono">docs/SETUP.md</code>.
      </p>
    </div>
  );
}
