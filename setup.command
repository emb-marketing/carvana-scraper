#!/bin/bash
# Double-clickable setup for the machine that serves GRID.
#
# Gets a fresh machine from "cloned the repo" to "the site is live and running searches" without
# editing a file by hand. Safe to re-run: every step is a no-op when it has already been done.
#
# Finder runs this from an arbitrary working directory, so cd to the repo first.
set -u
cd "$(dirname "$0")" || exit 1

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m  %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mSetup stopped: %s\033[0m\n\n' "$*"; exit 1; }

say "GRID — set up this machine to run searches"

# ---- 1. Python -----------------------------------------------------------------------------

command -v python3 >/dev/null 2>&1 || die "python3 is not installed. Install Python 3.11 or newer."

python3 - <<'PY' || die "Python 3.11 or newer is required."
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
printf '  Python %s\n' "$(python3 -c 'import platform; print(platform.python_version())')"

# ---- 2. Real Google Chrome ------------------------------------------------------------------

# Playwright drives real Chrome (channel="chrome"), not a bundled Chromium. An aged profile in
# real Chrome is most of why report fetches are not challenged immediately.
if [ ! -d "/Applications/Google Chrome.app" ]; then
  die "Google Chrome is not installed. Get it from https://www.google.com/chrome/ — this tool
  drives real Chrome, not a bundled browser, and that is deliberate."
fi
printf '  Google Chrome found\n'

# ---- 3. Dependencies -------------------------------------------------------------------------

say "Installing dependencies (Playwright only)"
python3 -m pip install --quiet --user -r requirements.txt || die "pip install failed."
printf '  Done\n'

# ---- 4. Where the site lives ------------------------------------------------------------------

ENV_FILE=".env"
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "./$ENV_FILE"
fi

if [ -z "${CARVANA_WEB_URL:-}" ]; then
  say "Where is GRID deployed?"
  printf '  Paste the site URL (e.g. https://grid.vercel.app): '
  read -r CARVANA_WEB_URL
  [ -n "$CARVANA_WEB_URL" ] || die "A site URL is required."
  printf 'CARVANA_WEB_URL=%s\n' "$CARVANA_WEB_URL" >> "$ENV_FILE"
fi

chmod 600 "$ENV_FILE" 2>/dev/null

# ---- 5. Chrome login ---------------------------------------------------------------------------

# Two things persist and both matter: profile trust, and the delivery location that decides which
# zip Carvana prices against. Skipped when a profile already exists so re-running is cheap.
if [ ! -d ".browser-profile" ]; then
  say "Opening Chrome once to establish the profile"
  cat <<'EOF'
  A Chrome window will open on Carvana. While it is open:
    1. Set your delivery ZIP using Carvana's own location picker.
    2. Sign in if you have an account (optional, but it ages the profile).
  Then close the window, or press Enter in the terminal, to continue.

EOF
  python3 -m carvana_scraper --login || warn "Login did not complete — you can re-run this later
  with: python3 -m carvana_scraper --login"
else
  printf '  Existing browser profile found — skipping login.\n'
  printf '  Re-run it any time with: python3 -m carvana_scraper --login\n'
fi

# ---- 6. Start the worker ------------------------------------------------------------------------

say "Starting the worker"
cat <<'EOF'
  Leave this window open. While it runs, anyone with the site link and PIN can run searches —
  they install nothing. Close it and their searches just queue until you are back.

  Ctrl-C to stop.

EOF

export CARVANA_WEB_URL
export VERCEL_AUTOMATION_BYPASS_SECRET="${VERCEL_AUTOMATION_BYPASS_SECRET:-}"
exec python3 -m carvana_scraper.worker
