# Running the machine that serves GRID

**Most people reading about GRID do not need this page.** To *use* the site you need two things:
the link and the PIN. Open it, enter the PIN, search. Nothing to install.

This page is for the one machine that does the actual work.

---

## Why one machine has to run something

The site queues searches. It cannot run them.

Scraping needs a real Chrome window with a persistent, aged profile, a human available to clear a
DataDome puzzle, several minutes per run, and a writable disk for the report cache. Vercel is
serverless — short-lived cloud functions with no display, no browser and no persistent disk. So one
laptop somewhere runs the scraper, and everyone else just uses the link.

Whichever machine that is becomes the engine for the whole site. Leave it running and GRID works
for everyone; close it and searches simply queue until it comes back.

---

## What that machine needs

| | |
|---|---|
| **macOS** | The launcher is a `.command` file. Linux and Windows work; run the Python directly. |
| **Python 3.11+** | `python3 --version`. macOS ships one; otherwise [python.org](https://www.python.org/downloads/). |
| **Google Chrome** | The real one, from [google.com/chrome](https://www.google.com/chrome/). Not Chromium, not Brave. |
| **The site URL** | e.g. `https://grid-xi-nine.vercel.app` |

---

## Setup

```bash
git clone git@github.com:emb-marketing/carvana-scraper.git
cd carvana-scraper
./setup.command          # or double-click it in Finder
```

It checks Python and Chrome, installs Playwright, asks for the site URL, opens Chrome once so you
can set your delivery ZIP, and then starts the worker. When you see

```
Running as 'your-machine'.
https://… can now run searches — anyone with the PIN, no install.
```

the site is live. Leave the window open.

### The Chrome step is not optional

Two things persist from it, and both matter:

- **Trust.** An aged profile with real cookies is most of why Carfax does not challenge every
  fetch immediately.
- **The delivery ZIP.** Carvana decides its pricing zip from a cookie triple it writes itself, and
  that cookie is session-scoped — absent at the start of every run, so Carvana re-geolocates from
  your IP. `--login` captures the location Carvana writes *after you use its own location picker*,
  and every later run replays it. Typing a zip into a box does nothing; using Carvana's picker
  sticks.

Shipping cost is part of landed price, which is part of the ranking. Every search the site runs
will be priced from **this** machine's location, so set it deliberately.

---

## Day to day

```bash
cd carvana-scraper
python3 -m carvana_scraper.worker
```

| Flag | Effect |
|---|---|
| `--once` | Run one job and exit. Handy for testing. |
| `--poll-interval N` | Seconds between queue checks (default 5). |

The site shows a green **ready** pill when a machine is polling, and a plain warning when none is.
That badge is the answer to almost every "I submitted and nothing happened."

---

## When a puzzle appears

Carfax sits behind DataDome. After roughly six report fetches in a session it shows a puzzle.

The site displays **"Puzzle waiting"**, but the puzzle is in *your* Chrome window — you are the one
who has to solve it. The run then continues by itself, and one solve typically covers the rest of
the shortlist.

**The tool never solves a puzzle, and never will.** If nobody solves it within the assist timeout,
that car is recorded as blocked — never cached — so the next run retries it. It appears under
*Not classified* with a link to open the report by hand.

This is the real cost of hosting the machine: searches other people submit will occasionally need
you at the keyboard.

---

## Troubleshooting

**"CARVANA_WEB_URL is not set"** — the `.env` did not load. Re-run `./setup.command`, or
`export CARVANA_WEB_URL=https://…` before starting the worker.

**"another Chrome is using the dedicated profile"** — the local app or a second worker is running.
Close it. The error text includes the exact `rm -f` command if a crash left a stale lock.

**Requests fail with HTML where JSON should be** — only relevant if the site is behind Vercel
Deployment Protection. It is not today (the plan does not offer it), but if that changes, set
`VERCEL_AUTOMATION_BYPASS_SECRET` in `.env`.

**Report review says it was skipped** — that feature shells out to a local `claude` CLI. Without
it you get no review. The ranking is unaffected; it is deterministic and involves no model.

---

## What this machine publishes

Every run uploads to the shared site: the ranking, the run manifest, and **the full text of the
Carfax and AutoCheck reports it fetched**. Everyone with the site PIN can read all of it.

Your Carvana account, cookies and browser profile never leave the machine.
