"use client";

import { useEffect, useMemo, useState } from "react";

import { LIMITS, SORT_KEYS } from "@/lib/options";
import { miles, money, type Taxonomy } from "@/lib/types";

import { StartLights } from "./StartLights";

/**
 * Queue a search.
 *
 * Make and model come from Carvana's own inventory taxonomy rather than a free-text box, because
 * make/model are the only filters applied server-side via the URL path — a typo there yields an
 * empty run rather than a validation error.
 */
export function SearchForm({ onQueued }: { onQueued: (id: string) => void }) {
  const [taxonomy, setTaxonomy] = useState<Taxonomy | null>(null);
  const [make, setMake] = useState("");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [launching, setLaunching] = useState(false);
  const [problem, setProblem] = useState<{ error: string; hint?: string } | null>(null);

  useEffect(() => {
    fetch("/carvana-taxonomy.json")
      .then((response) => (response.ok ? response.json() : null))
      .then(setTaxonomy)
      .catch(() => setTaxonomy(null));
  }, []);

  const models = useMemo(
    () => taxonomy?.makes.find((entry) => entry.name === make)?.models ?? [],
    [taxonomy, make],
  );
  const bounds = taxonomy?.bounds;

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setProblem(null);

    const form = new FormData(event.currentTarget);
    const value = (name: string) => {
      const raw = form.get(name);
      return typeof raw === "string" && raw.trim() ? raw.trim() : undefined;
    };

    const options = {
      make: value("make"),
      model: value("model"),
      year_min: value("year_min"),
      year_max: value("year_max"),
      max_price: value("max_price"),
      max_miles: value("max_miles"),
      zip_code: value("zip_code"),
      top_n: value("top_n"),
      max_reports: value("max_reports"),
      max_pages: value("max_pages"),
      sort: value("sort"),
      search_only: form.get("search_only") === "on",
    };

    try {
      const response = await fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ options }),
      });
      const payload = await response.json();
      if (!response.ok) {
        setProblem(payload);
        return;
      }
      // Let the start sequence play before navigating — it is the acknowledgement that the run
      // was accepted, and the run page opens on a queued state anyway.
      setLaunching(true);
      setTimeout(() => onQueued(payload.id), 2200);
    } catch {
      setProblem({ error: "Could not reach the server. Check your connection." });
    } finally {
      setBusy(false);
    }
  }

  if (launching) {
    return (
      <div className="card" style={{ textAlign: "center", padding: "48px 22px" }}>
        <StartLights />
        <h2 style={{ marginTop: 24 }}>Lights out</h2>
        <p className="muted small">Queued. The machine running the scraper takes it from here.</p>
      </div>
    );
  }

  return (
    <form className="card" onSubmit={submit}>
      <div className="card-title">New search</div>

      <div className="grid-2">
        <div>
          <label htmlFor="make">Make</label>
          <select
            id="make"
            name="make"
            value={make}
            onChange={(event) => {
              setMake(event.target.value);
              setModel("");
            }}
          >
            <option value="">Any make</option>
            {/* Keyed by name: makes have no slug in the taxonomy, only models do. */}
            {taxonomy?.makes.map((entry) => (
              <option key={entry.name} value={entry.name}>
                {entry.name} ({entry.count.toLocaleString("en-US")})
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="model">Model</label>
          <select
            id="model"
            name="model"
            value={model}
            onChange={(event) => setModel(event.target.value)}
            disabled={!make}
          >
            <option value="">{make ? "Any model" : "Pick a make first"}</option>
            {models.map((entry) => (
              <option key={entry.slug} value={entry.name}>
                {entry.name} ({entry.count.toLocaleString("en-US")})
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="year_min">Year from</label>
          <input id="year_min" name="year_min" type="number" min={1990} max={2030} placeholder="2018" />
        </div>
        <div>
          <label htmlFor="year_max">Year to</label>
          <input id="year_max" name="year_max" type="number" min={1990} max={2030} placeholder="2023" />
          {bounds && <p className="hint">Inventory spans {bounds.year[0]}–{bounds.year[1]}.</p>}
        </div>

        <div>
          <label htmlFor="max_price">Max landed price</label>
          <input id="max_price" name="max_price" type="number" min={0} step={500} placeholder="45000" />
          {bounds && (
            <p className="hint">
              Listings run {money(bounds.price[0])}–{money(bounds.price[1])}.
            </p>
          )}
        </div>
        <div>
          <label htmlFor="max_miles">Max mileage</label>
          <input id="max_miles" name="max_miles" type="number" min={0} step={1000} placeholder="80000" />
          {bounds && (
            <p className="hint">
              Odometers run {miles(bounds.mileage[0])}–{miles(bounds.mileage[1])}.
            </p>
          )}
        </div>
      </div>

      <p className="hint warn" style={{ marginTop: 12 }}>
        Set <strong>both</strong> price and mileage. They are the scoring anchors — without them
        scores are normalised against this run&rsquo;s own results and cannot be compared with any
        other run.
      </p>

      <div className="grid-3" style={{ marginTop: 20 }}>
        <div>
          <label htmlFor="top_n">Carfax shortlist</label>
          <input id="top_n" name="top_n" type="number" min={1} max={LIMITS.top_n} defaultValue={12} />
          <p className="hint">Max {LIMITS.top_n}. Carfax is the rate-limited one.</p>
        </div>
        <div>
          <label htmlFor="max_reports">Max reports</label>
          <input
            id="max_reports"
            name="max_reports"
            type="number"
            min={1}
            max={LIMITS.max_reports}
            defaultValue={40}
          />
          <p className="hint">A car beyond this gets no history at all.</p>
        </div>
        <div>
          <label htmlFor="max_pages">Search pages</label>
          <input
            id="max_pages"
            name="max_pages"
            type="number"
            min={1}
            max={LIMITS.max_pages}
            defaultValue={8}
          />
          <p className="hint">~21 vehicles each. Max {LIMITS.max_pages}.</p>
        </div>
      </div>

      <div className="grid-2" style={{ marginTop: 20 }}>
        <div>
          <label htmlFor="zip_code">Delivery ZIP</label>
          <input id="zip_code" name="zip_code" inputMode="numeric" maxLength={10} placeholder="from your profile" />
          <p className="hint">
            Blank uses the delivery location captured on the machine running the scraper —
            which is the only thing that actually moves Carvana&rsquo;s pricing zip.
          </p>
        </div>
        <div>
          <label htmlFor="sort">Sort by</label>
          <select id="sort" name="sort" defaultValue="score">
            {SORT_KEYS.map((key) => (
              <option key={key} value={key}>
                {key}
              </option>
            ))}
          </select>
          <label className="inline" style={{ marginTop: 14, textTransform: "none" }}>
            <input type="checkbox" name="search_only" style={{ width: "auto" }} />
            <span className="small muted">Search only — list inventory, fetch no reports</span>
          </label>
        </div>
      </div>

      {problem && (
        <div className="alert bad" style={{ marginTop: 18 }}>
          <h3>{problem.error}</h3>
          {problem.hint && <p>{problem.hint}</p>}
        </div>
      )}

      <div style={{ marginTop: 22 }}>
        <button className="btn" type="submit" disabled={busy}>
          {busy ? "Queuing…" : "Form up the grid"}
        </button>
      </div>
    </form>
  );
}
