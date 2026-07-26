/**
 * The payload shape the worker publishes.
 *
 * This mirrors `AppState.snapshot()` in carvana_scraper/app/state.py, which is built by
 * carvana_scraper/app/serialize.py. That module is the single contract between the pipeline and
 * both front ends — the local app's page and this one render the same JSON, so a field added
 * there must be reflected here.
 *
 * Only the fields this UI actually reads are typed. The rest ride along untouched.
 */

export type Listing = {
  vin: string;
  label: string;
  year: number;
  mileage: number;
  price: number;
  landed_price: number;
  kbb_value: number | null;
  price_vs_kbb: number | null;
  miles_per_year: number | null;
  listing_url: string;
  carfax_url: string;
  autocheck_url: string;
};

export type ScoredVehicle = {
  listing: Listing;
  score: number | null;
  cost_per_remaining_mile: number | null;
  positives: string[];
  negatives: string[];
  disqualifiers: string[];
  is_disqualified: boolean;
  is_rankable: boolean;
  completeness_marker: string;
};

export type NeedsCarfaxEntry = {
  vin: string;
  label: string;
  carfax_url: string;
  listing_url: string;
  landed_price: number;
  mileage: number;
  remedy: "paste_or_retry" | "carfax_skipped" | "raise_top_n";
  missing_decision_fields: string[];
};

export type Manifest = {
  criteria: string;
  counters: Record<string, number>;
  warnings: string[];
  reconciliation_problems: string[];
  reconciled: boolean;
  lines: string[];
};

export type ReviewFinding = {
  vin: string;
  claim: string;
  evidence: string;
  severity: string;
  evidence_supported: boolean;
};

export type Review = {
  error?: string;
  findings?: ReviewFinding[];
  pick_vin?: string | null;
  pick_reasoning?: string;
  unsupported_findings?: number;
  model?: string;
};

export type Snapshot = {
  status: string;
  criteria: string | null;
  stage: { n?: number; of?: number; name?: string; total?: number };
  progress: { i?: number; of?: number; label?: string };
  events: { seq: number; kind: string; text?: string }[];
  warnings: string[];
  challenge: { label?: string; timeout_s?: number } | null;
  error: { kind: string; message: string; hint: string } | null;
  review: Review | null;
  manifest: Manifest | null;
  ranked: ScoredVehicle[];
  needs_carfax: NeedsCarfaxEntry[];
  disqualified: ScoredVehicle[];
  report_body?: string;
  exit_code: number | null;
};

export type RunStatus = "queued" | "running" | "done" | "failed";

export type RunRow = {
  id: string;
  status: RunStatus;
  criteria: string | null;
  created_at: string;
  claimed_at?: string | null;
  finished_at: string | null;
  worker_label: string;
  worker_online: boolean;
  progress?: Snapshot | null;
  result?: Snapshot | null;
  error?: { kind: string; message: string; hint: string } | null;
};

export type ReportRef = { vin: string; vendor: "carfax" | "autocheck" };

/**
 * Taxonomy as tools/extract_taxonomy.py actually writes it.
 *
 * Note that **makes carry no slug** — only models do. Keying a make list by `slug` therefore
 * produces an undefined key for every entry.
 */
export type Taxonomy = {
  extracted_at: string;
  bounds: { year: [number, number]; price: [number, number]; mileage: [number, number] };
  makes: {
    name: string;
    count: number;
    models: { name: string; count: number; slug: string; trims: string[] }[];
  }[];
};

/** Sign goes outside the currency symbol: "-$610", never "$-610". */
export const money = (value: number | null | undefined): string => {
  if (value === null || value === undefined) return "—";
  const rounded = Math.round(value);
  return `${rounded < 0 ? "-" : ""}$${Math.abs(rounded).toLocaleString("en-US")}`;
};

export const miles = (value: number | null | undefined): string =>
  value === null || value === undefined ? "—" : `${Math.round(value).toLocaleString("en-US")} mi`;

export function ago(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
