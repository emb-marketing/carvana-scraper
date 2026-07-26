# GRID — the shared site

A Next.js app that queues used-car searches and shows their results. **It does not scrape.** The
pipeline needs headful real Chrome with an aged profile, a human to clear DataDome puzzles, minutes
of wall clock, and a writable disk — none of which a serverless host has. So this holds the queue
and the results, and each person runs `python3 -m carvana_scraper.worker` on their own laptop.

Worker setup for an end user is [`../docs/SETUP.md`](../docs/SETUP.md).

## Stack

Next.js App Router · `pg` against Postgres · no CSS framework, no component library, no ORM. The
Python package's "Playwright only" rule does not extend here, but the spirit does.

`pg` rather than `@neondatabase/serverless` on Neon's own recommendation for Vercel: Fluid compute
keeps functions warm long enough to reuse a TCP connection. It also means the app runs against any
Postgres, including a local one, so the queries are testable without provisioning a cloud database.

## Local development

```bash
cd web
npm install
createdb grid_dev
psql -d grid_dev -f schema.sql
DATABASE_URL="postgresql://$USER@localhost/grid_dev" npm run dev
```

`predev` copies `../config/carvana-taxonomy.json` into `public/`, which is where the make/model
dropdowns come from. The mirror is gitignored — the committed file in `config/` is the source of
truth.

Point a worker at it:

```bash
cd ..
CARVANA_WEB_URL=http://127.0.0.1:3000 python3 -m carvana_scraper.worker
```

## Deploying

1. `vercel link` here, with **Root Directory = `web`**. Use the `emb-marketing` account.
2. Provision Postgres (Neon). Apply `schema.sql`. Set `DATABASE_URL` in Vercel to the **pooled**
   connection string — the hostname containing `-pooler`.
3. Deploy.
4. **Settings → Deployment Protection → enable password protection.** This is the only access
   control on the site.
5. **Same page → Protection Bypass for Automation → generate a secret.** Deployment Protection
   gates the API routes too, so without this every worker is served the password page instead of
   JSON. Distribute it to each person alongside the site password; their worker sends it as
   `x-vercel-protection-bypass`.

Verify the gate is actually on:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://<site>/api/runs          # 401 from Vercel
curl -s -H "x-vercel-protection-bypass: $SECRET" https://<site>/api/runs  # JSON
```

## How access works

Two secrets, because they answer different questions:

| Secret | Held by | Answers |
|---|---|---|
| Site password (Vercel) | every visitor | may this person see the site at all |
| Automation bypass (Vercel) | every worker | may this machine reach the API past the password |
| Worker token | one machine | **which** laptop is this, and whose jobs may it claim |

The password is shared, so it cannot identify a machine — a visitor past it could otherwise claim
other people's jobs or publish fabricated results. Hence the per-machine token, and hence pairing:
a worker prints a six-character code, the browser redeems it once for a long-lived owner key, and
submitted jobs are tagged to that worker. Tokens and owner keys are stored only as sha256.

**Anyone with the bypass secret reaches the API without the password.** It is a shared secret
distributed to every user; treat it as one.

## Data

Three tables (`schema.sql`): `workers`, `runs`, `run_reports`.

Report prose lives in `run_reports`, not in `runs.result`. A twelve-car run carries a few hundred
KB of Carfax and AutoCheck text, and the run view polls every two seconds — keeping it out of the
result blob is what makes that poll cheap. It loads per car, on demand.

`runs.progress` and `runs.result` are both `AppState.snapshot()` payloads, produced by
`carvana_scraper/app/serialize.py`. **That module is the contract**: the local app's page and this
one render the same JSON, so a field added there must be reflected in `src/lib/types.ts`.

## Visibility

Every finished run is visible to everyone past the site password, including full report text. That
is the only thing this buys over each person just running the local app. Making runs owner-private
would be a `visibility` column and a `where` clause on `GET /api/runs`.
