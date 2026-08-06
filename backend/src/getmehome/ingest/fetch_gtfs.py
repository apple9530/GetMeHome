"""Download a city's GTFS feeds.

Split out of the Makefile because the two agencies need different handling and
a shell recipe branching on the city is worse than a small script that reads
the city record.

WMATA gates its static feeds behind the same key as its real-time API; the MTA
publishes its openly. New York needs six feeds — the subway plus one per
borough for buses — which is more than a Makefile line wants to carry.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import httpx

from ..cities import CITIES, DEFAULT_CITY, City, get_city
from ..config import DATA_DIR, WMATA_API_KEY

log = logging.getLogger("getmehome.ingest.gtfs")

# Feeds are tens of megabytes and served from S3 or an API gateway, neither of
# which is reliably quick.
_TIMEOUT_S = 300.0


def fetch_feeds(city: City, root: Path = DATA_DIR, force: bool = False) -> int:
    """Download every feed a city declares. Returns the number that failed."""
    raw = city.raw_dir(root)
    raw.mkdir(parents=True, exist_ok=True)

    failed = 0
    for feed in city.transit_feeds:
        target = raw / f"{feed.name}.zip"
        if target.exists() and not force:
            log.info("%s already present (%.1f MB)", target.name, target.stat().st_size / 1e6)
            continue

        headers = {}
        if feed.needs_wmata_key:
            if not WMATA_API_KEY:
                log.error(
                    "%s needs WMATA_API_KEY. Get a free key at "
                    "https://developer.wmata.com/ and export it.",
                    feed.name,
                )
                failed += 1
                continue
            headers["api_key"] = WMATA_API_KEY

        log.info("downloading %s", feed.url)
        try:
            with httpx.stream(
                "GET", feed.url, headers=headers, timeout=_TIMEOUT_S,
                follow_redirects=True,
            ) as response:
                response.raise_for_status()
                # Written to a temporary name and moved into place, so an
                # interrupted download cannot leave a truncated zip that the
                # build then tries to read.
                partial = target.with_suffix(".zip.partial")
                with partial.open("wb") as fh:
                    for chunk in response.iter_bytes(chunk_size=1 << 20):
                        fh.write(chunk)
                partial.replace(target)
        except httpx.HTTPError as exc:
            log.error("%s failed: %s", feed.name, exc)
            failed += 1
            continue

        log.info("wrote %s (%.1f MB)", target, target.stat().st_size / 1e6)

    return failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download a city's GTFS feeds")
    parser.add_argument("--city", default=DEFAULT_CITY, choices=sorted(CITIES))
    parser.add_argument(
        "--force", action="store_true", help="re-download feeds already present"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    city = get_city(args.city)

    if not city.transit_feeds:
        log.warning("%s declares no GTFS feeds", city.name)
        return 0

    failed = fetch_feeds(city, force=args.force)
    if failed:
        log.error(
            "%d of %d feeds failed. The build will use whatever arrived; "
            "transit coverage will be partial.",
            failed, len(city.transit_feeds),
        )
        return 1

    log.info("all %s feeds downloaded to %s", city.slug, city.raw_dir(DATA_DIR))
    return 0


if __name__ == "__main__":
    sys.exit(main())
