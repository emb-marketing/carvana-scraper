"use client";

import { useCallback, useEffect, useState } from "react";

import { PairPanel } from "@/components/PairPanel";
import { SearchForm } from "@/components/SearchForm";
import { ago, type RunRow } from "@/lib/types";

/**
 * Home: pair, submit a search, and see recent runs.
 *
 * The owner key lives in localStorage. It identifies which machine a submitted job is addressed
 * to, not who the visitor is — the site password is the only access control, and every finished
 * run is visible to everyone past it.
 */
const OWNER_KEY_STORAGE = "grid.owner_key";
const LABEL_STORAGE = "grid.worker_label";

export default function HomePage() {
  const [ownerKey, setOwnerKey] = useState<string | null>(null);
  const [label, setLabel] = useState<string>("");
  const [ready, setReady] = useState(false);
  const [runs, setRuns] = useState<RunRow[]>([]);

  useEffect(() => {
    setOwnerKey(localStorage.getItem(OWNER_KEY_STORAGE));
    setLabel(localStorage.getItem(LABEL_STORAGE) ?? "");
    setReady(true);
  }, []);

  const loadRuns = useCallback(async () => {
    try {
      const response = await fetch("/api/runs");
      if (response.ok) setRuns((await response.json()).runs ?? []);
    } catch {
      // A failed poll is not worth surfacing; the next tick retries.
    }
  }, []);

  useEffect(() => {
    loadRuns();
    const timer = setInterval(loadRuns, 5000);
    return () => clearInterval(timer);
  }, [loadRuns]);

  function handlePaired(key: string, workerLabel: string) {
    localStorage.setItem(OWNER_KEY_STORAGE, key);
    localStorage.setItem(LABEL_STORAGE, workerLabel);
    setOwnerKey(key);
    setLabel(workerLabel);
  }

  function unpair() {
    localStorage.removeItem(OWNER_KEY_STORAGE);
    localStorage.removeItem(LABEL_STORAGE);
    setOwnerKey(null);
    setLabel("");
  }

  const mine = runs.find((run) => run.worker_label === label);
  const online = mine?.worker_online ?? false;

  if (!ready) return null;

  return (
    <main>
      {ownerKey && (
        <div className="spread" style={{ marginBottom: 18 }}>
          <span className="pill">
            <span className={`dot ${online ? "live" : "bad"}`} />
            {label || "your machine"} · {online ? "worker online" : "worker offline"}
          </span>
          <button className="btn ghost" onClick={unpair} type="button">
            Unpair
          </button>
        </div>
      )}

      {!online && ownerKey && (
        <div className="alert">
          <h3>Your worker is not running</h3>
          <p>
            Nothing will happen to a queued search until it is. Start it on your laptop with{" "}
            <code className="mono">python3 -m carvana_scraper.worker</code>.
          </p>
        </div>
      )}

      {ownerKey ? (
        <SearchForm ownerKey={ownerKey} onQueued={(id) => { window.location.href = `/runs/${id}`; }} />
      ) : (
        <PairPanel onPaired={handlePaired} />
      )}

      <div className="section-head">
        <h2>Recent runs</h2>
        <span className="count">{runs.length}</span>
      </div>

      <div className="card">
        {runs.length === 0 ? (
          <div className="empty">Nothing has run yet.</div>
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
