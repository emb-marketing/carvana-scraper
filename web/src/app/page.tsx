"use client";

import { useCallback, useEffect, useState } from "react";

import { SearchForm } from "@/components/SearchForm";
import { ago, type RunRow } from "@/lib/types";

/**
 * Home: submit a search, and see recent runs.
 *
 * There is nothing to install and nothing to pair. The PIN is the only gate, and a submitted
 * search goes to the queue for whichever machine is currently running a worker. Everyone past the
 * PIN sees every run.
 */
type Pool = { online: boolean; workers: string[] };

export default function HomePage() {
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [pool, setPool] = useState<Pool>({ online: false, workers: [] });
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/runs");
      if (response.ok) {
        const payload = await response.json();
        setRuns(payload.runs ?? []);
        setPool(payload.pool ?? { online: false, workers: [] });
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
      <div className="spread" style={{ marginBottom: 18 }}>
        <span className="pill">
          <span className={`dot ${pool.online ? "live" : "bad"}`} />
          {pool.online
            ? `${pool.workers[0]}${pool.workers.length > 1 ? ` +${pool.workers.length - 1}` : ""} · ready`
            : "no machine running"}
        </span>
        <span className="faint small">
          Searches run on whichever machine is running the scraper.
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
