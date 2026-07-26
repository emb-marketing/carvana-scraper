# Setting up a GRID worker

GRID is a website that queues used-car searches, but **the searching happens on your own laptop**.
This page gets you from nothing to a working worker.

Be warned up front: this is a developer-ish install. You need Python, a terminal, real Google
Chrome, and a window left running. There is no way around that — see [Why your laptop](#why-your-laptop).

---

## What you need

| | |
|---|---|
| **macOS** | The launcher is a `.command` file. Linux and Windows work too, just run the Python directly. |
| **Python 3.11+** | `python3 --version`. macOS ships one; otherwise [python.org](https://www.python.org/downloads/). |
| **Google Chrome** | The real one, from [google.com/chrome](https://www.google.com/chrome/). Not Chromium, not Brave. |
| **The site URL and its password** | From whoever runs GRID. |
| **The automation bypass secret** | Also from them. Without it your worker cannot reach the API. |

---

## Setup

```bash
git clone git@github.com:emb-marketing/carvana-scraper.git
cd carvana-scraper
./setup.command          # or double-click it in Finder
```

It will:

1. Check Python and Chrome.
2. `pip install -r requirements.txt` — Playwright, and nothing else.
3. Ask for the site URL and bypass secret, saving them to a gitignored `.env`.
4. Open Chrome once so you can set your delivery ZIP (see below).
5. Start the worker and print a **pairing code**.

Then open the site, enter the code, and you are done. The code lasts 15 minutes; restart the
worker for a fresh one.

### The Chrome step is not optional

Two things persist from it, and both matter:

- **Trust.** An aged profile with real cookies is most of why Carfax does not challenge every
  fetch immediately.
- **Your delivery ZIP.** Carvana decides its pricing zip from a cookie triple it writes itself,
  and that cookie is session-scoped — so it is absent at the start of every run and Carvana
  re-geolocates from your IP. `--login` captures the location Carvana writes *after you use its
  own location picker*, and every later run replays it. Type a zip into a box and nothing
  happens; use Carvana's picker and it sticks.

Shipping cost is part of landed price, which is part of the ranking. Getting this wrong changes
your results.

---

## Running it day to day

```bash
cd carvana-scraper
python3 -m carvana_scraper.worker
```

Leave it running. It polls for your queued searches and picks them up within a few seconds.

**If the site says your worker is offline, nothing will happen to anything you queue.** That is
the single most common confusion, and the fix is always "start the worker."

Useful flags:

| Flag | Effect |
|---|---|
| `--once` | Run one job and exit. Handy for testing. |
| `--poll-interval N` | Seconds between queue checks (default 5). |

---

## When a puzzle appears

Carfax sits behind DataDome. After roughly six report fetches in a session it shows a puzzle.

The site will say **"Puzzle waiting — check your Chrome window."** Go solve it in the Chrome
window your worker opened. The run continues by itself, and one solve typically covers the rest
of the shortlist.

**The tool never solves a puzzle for you, and never will.** If nobody solves it within the assist
timeout, that car is recorded as blocked — never cached — so the next run retries it. It shows up
under *Not classified* with a link to open the report yourself.

---

## Why your laptop {#why-your-laptop}

A reasonable question: why not run the scraping on the server?

Because it cannot. The pipeline needs a real Chrome window with a persistent, aged profile, a
human available to clear a puzzle, minutes of wall-clock time per run, and a writable disk for the
report cache. A serverless host has none of those. The workarounds — a hosted browser service,
rotating IPs, a CAPTCHA-solving service — are exactly what this project refuses to do, because
they turn human-scale research into something else.

So the site holds the queue and the results, and the browser work happens where a human is:
your machine, your IP, your profile, your puzzles.

---

## Troubleshooting

**"CARVANA_WEB_URL is not set"** — the `.env` did not load. Run `./setup.command` again, or
`export CARVANA_WEB_URL=https://…` before starting the worker.

**Every request fails, or the worker reports HTML where JSON should be** — the automation bypass
secret is missing or wrong. Vercel is serving the password page to your worker. Ask for the
secret and put it in `.env` as `VERCEL_AUTOMATION_BYPASS_SECRET`.

**"another Chrome is using the dedicated profile"** — you have the local app or another worker
running. Close it. The error text includes the exact `rm -f` command if a lock was left behind by
a crash.

**Your pairing code expired** — restart the worker, it prints a new one.

**Report review says it was skipped** — that feature shells out to a local `claude` CLI. If you do
not have it, you do not get the review. The ranking is unaffected; it is deterministic and does
not involve a model.

---

## What your machine sends

Your worker uploads, to the shared site: the ranking, the run manifest, and **the full text of the
Carfax and AutoCheck reports it fetched**. Everyone with the site password can read all of it.

That is a deliberate choice by whoever runs the instance, not an accident — but you should know it
before you pair. Your Carvana account credentials, cookies, and browser profile never leave your
machine.
