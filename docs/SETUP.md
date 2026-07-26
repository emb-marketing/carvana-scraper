# Running the machine that serves GRID

**Most people reading about GRID do not need this page.** To *use* the site you need two things:
the link and the PIN. Open it, enter the PIN, search. Nothing to install.

This page is for setting up a machine that *does* the work — either as the one serving everyone,
or as your own so that your searches run on your laptop.

---

## Why one machine has to run something

The site queues searches. It cannot run them.

Scraping needs a real Chrome window with a persistent, aged profile, a human available to clear a
DataDome puzzle, several minutes per run, and a writable disk for the report cache. Vercel is
serverless — short-lived cloud functions with no display, no browser and no persistent disk.

Nor can the page in *your* browser do it: the sandbox forbids a page on one origin from reading
another site's content, which is exactly what makes browsing safe. So running searches on your own
machine means running the scraper there, not just opening a tab.

At least one machine has to be up for anything to run. Leave one running and GRID works for
everyone; close them all and searches queue until one returns.

---

## What that machine needs

| | |
|---|---|
| **macOS** | The launcher is a `.command` file. Linux and Windows work; run the Python directly. |
| **Python 3.11+** | `python3 --version`. macOS ships one; otherwise [python.org](https://www.python.org/downloads/). |
| **Google Chrome** | The real one, from [google.com/chrome](https://www.google.com/chrome/). Not Chromium, not Brave. |
| **The site** | You need the link and the PIN to download the worker in the first place. |

---

## Setup

Open the site, go to **Set up**, and download `grid-worker.tar.gz`. Then either double-click
`start.command` after unzipping, or paste this:

```bash
cd ~/Downloads && tar xzf grid-worker.tar.gz && cd grid-worker && ./start.command
```

The site URL is already inside the download, so there is nothing to configure. It checks Python and
Chrome, installs Playwright, opens Chrome once so you can set your delivery ZIP, and starts the
worker. When you see

```
Running as 'your-machine'.
https://… can now run searches.
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

### Making searches run on *your* laptop

By default a search runs on whichever machine is online. To make **your** searches use **your**
laptop, run the worker there and open the link it prints:

```
  To make YOUR searches run on THIS machine, open once in your browser:
    https://…/link?t=…
```

Open it once. That browser is now bound to that machine, and everything you submit runs there.
Your friend does the same on theirs and gets the same result — their searches, their laptop.

A browser that has never done this is not broken: its searches go to the pool and run on whoever
is online. That is what lets someone who installed nothing use the site at all.

**A web page cannot do this for you.** The browser sandbox forbids a page on one origin from
reading another site's content — that restriction is what makes browsing safe, and it is why using
your own machine means running the scraper on it rather than just opening a tab.

---

## Day to day

```bash
cd ~/Downloads/grid-worker && ./start.command
```

or, equivalently, `python3 -m carvana_scraper.worker` from that folder — the download ships a
`.env` holding the site URL, so both work.

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

**"CARVANA_WEB_URL is not set"** — you are running from a folder with no `.env`. Use
`./start.command`, or `CARVANA_WEB_URL=https://… python3 -m carvana_scraper.worker`.

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
