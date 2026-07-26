"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";

import { SearchForm } from "@/components/SearchForm";
import { ago, type RunRow } from "@/lib/types";

/**
 * Home: submit a search, and see recent runs.
 *
 * The PIN is the only thing needed to use the site. Where a search *runs* depends on whether this
 * browser has claimed a machine: if it has, searches go to that laptop; if not, they go to
 * whichever machine is online. Everyone past the PIN sees every run.
 */
type Pool = { online: boolean; workers: string[] };

export default function HomePage() {
  return (
    <Suspense fallback={null}>
      <Home />
    </Suspense>
  );
}

function Home() {
  const params = useSearchParams();
  const linkResult = params.get("link");
  const linkedMachine = params.get("machine");
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [pool, setPool] = useState<Pool>({ online: false, workers: [] });
  const [mine, setMine] = useState<{ label: string } | null>(null);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/runs");
      if (response.ok) {
        const payload = await response.json();
        setRuns(payload.runs ?? []);
        setPool(payload.pool ?? { online: false, workers: [] });
        setMine(payload.mine ?? null);
      }
    } catch {
      // A failed poll is not worth surfacing; the next tick retries.
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [load]);

  return (
    <main>
      {linkResult === "ok" && (
        <div className="alert" style={{ borderColor: "var(--signal)" }}>
          <h3>This browser now uses {linkedMachine}</h3>
          <p>Your searches will run there from now on, not on anyone else&rsquo;s machine.</p>
        </div>
      )}
      {(linkResult === "expired" || linkResult === "invalid") && (
        <div className="alert bad">
          <h3>That setup link is no longer valid</h3>
          <p>Restart the worker on your laptop — it prints a fresh one.</p>
        </div>
      )}

      <div className="spread" style={{ marginBottom: 18 }}>
        <span className="pill">
          <span className={`dot ${pool.online ? "live" : "bad"}`} />
          {mine
            ? `your machine: ${mine.label}`
            : pool.online
              ? `${pool.workers[0]}${pool.workers.length > 1 ? ` +${pool.workers.length - 1}` : ""} · ready`
              : "no machine running"}
        </span>
        <span className="faint small">
          {mine
            ? "Your searches run on your own laptop."
            : "Searches run on whichever machine is online."}
        </span>
      </div>

      {loaded && !pool.online && (
        <div className="alert">
          <h3>No machine is running the scraper right now</h3>
          <p>
            You can still submit — it will sit in the queue and start by itself the moment one
            comes online.
          </p>
        </div>
      )}

      {loaded && !mine && (
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="spread">
            <div style={{ minWidth: 0 }}>
              <div className="card-title" style={{ marginBottom: 6 }}>
                Run searches on your own machine
              </div>
              <p className="muted small" style={{ margin: 0 }}>
                Optional — one download and one command. Yours run on your laptop instead of
                whoever else is online.
              </p>
            </div>
            <a className="btn ghost" href="/setup">Set up</a>
          </div>
        </div>
      )}

      <SearchForm onQueued={(id) => { window.location.href = `/runs/${id}`; }} />

      <div className="section-head">
        <h2>Recent runs</h2>
        <span className="count">{runs.length}</span>
      </div>

      <div className="card">
        {runs.length === 0 ? (
          <div className="empty">Nothing has run yet. Start one above.</div>
        ) : (
          <div className="rows">
            {runs.map((run) => (
              <a className="row" key={run.id} href={`/runs/${run.id}`}>
                <span style={{ minWidth: 0 }}>
                  <span className="slot-name">{run.criteria || "search"}</span>
                  <span className="slot-meta" style={{ display: "block" }}>
                    {run.worker_label} · {ago(run.created_at)}
                  </span>
                </span>
                <span className={`badge ${run.status}`}>{run.status}</span>
              </a>
            ))}
          </div>
        )}
      </div>
    </main>
  );
}
