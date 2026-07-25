#!/bin/bash
# Double-clickable launcher for the Carvana ranker UI.
# Finder runs this from an arbitrary working directory, so cd to the repo first.
cd "$(dirname "$0")" || exit 1
exec python3 -m carvana_scraper.app
