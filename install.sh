#!/bin/bash
# One-command install for the Carvana ranker.
#
#   curl -fsSL https://raw.githubusercontent.com/emb-marketing/carvana-scraper/main/install.sh | bash
#
# This exists to be piped into bash, which is also its one real hazard: when you do that, stdin is
# the script itself. Anything reading stdin swallows script text instead — and `browser.login()`
# ends in a bare `input()`, which on a non-TTY stdin raises EOFError. So the single step that needs
# a human reads /dev/tty explicitly, and with no terminal at all the script prints the commands
# rather than failing somewhere obscure.
#
# Safe to re-run. An existing install is updated in place, and the Chrome profile, the report cache
# and .env are never touched — none of them are in the archive.
set -u

REPO="emb-marketing/carvana-scraper"
BRANCH="${CARVANA_BRANCH:-main}"
DIR="${CARVANA_DIR:-$HOME/carvana-scraper}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '\033[33m  %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mInstall stopped: %s\033[0m\n\n' "$*"; exit 1; }

say "Carvana ranker"

# ---- 1. Somewhere to ask questions -----------------------------------------------------------

# /dev/tty is the real keyboard however this was invoked; under `curl | bash` stdin is not.
if (: < /dev/tty) 2>/dev/null; then
  TTY="/dev/tty"
else
  TTY=""
fi

# ---- 2. Python -------------------------------------------------------------------------------

command -v python3 >/dev/null 2>&1 || die "python3 is not installed. Get Python 3.11 or newer from
  https://www.python.org/downloads/ and run this again."

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Python 3.11 or newer is required, found $(python3 -c 'import platform; print(platform.python_version())')."
info "Python $(python3 -c 'import platform; print(platform.python_version())')"

# ---- 3. Real Google Chrome -------------------------------------------------------------------

# Playwright drives the actual Chrome install (channel="chrome"), not a bundled Chromium, because an
# aged real-Chrome profile is most of why report fetches are not challenged immediately. So there is
# no browser to download here — but there is one that has to already exist.
if [ -d "/Applications/Google Chrome.app" ] \
   || command -v google-chrome >/dev/null 2>&1 \
   || command -v google-chrome-stable >/dev/null 2>&1; then
  info "Google Chrome found"
else
  die "Google Chrome is not installed. Get it from https://www.google.com/chrome/ — this tool
  drives real Chrome rather than a bundled browser, and that is deliberate."
fi

# ---- 4. The code -----------------------------------------------------------------------------

# An existing git checkout is updated with git, so someone's local work is not clobbered. Anything
# else takes the tarball: it needs no git, which on a fresh Mac means no Xcode Command Line Tools
# prompt appearing halfway through an install.
if [ -d "$DIR/.git" ]; then
  say "Updating $DIR"
  git -C "$DIR" pull --ff-only \
    || warn "git pull did not fast-forward — leaving this checkout exactly as it is."
else
  say "Downloading to $DIR"
  mkdir -p "$DIR" || die "cannot create $DIR"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" \
    | tar xz --strip-components=1 -C "$DIR" \
    || die "download failed. Check your network, and that github.com/$REPO is reachable."
fi

cd "$DIR" || die "cannot enter $DIR"

# ---- 5. Dependencies -------------------------------------------------------------------------

# Playwright is the only one. Everything else the tool uses is the standard library.
say "Installing Playwright"
python3 -m pip install --quiet --user -r requirements.txt 2>/dev/null \
  || python3 -m pip install --quiet --user --break-system-packages -r requirements.txt 2>/dev/null \
  || warn "pip reported a problem — checking whether Playwright is importable anyway."

# pip's exit code is not the question; whether *this* interpreter can import it is. A user-site
# install is invisible to PATH yet perfectly importable, and Homebrew's Python refuses a plain
# --user install under PEP 668 while accepting --break-system-packages.
python3 -c 'import playwright' 2>/dev/null \
  || die "Playwright is not importable. Try: python3 -m pip install --user playwright"
info "Playwright ready"

# ---- 6. Chrome profile and delivery ZIP ------------------------------------------------------

# Two things persist from this and both matter: profile trust, and the delivery location that
# decides which zip Carvana prices against. Carvana honours only its own location picker, and the
# zip cookie is session-scoped, so --login captures what Carvana wrote and every later run replays
# it. Skipped once a profile exists, which is what makes re-running this script cheap.
if [ -d ".browser-profile" ]; then
  info "Existing Chrome profile — skipping login"
elif [ -z "$TTY" ]; then
  warn "No terminal available, so the one-time Chrome login was skipped. Run it yourself:"
  warn "  cd $DIR && python3 -m carvana_scraper --login"
else
  say "Opening Chrome once"
  cat <<'EOF'
  A Chrome window will open on Carvana. While it is open:
    1. Set your delivery ZIP using Carvana's own location picker.
       Typing a zip anywhere else does nothing — only the picker sticks.
    2. Sign in if you have an account. Optional, but it ages the profile.
  Then press Enter back in this terminal.

EOF
  python3 -m carvana_scraper --login < "$TTY" \
    || warn "Login did not finish. Re-run it: cd $DIR && python3 -m carvana_scraper --login"
fi

# ---- 7. Go -----------------------------------------------------------------------------------

if [ -z "$TTY" ]; then
  say "Installed. To start it:"
  info "cd $DIR && python3 -m carvana_scraper.app"
  exit 0
fi

say "Starting the app"
cat <<EOF
  Opens http://127.0.0.1:8765 in your browser. Pick a make and model, then set both a max
  price and a max mileage — they are the scoring anchors, and scores are not comparable
  between runs without them.

  Ctrl-C to stop. Start it again later with:
    cd $DIR && ./run-app.command

EOF
exec python3 -m carvana_scraper.app
