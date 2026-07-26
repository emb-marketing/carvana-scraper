"use client";

import { useState } from "react";

/**
 * How to run searches on your own machine: one download, one command.
 *
 * The source is public, but this site's URL is not. The archive is generated at build time with the
 * URL already inside it, so there is nothing to configure after unpacking and nothing to publish.
 */
const COMMAND = "cd ~/Downloads && tar xzf grid-worker.tar.gz && cd grid-worker && ./start.command";

export default function SetupPage() {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(COMMAND);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }

  return (
    <main style={{ maxWidth: 660 }}>
      <h1 style={{ fontSize: 26 }}>Run searches on your own machine</h1>
      <p className="muted small" style={{ marginTop: 8 }}>
        Optional. The site already works without this — your searches just run on whichever machine
        is online. Do this and they run on <strong>yours</strong>.
      </p>

      <div className="card" style={{ marginTop: 24 }}>
        <div className="card-title">1 · Download</div>
        <a className="btn" href="/grid-worker.tar.gz" download>
          Download grid-worker.tar.gz
        </a>
        <p className="hint" style={{ marginTop: 12 }}>
          macOS · needs Python 3.11+ and Google Chrome. The site URL is already inside it.
        </p>
      </div>

      <div className="card">
        <div className="card-title">2 · Run it</div>
        <p className="muted small" style={{ marginTop: 0 }}>
          Open Terminal and paste:
        </p>
        <pre
          style={{
            background: "var(--asphalt-900)",
            border: "1px solid var(--kerb-line)",
            borderRadius: "var(--radius-sm)",
            padding: "14px 16px",
            fontSize: 12.5,
            margin: 0,
            color: "var(--text)",
            // Wrapped, not scrolled: a command the reader cannot see all of is a command they
            // cannot trust enough to paste.
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {COMMAND}
        </pre>
        <button className="btn ghost" onClick={copy} type="button" style={{ marginTop: 12 }}>
          {copied ? "Copied" : "Copy command"}
        </button>
        <p className="hint" style={{ marginTop: 12 }}>
          Or just double-click <code className="mono">start.command</code> after unzipping. It
          installs what it needs, opens Chrome once so you can set your delivery ZIP, then runs.
        </p>
      </div>

      <div className="card">
        <div className="card-title">3 · Claim it</div>
        <p className="muted small" style={{ marginTop: 0, marginBottom: 0 }}>
          On first run it prints a link. Open that link once and this browser is bound to that
          machine — everything you submit from then on runs there. Leave the Terminal window open
          while you use the site.
        </p>
      </div>

      <div className="card">
        <div className="card-title">Why a download and not just the website</div>
        <p className="muted small" style={{ marginTop: 0, marginBottom: 0 }}>
          A web page cannot drive your browser or read another site&rsquo;s pages — the sandbox
          forbids it, and that restriction is what makes browsing safe. Searching Carvana needs a
          real Chrome window, a real profile, and a human to clear the occasional puzzle. So it runs
          as a small program on your machine rather than in a tab.
        </p>
      </div>

      <p style={{ marginTop: 24 }}>
        <a href="/">← Back</a>
      </p>
    </main>
  );
}
