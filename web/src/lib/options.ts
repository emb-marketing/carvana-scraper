/**
 * Validate and clamp a submitted search into the payload a worker will accept.
 *
 * The field names here mirror `RunOptions.__dataclass_fields__` in carvana_scraper/pipeline.py
 * exactly. That is a real contract, not a convention: the worker **rejects** a job carrying a key
 * it does not recognise rather than ignoring it, because silently dropping a criterion would run
 * a different search than the person asked for and still report success.
 */

export const SORT_KEYS = ["score", "price", "cpm", "mileage"] as const;
export type SortKey = (typeof SORT_KEYS)[number];

/**
 * Ceilings on what a single submission may ask for.
 *
 * These protect the submitter from their own runaway search — every report fetch is a real page
 * load through their own browser profile, and a 500-car run is hours of attended work — and keep
 * a single result payload bounded. They are not an abuse control: each person scrapes from their
 * own machine and IP.
 */
export const LIMITS = {
  max_pages: 8,
  max_reports: 40,
  top_n: 12,
  assist_timeout: 600,
  year_min: 1990,
  year_max: 2030,
} as const;

export type RunOptionsPayload = {
  make?: string;
  model?: string;
  year_min?: number;
  year_max?: number;
  max_price?: number;
  max_miles?: number;
  zip_code?: string;
  top_n?: number;
  max_reports?: number;
  limit?: number;
  max_pages?: number;
  sort?: SortKey;
  search_only?: boolean;
  no_history?: boolean;
  no_carfax?: boolean;
  no_imperfections?: boolean;
  unattended?: boolean;
  assist_timeout?: number;
};

export class OptionsError extends Error {}

function optionalString(value: unknown, field: string, maxLength = 60): string | undefined {
  if (value === undefined || value === null || value === "") return undefined;
  if (typeof value !== "string") throw new OptionsError(`${field} must be text`);
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  if (trimmed.length > maxLength) throw new OptionsError(`${field} is too long`);
  return trimmed;
}

function optionalNumber(value: unknown, field: string): number | undefined {
  if (value === undefined || value === null || value === "") return undefined;
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed)) throw new OptionsError(`${field} must be a number`);
  if (parsed < 0) throw new OptionsError(`${field} cannot be negative`);
  return parsed;
}

function clampedInt(
  value: unknown,
  field: string,
  ceiling: number,
  fallback?: number,
): number | undefined {
  const parsed = optionalNumber(value, field);
  if (parsed === undefined) return fallback;
  return Math.min(Math.trunc(parsed), ceiling);
}

/**
 * Build the payload to queue, or throw OptionsError with a message safe to show the submitter.
 *
 * Unknown keys are dropped here rather than rejected: this is the boundary where a browser's
 * arbitrary JSON becomes a known shape. The strict check lives one layer further in, on the
 * worker, where an unexpected key means the two schemas have drifted.
 */
export function buildOptions(input: Record<string, unknown>): RunOptionsPayload {
  const options: RunOptionsPayload = {
    make: optionalString(input.make, "Make"),
    model: optionalString(input.model, "Model"),
    year_min: clampedInt(input.year_min, "Minimum year", LIMITS.year_max),
    year_max: clampedInt(input.year_max, "Maximum year", LIMITS.year_max),
    max_price: optionalNumber(input.max_price, "Max price"),
    max_miles: clampedInt(input.max_miles, "Max mileage", 1_000_000),
    zip_code: optionalString(input.zip_code, "ZIP", 10),
    top_n: clampedInt(input.top_n, "Carfax shortlist", LIMITS.top_n, 12),
    max_reports: clampedInt(input.max_reports, "Max reports", LIMITS.max_reports, 40),
    limit: clampedInt(input.limit, "Limit", 500),
    max_pages: clampedInt(input.max_pages, "Max pages", LIMITS.max_pages, 8),
    sort: SORT_KEYS.includes(input.sort as SortKey) ? (input.sort as SortKey) : "score",
    search_only: Boolean(input.search_only),
    no_history: Boolean(input.no_history),
    no_carfax: Boolean(input.no_carfax),
    no_imperfections: Boolean(input.no_imperfections),
    unattended: Boolean(input.unattended),
    assist_timeout: clampedInt(input.assist_timeout, "Assist timeout", LIMITS.assist_timeout, 240),
  };

  if (
    options.year_min !== undefined &&
    options.year_max !== undefined &&
    options.year_min > options.year_max
  ) {
    throw new OptionsError("Minimum year cannot be after maximum year.");
  }

  // Mirrors RunOptions.has_criterion(), including its deliberate omission of zip_code: a zip alone
  // would make an otherwise empty search look valid and page through all of Carvana.
  const hasCriterion = [
    options.make,
    options.model,
    options.year_min,
    options.year_max,
    options.max_price,
    options.max_miles,
  ].some((value) => value !== undefined);

  if (!hasCriterion) {
    throw new OptionsError(
      "Give at least one of make, model, year range, max price or max mileage.",
    );
  }

  // Undefined keys are stripped so the queued JSON contains only what was actually asked for —
  // the worker's unknown-key check reads better against a minimal payload.
  return Object.fromEntries(
    Object.entries(options).filter(([, value]) => value !== undefined),
  ) as RunOptionsPayload;
}

/**
 * A human summary of a queued search, for the list before a worker picks it up.
 *
 * Mirrors `SearchCriteria.describe()`. Cosmetic only and short-lived: the moment the run starts,
 * the pipeline's own description arrives with the progress payload and replaces this.
 */
export function describeOptions(options: RunOptionsPayload): string {
  const parts: string[] = [];
  const makeModel = [options.make, options.model].filter(Boolean).join(" ");
  parts.push(makeModel || "any make/model");

  if (options.year_min || options.year_max) {
    parts.push(`${options.year_min ?? "…"}-${options.year_max ?? "…"}`);
  }
  if (options.max_price !== undefined) {
    parts.push(`≤$${options.max_price.toLocaleString("en-US")} landed`);
  }
  if (options.max_miles !== undefined) {
    parts.push(`≤${options.max_miles.toLocaleString("en-US")} mi`);
  }
  if (options.zip_code) parts.push(`zip ${options.zip_code}`);

  return parts.join(" · ");
}
