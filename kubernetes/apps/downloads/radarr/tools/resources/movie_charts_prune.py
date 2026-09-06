#!/usr/bin/env python3
"""Prune Radarr movies that dropped out of the upstream MDBList chart.

The movie counterpart of tv_charts_prune.py. A movie is deleted only when
BOTH hold:
  * it carries the Radarr tag (default `movie-charts`) but is no longer in the
    upstream list the tagged import list points at, AND
  * nobody watched it within the last WATCH_WINDOW_DAYS.

The upstream URL is never configured twice - it is read out of Radarr's own
import-list config, so the list Radarr imports from and the list checked here
can never drift apart.

Everything else is a safety net, because both data sources fail in ways that
look like success:

  * mdblist serves `/lists/<user>/<slug>/json` through Cloudflare with
    `cache-control: max-age=86400`. A stale error body ("This List is Private")
    comes back with HTTP 200 for up to a day, so the status code proves nothing
    and every response is fetched with a cache buster and validated as data.
  * Tautulli returns `{"result": "error"}` inside an HTTP 200 envelope.

Anything unresolved keeps a movie alive: no upstream list, no Plex match, an
unreadable history - all of it protects rather than deletes. The only way a
movie is removed is a positive answer from both sources.

Movie ids differ from the show case in two ways worth knowing:
  * mdblist reports a movie's TMDb id in the generic `id` field and its IMDb id
    in `imdb_id`; the `tvdbid` it also carries is a TheTVDB *movie* id and does
    not exist in Radarr. Matching therefore runs on TMDb first, IMDb second.
  * Plex movie history hangs off `rating_key` directly - there is no
    grandparent, which is what an episode's show id would have been.

Env:
  RADARR_URL, RADARR_API_KEY         - Radarr instance and key
  TAUTULLI_URL, TAUTULLI_API_KEY     - Tautulli instance and key
  RADARR_TAG                         - tag marking chart imports (default movie-charts)
  DRY_RUN                            - "true" (default) logs without deleting
  MAX_DELETE                         - abort without deleting above this many candidates
  WATCH_MIN_PERCENT                  - percent_complete that counts as watched
  WATCH_WINDOW_DAYS                  - how far back history protects a movie
  GRACE_DAYS                         - freshly added movies are never touched
  PUSHGATEWAY_URL                    - optional; metrics are best-effort
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

log = logging.getLogger("movie-charts-prune")

RADARR_URL = os.environ.get("RADARR_URL", "").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
TAUTULLI_URL = os.environ.get("TAUTULLI_URL", "").rstrip("/")
TAUTULLI_API_KEY = os.environ.get("TAUTULLI_API_KEY", "")

RADARR_TAG = os.environ.get("RADARR_TAG", "movie-charts")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
MAX_DELETE = int(os.environ.get("MAX_DELETE", "5"))
WATCH_MIN_PERCENT = int(os.environ.get("WATCH_MIN_PERCENT", "20"))
WATCH_WINDOW_DAYS = int(os.environ.get("WATCH_WINDOW_DAYS", "30"))
GRACE_DAYS = int(os.environ.get("GRACE_DAYS", "30"))

PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", "").rstrip("/")
PUSHGATEWAY_JOB = os.environ.get("PUSHGATEWAY_JOB", "movie-charts-prune")

TIMEOUT = 30
HISTORY_PAGE = 1000

# Radarr's own list implementations name the upstream URL field differently
# (RadarrListImport uses `url`, the Sonarr-style ones `baseUrl`), so the field
# is looked up by any of its known names rather than assumed.
URL_FIELDS = ("url", "baseUrl", "listUrl")

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
# Radarr
# --------------------------------------------------------------------------

def radarr(path, method="GET", params=None):
    url = f"{RADARR_URL}/api/v3/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method, headers={"X-Api-Key": RADARR_API_KEY})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = resp.read()
    return json.loads(body) if body.strip() else None


def _size_of(movie):
    """Radarr reports a movie's size both top-level and under statistics."""
    return movie.get("sizeOnDisk") or (movie.get("statistics") or {}).get("sizeOnDisk", 0)


def resolve_tag_id():
    for tag in radarr("tag"):
        if tag["label"] == RADARR_TAG:
            return tag["id"]
    raise Fatal(f"tag {RADARR_TAG!r} does not exist in Radarr")


def resolve_upstream_url(tag_id):
    """Read the chart list's URL out of Radarr's own import-list config."""
    matches = [lst for lst in radarr("importlist") if tag_id in lst.get("tags", [])]
    if not matches:
        raise Fatal(f"no import list carries tag {RADARR_TAG!r}")
    if len(matches) > 1:
        names = ", ".join(lst["name"] for lst in matches)
        raise Fatal(f"expected exactly one import list tagged {RADARR_TAG!r}, found: {names}")

    lst = matches[0]
    # A disabled list stops refreshing the tag, so the tagged movies slowly go
    # stale rather than rotating - worth a warning, not an abort.
    if not lst.get("enabled", True):
        log.warning("import list %r is disabled in Radarr - its tag is no longer refreshed",
                    lst["name"])
    for field in lst.get("fields", []):
        if field.get("name") in URL_FIELDS and field.get("value"):
            return lst["name"], field["value"]
    raise Fatal(
        f"import list {lst['name']!r} has none of the {'/'.join(URL_FIELDS)} fields set"
    )


# --------------------------------------------------------------------------
# Upstream list
# --------------------------------------------------------------------------

def fetch_upstream(url):
    """Fetch the chart list, defeating the CDN cache and validating the body.

    mdblist answers with HTTP 200 for stale error bodies, so the response is
    only trusted once it parses as a non-empty array of movies with ids.
    Returns (tmdb_ids, imdb_ids); an entry carrying either one is enough.
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

    tmdb_ids, imdb_ids, skipped = set(), set(), 0
    for item in data:
        if item.get("mediatype") not in (None, "movie"):
            continue
        tmdb_id, imdb_id = item.get("id"), item.get("imdb_id")
        if tmdb_id:
            tmdb_ids.add(int(tmdb_id))
        if imdb_id:
            imdb_ids.add(str(imdb_id))
        if not tmdb_id and not imdb_id:
            skipped += 1
            log.warning("upstream entry without any id: %s", item.get("title", item))

    if not tmdb_ids and not imdb_ids:
        raise Fatal(f"upstream held {len(data)} entries but none carried an id")
    if skipped:
        log.warning("%d upstream entries lacked an id and were ignored", skipped)

    return tmdb_ids, imdb_ids


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


def build_id_maps():
    """Map Radarr tmdbId/imdbId -> Plex rating_key via each movie's Plex GUIDs.

    Titles are deliberately not used: German Plex titles routinely differ from
    Radarr's, and a missed title match would read as "nobody watched this".
    """
    sections = [
        s for s in tautulli("get_libraries")
        if s.get("section_type") == "movie"
    ]
    if not sections:
        raise Fatal("tautulli reports no movie libraries")

    by_tmdb, by_imdb, unidentified = {}, {}, 0
    for section in sections:
        info = tautulli(
            "get_library_media_info",
            section_id=section["section_id"],
            length=10000,
        )
        for movie in info.get("data", []):
            rating_key = movie.get("rating_key")
            if not rating_key:
                continue
            meta = tautulli("get_metadata", rating_key=rating_key) or {}
            matched = False
            for guid in meta.get("guids", []):
                if guid.startswith("tmdb://"):
                    tmdb_id = guid.removeprefix("tmdb://").split("?")[0]
                    if tmdb_id.isdigit():
                        by_tmdb[int(tmdb_id)] = str(rating_key)
                        matched = True
                elif guid.startswith("imdb://"):
                    imdb_id = guid.removeprefix("imdb://").split("?")[0]
                    if imdb_id:
                        by_imdb[imdb_id] = str(rating_key)
                        matched = True
            if not matched:
                # Counted rather than logged per item: movie libraries that
                # hold unmatched rips run to the hundreds, and none of them can
                # ever be a chart import anyway.
                unidentified += 1
                log.debug("plex movie %r (rating_key %s) exposes no tmdb or imdb guid",
                          movie.get("title"), rating_key)

    log.info("mapped %d plex movies by tmdb and %d by imdb across %d libraries",
             len(by_tmdb), len(by_imdb), len(sections))
    if unidentified:
        log.warning("%d plex movies exposed no tmdb or imdb guid and cannot be matched",
                    unidentified)
    return by_tmdb, by_imdb


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
            media_type="movie",
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
            key = row.get("rating_key")
            if key:
                watched.add(str(key))

        start += len(rows)
        if len(rows) < HISTORY_PAGE:
            break

    log.info("%d movies watched >=%d%% in the last %d days",
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

    for name in ("RADARR_URL", "RADARR_API_KEY", "TAUTULLI_URL", "TAUTULLI_API_KEY"):
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

        upstream_tmdb, upstream_imdb = fetch_upstream(upstream_url)
        stats["upstream_count"] = len(upstream_tmdb or upstream_imdb)
        log.info("upstream holds %d movies (%d with tmdb, %d with imdb ids)",
                 stats["upstream_count"], len(upstream_tmdb), len(upstream_imdb))

        tagged = [m for m in radarr("movie") if tag_id in m.get("tags", [])]
        stats["tagged_count"] = len(tagged)
        log.info("%d movies carry tag %r", len(tagged), RADARR_TAG)

        if not tagged:
            raise Fatal(
                f"no movie carries tag {RADARR_TAG!r} - the import list is not "
                "applying it; check that Radarr's list URL still resolves"
            )

        tmdb_to_key, imdb_to_key = build_id_maps()
        watched_keys = fetch_watched_keys(started - WATCH_WINDOW_DAYS * 86400)
        grace_cutoff = started - GRACE_DAYS * 86400

        candidates = []
        for movie in sorted(tagged, key=lambda m: m["title"]):
            title, tmdb_id, imdb_id = movie["title"], movie.get("tmdbId"), movie.get("imdbId")

            if tmdb_id in upstream_tmdb or (imdb_id and imdb_id in upstream_imdb):
                log.info("keep %s (tmdb %s): still upstream", title, tmdb_id)
                continue

            added = calendar.timegm(time.strptime(movie["added"][:19], "%Y-%m-%dT%H:%M:%S"))
            if added > grace_cutoff:
                stats["protected"]["grace"] += 1
                log.info("keep %s (tmdb %s): added %.0fd ago, within %dd grace",
                         title, tmdb_id, (started - added) / 86400, GRACE_DAYS)
                continue

            rating_key = tmdb_to_key.get(tmdb_id) or (imdb_id and imdb_to_key.get(imdb_id))
            if rating_key is None:
                stats["protected"]["unmatched"] += 1
                log.warning("keep %s (tmdb %s): no plex match - treating as watched",
                            title, tmdb_id)
                continue

            if rating_key in watched_keys:
                stats["protected"]["watched"] += 1
                log.info("keep %s (tmdb %s): watched within %dd",
                         title, tmdb_id, WATCH_WINDOW_DAYS)
                continue

            candidates.append(movie)
            log.info("candidate %s (tmdb %s): dropped upstream, unwatched, %.2f GiB",
                     title, tmdb_id, _size_of(movie) / 2**30)

        stats["candidates"] = len(candidates)

        if len(candidates) > MAX_DELETE:
            raise Fatal(
                f"{len(candidates)} candidates exceed MAX_DELETE={MAX_DELETE} - "
                "deleting nothing; review the list above and raise the cap to proceed"
            )

        if not candidates:
            log.info("no movies to remove")
        elif DRY_RUN:
            log.info("DRY_RUN - would delete %d movies (set DRY_RUN=false to act)",
                     len(candidates))
        else:
            for movie in candidates:
                size = _size_of(movie)
                try:
                    # Radarr spells the exclusion flag `addImportExclusion`, not
                    # Sonarr's `addImportListExclusion`.
                    radarr(f"movie/{movie['id']}", method="DELETE",
                           params={"deleteFiles": "true", "addImportExclusion": "false"})
                except (urllib.error.URLError, OSError) as exc:
                    stats["delete_errors"] += 1
                    log.error("deleting %s failed: %s", movie["title"], exc)
                else:
                    stats["deleted"] += 1
                    stats["freed_bytes"] += size
                    log.info("deleted %s (tmdb %s), freed %.2f GiB",
                             movie["title"], movie.get("tmdbId"), size / 2**30)

        status = 0 if stats["delete_errors"] == 0 else 1

    except Fatal as exc:
        log.error("%s", exc)
        status = 1
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        log.error("run failed: %s", exc)
        status = 1

    metrics = [
        f"movie_charts_prune_status {1 if status == 0 else 0}",
        f"movie_charts_prune_dry_run {1 if DRY_RUN else 0}",
        f"movie_charts_prune_upstream_count {stats['upstream_count']}",
        f"movie_charts_prune_tagged_count {stats['tagged_count']}",
        f"movie_charts_prune_candidates {stats['candidates']}",
        f"movie_charts_prune_deleted {stats['deleted']}",
        f"movie_charts_prune_delete_errors {stats['delete_errors']}",
        f"movie_charts_prune_freed_bytes {stats['freed_bytes']}",
        f"movie_charts_prune_duration_seconds {time.time() - started:.1f}",
    ]
    metrics += [
        f'movie_charts_prune_protected{{reason="{reason}"}} {count}'
        for reason, count in stats["protected"].items()
    ]
    if status == 0:
        metrics.append(f"movie_charts_prune_last_success_timestamp_seconds {time.time():.0f}")
    push_metrics(metrics)

    return status


if __name__ == "__main__":
    sys.exit(main())
