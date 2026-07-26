/**
 * Postgres access.
 *
 * `pg` over TCP rather than `@neondatabase/serverless`: Neon's own connection-method guide
 * recommends node-postgres for Vercel, because Fluid compute keeps functions warm long enough to
 * reuse the connection and skip the setup cost. It also means this runs against any Postgres —
 * including a local one — so the queries here are testable without provisioning a cloud database.
 *
 * Use Neon's **pooled** connection string (the hostname with `-pooler` in it). That routes through
 * PgBouncer, which is what keeps bursty serverless concurrency from exhausting Postgres
 * connections.
 */
import { Pool } from "pg";

const connectionString = process.env.DATABASE_URL;

if (!connectionString) {
  throw new Error(
    "DATABASE_URL is not set. Apply web/schema.sql to a Postgres database and set the variable.",
  );
}

// Module scope so warm invocations reuse it. `max` is deliberately small: many function instances
// each holding a few connections is how a pool exhausts a database.
const pool = new Pool({
  connectionString,
  max: 3,
  idleTimeoutMillis: 10_000,
  connectionTimeoutMillis: 10_000,
  // Local development has no TLS; hosted Postgres does. Inferring from the host keeps one
  // connection string working in both places.
  ssl: /localhost|127\.0\.0\.1/.test(connectionString) ? false : { rejectUnauthorized: true },
});

/**
 * Tagged-template query returning rows.
 *
 * Every `${value}` becomes a numbered placeholder and travels as a bound parameter, so callers
 * cannot accidentally build an injectable string — interpolating a value is the *safe* path here,
 * and there is deliberately no escape hatch for raw SQL fragments.
 */
export async function sql<T = Record<string, unknown>>(
  strings: TemplateStringsArray,
  ...values: unknown[]
): Promise<T[]> {
  const text = strings.reduce(
    (accumulated, part, index) =>
      accumulated + part + (index < values.length ? `$${index + 1}` : ""),
    "",
  );
  const result = await pool.query(text, values);
  return result.rows as T[];
}
