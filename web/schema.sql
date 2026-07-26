-- GRID — shared queue schema.
--
-- Apply once against the Neon database referenced by DATABASE_URL:
--   psql "$DATABASE_URL" -f web/schema.sql
--
-- Three tables. Report prose is deliberately kept out of `runs.result` so the two-second status
-- poll does not re-ship a few hundred KB of Carfax text on every tick.

create extension if not exists pgcrypto;

-- One row per machine running a worker. The token is that machine's identity; only its sha256 is
-- stored, so a database leak does not let anyone publish results as a worker.
create table if not exists workers (
  id                 uuid primary key default gen_random_uuid(),
  token_hash         text not null unique,
  label              text not null,
  created_at         timestamptz not null default now(),
  last_seen_at       timestamptz
);

create table if not exists runs (
  -- worker_id is NULL until a worker claims the job. Jobs are addressed to the pool, not to a
  -- machine: whichever worker is running takes the next one, so a visitor needs nothing installed.
  id           uuid primary key default gen_random_uuid(),
  worker_id    uuid references workers(id) on delete set null,
  created_at   timestamptz not null default now(),
  claimed_at   timestamptz,
  finished_at  timestamptz,
  status       text not null default 'queued'
               check (status in ('queued', 'running', 'done', 'failed')),
  options      jsonb not null,
  criteria     text,
  progress     jsonb,
  result       jsonb,
  error        jsonb
);

create index if not exists runs_queue_idx on runs (status, created_at);
create index if not exists runs_recent_idx on runs (created_at desc);

create table if not exists run_reports (
  run_id uuid not null references runs(id) on delete cascade,
  vin    text not null,
  vendor text not null check (vendor in ('carfax', 'autocheck')),
  body   text not null,
  primary key (run_id, vin, vendor)
);
