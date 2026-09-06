#!/usr/bin/env python3
"""Prune Sonarr series that dropped out of the upstream MDBList chart.

A series is deleted only when BOTH hold:
  * it carries the Sonarr tag (default `tv-charts`) but is no longer in the
    upstream list the tagged import list points at, AND
  * nobody watched any of its episodes within the last WATCH_WINDOW_DAYS.

The upstream URL is never configured twice - it is read out of Sonarr's own
import-list config, so the list Sonarr imports from and the list checked here
can never drift apart.

Everything else is a safety net, because both data sources fail in ways that
look like success:

  * mdblist serves `/lists/<user>/<slug>/json` through Cloudflare with
    `cache-control: max-age=86400`. A stale error body ("This List is Private")
    comes back with HTTP 200 for up to a day, so the status code proves nothing
    and every response is fetched with a cache buster and validated as data.
  * Tautulli returns `{"result": "error"}` inside an HTTP 200 envelope.

Anything unresolved keeps a series alive: no upstream list, no Plex match, an
unreadable history - all of it protects rather than deletes. The only way a
series is removed is a positive answer from both sources.

Env:
  SONARR_URL, SONARR_API_KEY        - Sonarr instance and key
  TAUTULLI_URL, TAUTULLI_API_KEY    - Tautulli instance and key
  SONARR_TAG                        - tag marking chart imports (default tv-charts)
  DRY_RUN                           - "true" (default) logs without deleting
  MAX_DELETE                        - abort without deleting above this many candidates
  WATCH_MIN_PERCENT                 - percent_complete that counts as watched
  WATCH_WINDOW_DAYS                 - how far back history protects a series
  GRACE_DAYS                        - freshly added series are never touched
  PUSHGATEWAY_URL                   - optional; metrics are best-effort
"""

import calendar
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("tv-charts-prune")

SONARR_URL = os.environ.get("SONARR_URL", "").rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")
TAUTULLI_URL = os.environ.get("TAUTULLI_URL", "").rstrip("/")
TAUTULLI_API_KEY = os.environ.get("TAUTULLI_API_KEY", "")

SONARR_TAG = os.environ.get("SONARR_TAG", "tv-charts")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
MAX_DELETE = int(os.environ.get("MAX_DELETE", "5"))
WATCH_MIN_PERCENT = int(os.environ.get("WATCH_MIN_PERCENT", "20"))
WATCH_WINDOW_DAYS = int(os.environ.get("WATCH_WINDOW_DAYS", "30"))
GRACE_DAYS = int(os.environ.get("GRACE_DAYS", "30"))

PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", "").rstrip("/")
PUSHGATEWAY_JOB = os.environ.get("PUSHGATEWAY_JOB", "tv-charts-prune")

TIMEOUT = 30
HISTORY_PAGE = 1000

# Cloudflare answers the default Python-urllib agent with 403 on mdblist, so
# every outbound request identifies as a browser.
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class Fatal(Exception):
    """Abort the run without deleting anything."""


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = resp.read()
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise Fatal(
            f"{_redact(url)} returned non-JSON ({len(body)} bytes) - a chart list "
            "URL must end in /json and carry its query string after a '?'"
        ) from exc


def _redact(url):
    """Strip api keys so URLs are safe to log."""
    for param in ("apikey", "api_key"):
        url = re.sub(rf"({param}=)[^&]*", r"\1<redacted>", url)
    return url


# --------------------------------------------------------------------------
# Sonarr
# --------------------------------------------------------------------------

def sonarr(path, method="GET", params=None):
    url = f"{SONARR_URL}/api/v3/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method, headers={"X-Api-Key": SONARR_API_KEY})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = resp.read()
    return json.loads(body) if body.strip() else None


def resolve_tag_id():
    for tag in sonarr("tag"):
        if tag["label"] == SONARR_TAG:
            return tag["id"]
    raise Fatal(f"tag {SONARR_TAG!r} does not exist in Sonarr")


def resolve_upstream_url(tag_id):
    """Read the chart list's URL out of Sonarr's own import-list config."""
    matches = [lst for lst in sonarr("importlist") if tag_id in lst.get("tags", [])]
    if not matches:
        raise Fatal(f"no import list carries tag {SONARR_TAG!r}")
    if len(matches) > 1:
        names = ", ".join(lst["name"] for lst in matches)
        raise Fatal(f"expected exactly one import list tagged {SONARR_TAG!r}, found: {names}")

    lst = matches[0]
    for field in lst.get("fields", []):
        if field.get("name") == "baseUrl" and field.get("value"):
            return lst["name"], field["value"]
    raise Fatal(f"import list {lst['name']!r} has no baseUrl field")


# --------------------------------------------------------------------------
# Upstream list
# --------------------------------------------------------------------------

def fetch_upstream(url):
    """Fetch the chart list, defeating the CDN cache and validating the body.

    mdblist answers with HTTP 200 for stale error bodies, so the response is
    only trusted once it parses as a non-empty array of shows with tvdb ids.
    """
    separator = "&" if "?" in url else "?"
    busted = f"{url}{separator}_cb={int(time.time())}"

    data = _get_json(busted, headers={"Cache-Control": "no-cache"})

    if isinstance(data, dict):
        raise Fatal(f"upstream returned an error object: {data.get('error', data)}")
    if not isinstance(data, list):
        raise Fatal(f"upstream returned {type(data).__name__}, expected a list")
    if not data:
        raise Fatal("upstream list is empty - refusing to treat everything as removed")

    ids, skipped = set(), 0
    for item in data:
        if item.get("mediatype") not in (None, "show"):
            continue
        tvdb_id = item.get("tvdbid")
        if tvdb_id:
            ids.add(int(tvdb_id))
        else:
            skipped += 1
            log.warning("upstream entry without tvdbid: %s", item.get("title", item))

    if not ids:
        raise Fatal(f"upstream held {len(data)} entries but none carried a tvdbid")
    if skipped:
        log.warning("%d upstream entries lacked a tvdbid and were ignored", skipped)

    return ids


# --------------------------------------------------------------------------
# Tautulli
# --------------------------------------------------------------------------

def tautulli(cmd, **params):
    query = {"apikey": TAUTULLI_API_KEY, "cmd": cmd, **params}
    url = f"{TAUTULLI_URL}/api/v2?" + urllib.parse.urlencode(query)
    payload = _get_json(url).get("response", {})
    if payload.get("result") != "success":
        raise Fatal(f"tautulli {cmd} failed: {payload.get('message')}")
    return payload.get("data")


def build_tvdb_map():
    """Map Sonarr tvdbId -> Plex rating_key via each show's Plex GUIDs.

    Titles are deliberately not used: German Plex titles routinely differ from
    Sonarr's, and a missed title match would read as "nobody watched this".
    """
    sections = [
        s for s in tautulli("get_libraries")
        if s.get("section_type") == "show"
    ]
    if not sections:
        raise Fatal("tautulli reports no show libraries")

    mapping = {}
    for section in sections:
        info = tautulli(
            "get_library_media_info",
            section_id=section["section_id"],
            length=10000,
        )
        for show in info.get("data", []):
            rating_key = show.get("rating_key")
            if not rating_key:
                continue
            meta = tautulli("get_metadata", rating_key=rating_key) or {}
            for guid in meta.get("guids", []):
                if guid.startswith("tvdb://"):
                    tvdb_id = guid.removeprefix("tvdb://").split("?")[0]
                    if tvdb_id.isdigit():
                        mapping[int(tvdb_id)] = str(rating_key)
                    break
            else:
                log.warning(
                    "plex show %r (rating_key %s) exposes no tvdb guid",
                    show.get("title"), rating_key,
                )
    log.info("mapped %d plex shows to tvdb ids", len(mapping))
    return mapping


def fetch_watched_keys(cutoff):
    """Collect rating_keys watched past the threshold since `cutoff`.

    `after` is passed as a server-side hint but every row is re-checked against
    `cutoff` locally, so the window holds even if the parameter is ignored.
    """
    watched, start = set(), 0
    after = time.strftime("%Y-%m-%d", time.localtime(cutoff))

    while True:
        page = tautulli(
            "get_history",
            media_type="episode",
            after=after,
            start=start,
            length=HISTORY_PAGE,
        )
        rows = page.get("data", [])
        if not rows:
            break

        for row in rows:
            if int(row.get("date") or 0) < cutoff:
                continue
            if int(row.get("percent_complete") or 0) < WATCH_MIN_PERCENT:
                continue
            key = row.get("grandparent_rating_key")
            if key:
                watched.add(str(key))

        start += len(rows)
        if len(rows) < HISTORY_PAGE:
            break

    log.info("%d shows watched >=%d%% in the last %d days",
             len(watched), WATCH_MIN_PERCENT, WATCH_WINDOW_DAYS)
    return watched


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def push_metrics(metrics):
    """Best-effort push. POST replaces only the metric families we send, so a
    failed run reports status 0 without clobbering the last success timestamp
    the staleness alert depends on."""
    if not PUSHGATEWAY_URL:
        return
    body = "".join(f"{line}\n" for line in metrics).encode()
    url = f"{PUSHGATEWAY_URL}/metrics/job/{urllib.parse.quote(PUSHGATEWAY_JOB)}"
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "text/plain; version=0.0.4"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT):
            pass
    except (urllib.error.URLError, OSError) as exc:
        log.warning("pushing metrics failed: %s", exc)


# --------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    for name in ("SONARR_URL", "SONARR_API_KEY", "TAUTULLI_URL", "TAUTULLI_API_KEY"):
        if not globals()[name]:
            log.error("%s is not set", name)
            return 1

    started = time.time()
    stats = {
        "upstream_count": 0, "tagged_count": 0, "candidates": 0, "deleted": 0,
        "delete_errors": 0, "freed_bytes": 0,
        "protected": {"watched": 0, "grace": 0, "unmatched": 0},
    }
    status = 0

    try:
        tag_id = resolve_tag_id()
        list_name, upstream_url = resolve_upstream_url(tag_id)
        log.info("chart list %r -> %s", list_name, _redact(upstream_url))

        upstream_ids = fetch_upstream(upstream_url)
        stats["upstream_count"] = len(upstream_ids)
        log.info("upstream holds %d shows", len(upstream_ids))

        tagged = [s for s in sonarr("series") if tag_id in s.get("tags", [])]
        stats["tagged_count"] = len(tagged)
        log.info("%d series carry tag %r", len(tagged), SONARR_TAG)

        if not tagged:
            raise Fatal(
                f"no series carries tag {SONARR_TAG!r} - the import list is not "
                "applying it; check that Sonarr's list URL still resolves"
            )

        tvdb_to_key = build_tvdb_map()
        watched_keys = fetch_watched_keys(started - WATCH_WINDOW_DAYS * 86400)
        grace_cutoff = started - GRACE_DAYS * 86400

        candidates = []
        for series in sorted(tagged, key=lambda s: s["title"]):
            title, tvdb_id = series["title"], series["tvdbId"]

            if tvdb_id in upstream_ids:
                log.info("keep %s (tvdb %s): still upstream", title, tvdb_id)
                continue

            added = calendar.timegm(time.strptime(series["added"][:19], "%Y-%m-%dT%H:%M:%S"))
            if added > grace_cutoff:
                stats["protected"]["grace"] += 1
                log.info("keep %s (tvdb %s): added %.0fd ago, within %dd grace",
                         title, tvdb_id, (started - added) / 86400, GRACE_DAYS)
                continue

            rating_key = tvdb_to_key.get(tvdb_id)
            if rating_key is None:
                stats["protected"]["unmatched"] += 1
                log.warning("keep %s (tvdb %s): no plex match - treating as watched",
                            title, tvdb_id)
                continue

            if rating_key in watched_keys:
                stats["protected"]["watched"] += 1
                log.info("keep %s (tvdb %s): watched within %dd",
                         title, tvdb_id, WATCH_WINDOW_DAYS)
                continue

            candidates.append(series)
            log.info("candidate %s (tvdb %s): dropped upstream, unwatched, %.2f GiB",
                     title, tvdb_id, (series.get("statistics") or {}).get("sizeOnDisk", 0) / 2**30)

        stats["candidates"] = len(candidates)

        if len(candidates) > MAX_DELETE:
            raise Fatal(
                f"{len(candidates)} candidates exceed MAX_DELETE={MAX_DELETE} - "
                "deleting nothing; review the list above and raise the cap to proceed"
            )

        if not candidates:
            log.info("no series to remove")
        elif DRY_RUN:
            log.info("DRY_RUN - would delete %d series (set DRY_RUN=false to act)",
                     len(candidates))
        else:
            for series in candidates:
                size = (series.get("statistics") or {}).get("sizeOnDisk", 0)
                try:
                    sonarr(f"series/{series['id']}", method="DELETE",
                           params={"deleteFiles": "true", "addImportListExclusion": "false"})
                except (urllib.error.URLError, OSError) as exc:
                    stats["delete_errors"] += 1
                    log.error("deleting %s failed: %s", series["title"], exc)
                else:
                    stats["deleted"] += 1
                    stats["freed_bytes"] += size
                    log.info("deleted %s (tvdb %s), freed %.2f GiB",
                             series["title"], series["tvdbId"], size / 2**30)

        status = 0 if stats["delete_errors"] == 0 else 1

    except Fatal as exc:
        log.error("%s", exc)
        status = 1
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        log.error("run failed: %s", exc)
        status = 1

    metrics = [
        f"tv_charts_prune_status {1 if status == 0 else 0}",
        f"tv_charts_prune_dry_run {1 if DRY_RUN else 0}",
        f"tv_charts_prune_upstream_count {stats['upstream_count']}",
        f"tv_charts_prune_tagged_count {stats['tagged_count']}",
        f"tv_charts_prune_candidates {stats['candidates']}",
        f"tv_charts_prune_deleted {stats['deleted']}",
        f"tv_charts_prune_delete_errors {stats['delete_errors']}",
        f"tv_charts_prune_freed_bytes {stats['freed_bytes']}",
        f"tv_charts_prune_duration_seconds {time.time() - started:.1f}",
    ]
    metrics += [
        f'tv_charts_prune_protected{{reason="{reason}"}} {count}'
        for reason, count in stats["protected"].items()
    ]
    if status == 0:
        metrics.append(f"tv_charts_prune_last_success_timestamp_seconds {time.time():.0f}")
    push_metrics(metrics)

    return status


if __name__ == "__main__":
    sys.exit(main())
