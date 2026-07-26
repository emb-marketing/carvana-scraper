"use client";

import { use, useCallback, useEffect, useState } from "react";

import { GridSlot } from "@/components/GridSlot";
import { miles, money, type ReportRef, type RunRow, type Snapshot } from "@/lib/types";

const POLL_MS = 2000;
const STAGE_COUNT = 4;

/**
 * One run, live then final.
 *
 * `progress` and `result` are both `AppState.snapshot()` payloads and the worker writes the final
 * snapshot into both, so this reads a single field and never branches on status to find the data.
 */
export default function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [run, setRun] = useState<RunRow | null>(null);
  const [reports, setReports] = useState<ReportRef[]>([]);
  const [missing, setMissing] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch(`/api/runs/${id}`);
      if (response.status === 404) {
        setMissing(true);
        return true;
      }
      const payload = await response.json();
      setRun(payload.run);
      setReports(payload.reports ?? []);
      return payload.run.status === "done" || payload.run.status === "failed";
    } catch {
      return false;
    }
  }, [id]);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;

    const tick = async () => {
      const finished = await load();
      if (!stopped && !finished) timer = setTimeout(tick, POLL_MS);
    };
    tick();

    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [load]);

  if (missing) return <main className="card"><div className="empty">No such run.</div></main>;
  if (!run) return <main className="card"><div className="empty">Loading…</div></main>;

  const snapshot: Snapshot | null = run.result ?? run.progress ?? null;
  const live = run.status === "queued" || run.status === "running";

  return (
    <main>
      <div className="spread" style={{ marginBottom: 18 }}>
        <div>
          <h1 style={{ fontSize: 22 }}>{run.criteria || "search"}</h1>
          <p className="faint small" style={{ margin: "4px 0 0" }}>
            {run.worker_label}
            {run.status === "queued" && !run.worker_online && " · worker offline"}
          </p>
        </div>
        <span className={`badge ${run.status}`}>{run.status}</span>
      </div>

      {run.status === "queued" && (
        <div className="card">
          <div className="card-title">On the grid</div>
          <p className="muted small" style={{ margin: 0 }}>
            {run.worker_online
              ? "Waiting for your worker to pick this up."
              : "Your worker is offline. Start it and this begins automatically."}
          </p>
        </div>
      )}

      {live && run.status === "running" && snapshot && <LiveProgress snapshot={snapshot} />}

      {snapshot?.challenge && (
        <div className="alert">
          <h3>Puzzle waiting — check your Chrome window</h3>
          <p>
            Carfax is showing a DataDome challenge for {snapshot.challenge.label}. Solve it in the
            Chrome window your worker opened; the run continues on its own.
            {snapshot.challenge.timeout_s
              ? ` It gives up after ${snapshot.challenge.timeout_s}s and defers the car.`
              : ""}
          </p>
        </div>
      )}

      {run.error && (
        <div className="alert bad">
          <h3>{run.error.kind}: {run.error.message}</h3>
          {run.error.hint && <p>{run.error.hint}</p>}
        </div>
      )}

      {snapshot?.warnings?.map((warning) => (
        <div className="alert" key={warning}>
          <p>{warning}</p>
        </div>
      ))}

      {snapshot && <Results runId={id} snapshot={snapshot} reports={reports} />}
    </main>
  );
}

function LiveProgress({ snapshot }: { snapshot: Snapshot }) {
  const current = snapshot.stage?.n ?? 0;
  const recent = snapshot.events.filter((event) => event.text).slice(-40);

  return (
    <div className="card">
      <div className="card-title">
        Stage {current || "—"} of {STAGE_COUNT}
        {snapshot.stage?.name ? ` · ${snapshot.stage.name}` : ""}
      </div>

      <div className="stages">
        {[1, 2, 3, 4].map((n) => (
          <div key={n} className={`stage ${n < current ? "done" : n === current ? "active" : ""}`} />
        ))}
      </div>

      {snapshot.progress?.of ? (
        <p className="small muted" style={{ marginTop: 0 }}>
          {snapshot.progress.i} of {snapshot.progress.of} · {snapshot.progress.label}
        </p>
      ) : null}

      <div className="feed">
        {recent.map((event) => (
          <div key={event.seq}>{event.text}</div>
        ))}
      </div>
    </div>
  );
}

function Results({
  runId,
  snapshot,
  reports,
}: {
  runId: string;
  snapshot: Snapshot;
  reports: ReportRef[];
}) {
  const { ranked, needs_carfax: needsCarfax, disqualified, manifest, review } = snapshot;
  const nothing = ranked.length === 0 && needsCarfax.length === 0 && disqualified.length === 0;

  return (
    <>
      {manifest && (
        <>
          <div className="section-head">
            <h2>Run manifest</h2>
            {!manifest.reconciled && <span className="count">counts not checked for this mode</span>}
          </div>
          <div className="card">
            {manifest.reconciliation_problems.length > 0 && (
              <div className="alert bad">
                <h3>The counts do not reconcile</h3>
                <p>{manifest.reconciliation_problems.join(" · ")}</p>
              </div>
            )}
            <pre className="manifest">{manifest.lines.join("\n")}</pre>
          </div>
        </>
      )}

      {ranked.length > 0 && (
        <>
          <div className="section-head">
            <h2>The grid</h2>
            <span className="count">{ranked.length} ranked · both reports read</span>
          </div>
          {ranked.map((vehicle, index) => (
            <GridSlot
              key={vehicle.listing.vin}
              vehicle={vehicle}
              position={index + 1}
              runId={runId}
              reports={reports}
            />
          ))}
        </>
      )}

      {needsCarfax.length > 0 && (
        <>
          <div className="section-head">
            <h2>Not classified</h2>
            <span className="count">{needsCarfax.length} held out — unknown is never clean</span>
          </div>
          <div className="card">
            <div className="rows">
              {needsCarfax.map((entry) => (
                <div className="row" key={entry.vin}>
                  <span style={{ minWidth: 0 }}>
                    <span className="slot-name">{entry.label}</span>
                    <span className="slot-meta" style={{ display: "block" }}>
                      {money(entry.landed_price)} · {miles(entry.mileage)} · missing{" "}
                      {entry.missing_decision_fields.join(", ") || "history"}
                    </span>
                  </span>
                  <a className="btn ghost" href={entry.carfax_url} target="_blank" rel="noreferrer noopener">
                    Open Carfax
                  </a>
                </div>
              ))}
            </div>
            <p className="hint" style={{ marginTop: 14 }}>
              {"Blocked cars are never cached, so the next run retries them. "}
              {"To resolve one now, open its report and paste it into the local app."}
            </p>
          </div>
        </>
      )}

      {disqualified.length > 0 && (
        <>
          <div className="section-head">
            <h2>Out</h2>
            <span className="count">{disqualified.length} disqualified</span>
          </div>
          {disqualified.map((vehicle) => (
            <GridSlot
              key={vehicle.listing.vin}
              vehicle={vehicle}
              position={0}
              runId={runId}
              reports={reports}
            />
          ))}
        </>
      )}

      {review && <ReviewPanel review={review} />}

      {nothing && snapshot.status === "done" && (
        <div className="card">
          <div className="empty">Nothing matched those criteria.</div>
        </div>
      )}
    </>
  );
}

function ReviewPanel({ review }: { review: NonNullable<Snapshot["review"]> }) {
  if (review.error) {
    return (
      <>
        <div className="section-head"><h2>Report review</h2></div>
        <div className="card"><p className="muted small" style={{ margin: 0 }}>{review.error}</p></div>
      </>
    );
  }

  const findings = review.findings ?? [];

  return (
    <>
      <div className="section-head">
        <h2>Report review</h2>
        <span className="count">
          reads the prose the scorer discards · cannot change the ranking
        </span>
      </div>
      <div className="card">
        {review.pick_reasoning && (
          <p className="small" style={{ marginTop: 0 }}>{review.pick_reasoning}</p>
        )}
        <div className="rows">
          {findings.map((finding, index) => (
            <div className="row" key={`${finding.vin}-${index}`} style={{ alignItems: "flex-start" }}>
              <span style={{ minWidth: 0 }}>
                <span className="slot-name">{finding.claim}</span>
                {finding.evidence && (
                  <span className="slot-meta" style={{ display: "block", fontStyle: "italic" }}>
                    &ldquo;{finding.evidence}&rdquo;
                  </span>
                )}
              </span>
              <span className={`badge ${finding.evidence_supported ? "done" : "warn"}`}>
                {finding.evidence_supported ? "quote verified" : "not found in report"}
              </span>
            </div>
          ))}
        </div>
        {review.unsupported_findings ? (
          <p className="hint warn" style={{ marginTop: 14 }}>
            {review.unsupported_findings} quote(s) could not be located in the report text. A claim
            you cannot check is not evidence.
          </p>
        ) : null}
      </div>
    </>
  );
}
