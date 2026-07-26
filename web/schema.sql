-- GRID — shared queue schema.
--
-- Apply once against the Neon database referenced by DATABASE_URL:
--   psql "$DATABASE_URL" -f web/schema.sql
--
-- Three tables. Report prose is deliberately kept out of `runs.result` so the two-second status
-- poll does not re-ship a few hundred KB of Carfax text on every tick.

create extension if not exists pgcrypto;

-- One row per paired machine. The token is this machine's identity; only its sha256 is stored, so
-- a database leak does not let anyone claim someone else's jobs.
create table if not exists workers (
  id                 uuid primary key default gen_random_uuid(),
  token_hash         text not null unique,
  label              text not null,
  owner_key_hash     text,
  pairing_code       text,
  pairing_expires_at timestamptz,
  created_at         timestamptz not null default now(),
  last_seen_at       timestamptz
);

-- Pairing codes are short and therefore guessable at volume; they are looked up directly, so an
-- index keeps that cheap, and they are cleared the moment they are redeemed.
create index if not exists workers_pairing_code_idx
  on workers (pairing_code) where pairing_code is not null;

create table if not exists runs (
  id           uuid primary key default gen_random_uuid(),
  worker_id    uuid not null references workers(id) on delete cascade,
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

create index if not exists runs_queue_idx on runs (worker_id, status, created_at);
create index if not exists runs_recent_idx on runs (created_at desc);

create table if not exists run_reports (
  run_id uuid not null references runs(id) on delete cascade,
  vin    text not null,
  vendor text not null check (vendor in ('carfax', 'autocheck')),
  body   text not null,
  primary key (run_id, vin, vendor)
);
