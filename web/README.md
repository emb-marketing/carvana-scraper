# GRID — the shared site

A Next.js app that queues used-car searches and shows their results. **It does not scrape.**
Scraping needs headful real Chrome with an aged profile, a human to clear DataDome puzzles, minutes
of wall clock, and a writable disk — none of which a serverless host has. So this holds the queue
and the results, and one machine somewhere runs `python3 -m carvana_scraper.worker`.

**Visitors install nothing.** Link + PIN + search. A submitted job goes to the queue with no worker
attached; whichever machine is running claims it. That is the whole design — do not reintroduce
per-visitor pairing.

Running the scraping machine: [`../docs/SETUP.md`](../docs/SETUP.md).

## Stack

Next.js App Router · `pg` against Postgres · no CSS framework, no component library, no ORM. The
Python package's "Playwright only" rule does not extend here, but the spirit does.

`pg` rather than `@neondatabase/serverless` on Neon's own recommendation for Vercel: Fluid compute
keeps functions warm long enough to reuse a TCP connection. It also means the app runs against any
Postgres, including a local one, so the queries are testable without a cloud database.

## Local development

```bash
cd web
npm install
createdb grid_dev
psql -d grid_dev -f schema.sql
DATABASE_URL="postgresql://$USER@localhost/grid_dev" SITE_PIN=dev SESSION_SECRET=dev npm run dev
```

`predev` copies `../config/carvana-taxonomy.json` into `public/` for the make/model dropdowns. The
mirror is gitignored — the committed file in `config/` is the source of truth.

Point a worker at it:

```bash
cd ..
CARVANA_WEB_URL=http://127.0.0.1:3000 python3 -m carvana_scraper.worker
```

## Access: two secrets, two questions

| Secret | Held by | Answers | Enforced by |
|---|---|---|---|
| `SITE_PIN` | every visitor | may this **person** in | `src/middleware.ts` |
| worker token | one machine | which **machine** is this | `Authorization: Bearer` on `/api/worker/*` |

They are not interchangeable. Without the worker token, anyone past the PIN could publish
fabricated results; without the PIN, the site is public. Worker routes are deliberately PIN-exempt
— they are called by machines with no browser session, and they authenticate themselves.

The middleware **fails closed**: with `SITE_PIN` or `SESSION_SECRET` unset it returns 503 rather
than serving, because the alternative failure mode is a public site nobody notices is public.

The session cookie carries no secret — it is an HMAC over a marker plus an expiry, so it cannot be
forged or self-extended. `src/lib/gate.ts` uses Web Crypto because middleware runs on the edge
runtime, where `node:crypto` does not exist.

### Why not Vercel's own Deployment Protection

It was the first choice and it is unavailable on this plan. Measured against the API:

- `passwordProtection` with `deploymentType: all` **and** `preview` → *"Advanced Deployment
  Protection is not enabled on your team"*
- `ssoProtection` → *"Vercel Authentication is not available on your plan for production
  deployments"*

If that add-on is ever purchased, `VERCEL_AUTOMATION_BYPASS_SECRET` becomes necessary for workers,
since Deployment Protection gates API routes too. The worker already sends the
`x-vercel-protection-bypass` header when that variable is set, so nothing needs changing.

## Deploying

1. `vercel link` from the **repo root**, Root Directory `web`. Building from the root is what makes
   `../config/carvana-taxonomy.json` reachable at build time.
2. Provision Postgres, apply `schema.sql`, set `DATABASE_URL` to the **pooled** connection string
   (hostname containing `-pooler`).
3. Set `SITE_PIN` and `SESSION_SECRET`.
4. `vercel deploy --prod`.

Verify the gate:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://<site>/          # 307 -> /gate
curl -s https://<site>/api/runs                                   # 401 JSON
```

## Data

Three tables (`schema.sql`): `workers`, `runs`, `run_reports`.

`runs.worker_id` is **nullable** — a job has no machine until one claims it. The claim is a single
`UPDATE … FOR UPDATE SKIP LOCKED`, which is what makes several workers safe.

Report prose lives in `run_reports`, not in `runs.result`. A twelve-car run carries a few hundred
KB of Carfax and AutoCheck text and the run view polls every two seconds — keeping it out of the
result blob is what makes that poll cheap. It loads per car, on demand.

`runs.progress` and `runs.result` are both `AppState.snapshot()` payloads from
`carvana_scraper/app/serialize.py`. **That module is the contract**: the local app's page and this
one render the same JSON, so a field added there must be reflected in `src/lib/types.ts`.

## Visibility

Every run is visible to everyone past the PIN, including full report text. That is the only thing
this buys over each person running the local app. Making runs private would be a `visibility`
column and a `where` clause on `GET /api/runs`.
