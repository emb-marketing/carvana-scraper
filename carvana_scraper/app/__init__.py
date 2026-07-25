"""Local browser-based GUI over the ranking pipeline.

Runs a stdlib HTTP server bound to 127.0.0.1 and serves one page. No new dependencies: the
project's rule is Playwright only, everything else stdlib, and that holds here.

    python3 -m carvana_scraper.app

The pipeline itself is unchanged — this package only drives `pipeline.execute`, renders its events,
and adds the two things a terminal cannot do well: dropdowns built from Carvana's real taxonomy,
and pasting in a report that could not be scraped.
"""

__all__ = ["ingest", "review", "runner", "serialize", "server", "state"]
