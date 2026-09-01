#!/usr/bin/env python3
"""Resolve an Audible ASIN for a downloaded audiobook.

Inputs (env):
  TITLE_B64, AUTHOR_B64   - base64 of the Shelfmark-provided title / author
  SOURCE_PATH_B64         - base64 of the downloaded file or folder path
  ASIN_OUT               - file to write the result to (default /work/asin)
  CHAPTERS_OUT          - ffmetadata chapter file to write (default /work/chapters.ffmeta)
  METADATA_OUT         - beets-audible metadata.yml to write (default /work/metadata.yml)
  COVER_OUT            - cover image to download to (default /work/cover.jpg)
  TAGS_OUT             - ffmetadata global-tag file to write (default /work/tags.ffmeta)
  MIN_SCORE             - minimum match score to accept (default 0.75)

Output: writes "<ASIN> <region>" to ASIN_OUT when a confident match is found.
On a match it is the single metadata resolver for the pipeline: it fetches the
region's book + chapters from Audnex and writes CHAPTERS_OUT (ffmetadata),
METADATA_OUT (a beets-audible metadata.yml so beets tags deterministically
without a fuzzy Audible lookup), COVER_OUT (cover art) and TAGS_OUT (ffmetadata
global tags the merge step embeds as a floor). m4b-merge is never handed an ASIN
- its own Audnex client sends no region and 404s on any non-us book.

With no confident match it still writes TAGS_OUT from the Shelfmark-provided
title / author so the merged file is at least named and shelved correctly.
Always exits 0 - a missing ASIN just means no Audible enrichment, which is far
better than the wrong book.

Matching strategy:
  * query Audible's catalog with the dedicated `title` filter, NOT a keyword
    mash of "author title" (that returns fuzzy garbage - e.g. "Brian Sibley Der
    Herr der Ringe" matched "Im Ankleidezimmer der Herrin");
  * hard-gate candidates on a shared significant title token (stopwords removed);
  * score on wanted-title coverage + sequence similarity, with the Shelfmark
    author / narrator as a corroborating bonus - never a hard filter, because
    Audible often credits the original writer, not the dramatiser.
"""

import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from difflib import SequenceMatcher

REGIONS = [("de", "https://api.audible.de"), ("us", "https://api.audible.com")]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ASIN_RE = re.compile(r"\b(B0[A-Z0-9]{8})\b")
TIMEOUT = 15
RESPONSE_GROUPS = "contributors,product_desc,series"
AUDNEX = "https://api.audnex.us"

# Dropped before token comparison: articles, prepositions and audiobook-format
# noise that carries no disambiguating signal.
STOPWORDS = {
    "der", "die", "das", "des", "dem", "den", "ein", "eine", "einen", "einer",
    "eines", "und", "oder", "im", "in", "am", "an", "auf", "zu", "zum", "zur",
    "von", "vom", "the", "a", "of", "and", "or", "to", "on",
    "hoerbuch", "hoerspiel", "ungekuerzt", "gekuerzt", "roman", "teil", "band",
    "vol", "volume", "edition",
}


def log(msg: str) -> None:
    print(f"[asin-lookup] {msg}", file=sys.stderr, flush=True)


def b64(name: str) -> str:
    try:
        return base64.b64decode(os.environ.get(name, "")).decode().strip()
    except Exception:  # noqa: BLE001
        return ""


def norm(text: str) -> str:
    text = text.lower()
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(src, dst)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def toks(text: str) -> set[str]:
    return {t for t in norm(text).split() if len(t) > 1 and t not in STOPWORDS}


def names(product: dict, key: str) -> set[str]:
    out: set[str] = set()
    for entry in product.get(key) or []:
        out |= toks(entry.get("name", ""))
    return out


def asin_from_source(path: str) -> str | None:
    """m4b-merge convention: an ASIN in brackets in the folder / file name."""
    candidates = [os.path.basename(path.rstrip("/"))]
    if os.path.isdir(path):
        try:
            candidates += os.listdir(path)
        except OSError:
            pass
    for cand in candidates:
        match = ASIN_RE.search(cand.upper())
        if match:
            return match.group(1)
    return None


def search(base_url: str, params: dict) -> list[dict]:
    query = dict(
        params,
        num_results="20",
        products_sort_by="Relevance",
        response_groups=RESPONSE_GROUPS,
    )
    url = f"{base_url}/1.0/catalog/products?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.load(resp)
    return data.get("products", []) or []


def score(product: dict, want_toks: set[str], want_norm: str, want_author: str) -> float:
    cand_toks = toks(product.get("title") or "")
    if not want_toks or not cand_toks:
        return 0.0
    overlap = want_toks & cand_toks
    if not overlap:
        return 0.0  # hard gate: no shared significant title word

    coverage = len(overlap) / len(want_toks)
    seq = SequenceMatcher(None, want_norm, norm(product.get("title") or "")).ratio()
    result = 0.6 * coverage + 0.4 * seq

    author_match = bool(
        want_author
        and toks(want_author)
        & (names(product, "authors") | names(product, "narrators"))
    )
    # A candidate carrying extra significant words (a series prefix, a different
    # subtitle) is only a partial title hit - trust it only when the author or
    # narrator corroborates.
    if cand_toks - want_toks and not author_match:
        result *= 0.7
    if author_match:
        result = min(1.0, result + 0.15)
    return result


def write_chapters(asin: str, region: str, path: str) -> None:
    """Fetch the region's chapters from Audnex and write them as ffmetadata.

    m4b-merge needs this because its own Audnex client sends no region param and
    404s on any non-us ASIN. Best effort - a failure just leaves the merge step
    to derive chapters from the source file boundaries.
    """
    url = f"{AUDNEX}/books/{asin}/chapters?region={region}"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            chapters = json.load(resp).get("chapters") or []
    except Exception as exc:  # noqa: BLE001
        log(f"chapter fetch failed for {asin} ({region}): {exc}")
        return
    if len(chapters) < 2:
        log(f"audnex has {len(chapters)} chapters for {asin} - skipping chapter file")
        return
    lines = [";FFMETADATA1"]
    for chapter in chapters:
        start = int(chapter.get("startOffsetMs") or 0)
        end = start + int(chapter.get("lengthMs") or 0)
        title = re.sub(r"[\n=;#\\]", " ", (chapter.get("title") or "").strip())
        lines += [
            "",
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start}",
            f"END={end}",
            f"title={title or 'Chapter'}",
        ]
    try:
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        log(f"wrote {len(chapters)} chapters ({region}) -> {path}")
    except OSError as exc:
        log(f"could not write {path}: {exc}")


def fetch_book(asin: str, region: str) -> dict | None:
    """Fetch the region's book record from Audnex."""
    url = f"{AUDNEX}/books/{asin}?region={region}"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except Exception as exc:  # noqa: BLE001
        log(f"book fetch failed for {asin} ({region}): {exc}")
        return None


def _ffmeta_escape(value: str) -> str:
    """Backslash-escape the ffmetadata reserved characters."""
    return re.sub(r"([=;#\n\\])", r"\\\1", value or "")


def write_tags_ffmeta(path: str, title: str, authors: str, **extra: str) -> None:
    """Write FFMETADATA1 global tags the merge step embeds into the .m4b.

    beets should overwrite these; they exist so a book still carries a real
    title / author when beets falls back to an as-is import.
    """
    if not title:
        return
    tags = {
        "title": title,
        "album": title,
        "artist": authors,
        "album_artist": authors,
        "sort_album": title,
    }
    tags.update({k: v for k, v in extra.items() if v})
    lines = [";FFMETADATA1"]
    lines += [f"{k}={_ffmeta_escape(str(v))}" for k, v in tags.items() if v]
    try:
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        log(f"wrote global tags -> {path}")
    except OSError as exc:
        log(f"could not write {path}: {exc}")


def write_metadata_yml(book: dict, path: str) -> None:
    """Emit a beets-audible metadata.yml so beets tags without an Audible lookup.

    Every key get_album_from_yaml_metadata() reads is populated. Scalars and
    lists go through json.dumps (valid YAML flow syntax, handles quoting and
    newlines); releaseDate stays an unquoted date so PyYAML yields a date obj.
    """
    authors = [a.get("name", "") for a in book.get("authors") or [] if a.get("name")]
    narrators = [n.get("name", "") for n in book.get("narrators") or [] if n.get("name")]
    genres = [
        g.get("name", "")
        for g in book.get("genres") or []
        if g.get("name") and g.get("type") == "genre"
    ] or [g.get("name", "") for g in book.get("genres") or [] if g.get("name")]
    # YYYY-MM-DD from the ISO string; beets does release_date.year unconditionally
    release = (book.get("releaseDate") or "")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", release):
        release = "1970-01-01"
    description = book.get("summary") or book.get("description") or ""
    language = (book.get("language") or "english").capitalize()
    series = book.get("seriesPrimary") or {}

    def j(value) -> str:
        return json.dumps(value, ensure_ascii=False)

    lines = [
        f"title: {j(book.get('title') or '')}",
        f"authors: {j(authors)}",
        f"narrators: {j(narrators)}",
        f"description: {j(description)}",
        f"genres: {j(genres)}",
        f"publisher: {j(book.get('publisherName') or '')}",
        f"language: {j(language)}",
        f"releaseDate: {release}",
    ]
    if book.get("subtitle"):
        lines.append(f"subtitle: {j(book['subtitle'])}")
    if series.get("name"):
        lines.append(f"series: {j(series['name'])}")
        if series.get("position"):
            lines.append(f"seriesPosition: {j(str(series['position']))}")
    try:
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        log(f"wrote metadata.yml -> {path}")
    except OSError as exc:
        log(f"could not write {path}: {exc}")


def write_cover(url: str, path: str) -> None:
    """Best-effort cover download; a miss just means no cover.jpg in the folder."""
    if not url:
        return
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read()
        with open(path, "wb") as fh:
            fh.write(data)
        log(f"wrote cover ({len(data)} bytes) -> {path}")
    except Exception as exc:  # noqa: BLE001
        log(f"cover fetch failed from {url}: {exc}")


def emit(asin: str, region: str) -> None:
    with open(os.environ.get("ASIN_OUT", "/work/asin"), "w") as fh:
        fh.write(f"{asin} {region}")
    write_chapters(asin, region, os.environ.get("CHAPTERS_OUT", "/work/chapters.ffmeta"))

    book = fetch_book(asin, region)
    if not book:
        # No book record - fall back to whatever Shelfmark told us.
        write_tags_ffmeta(
            os.environ.get("TAGS_OUT", "/work/tags.ffmeta"),
            b64("TITLE_B64"),
            b64("AUTHOR_B64"),
        )
        return

    authors = ", ".join(a.get("name", "") for a in book.get("authors") or [] if a.get("name"))
    write_metadata_yml(book, os.environ.get("METADATA_OUT", "/work/metadata.yml"))
    write_cover(book.get("image") or "", os.environ.get("COVER_OUT", "/work/cover.jpg"))
    write_tags_ffmeta(
        os.environ.get("TAGS_OUT", "/work/tags.ffmeta"),
        book.get("title") or b64("TITLE_B64"),
        authors or b64("AUTHOR_B64"),
        date=(book.get("releaseDate") or "")[:4],
        genre=next(
            (g.get("name", "") for g in book.get("genres") or [] if g.get("type") == "genre"),
            "",
        ),
    )


def emit_fallback_tags() -> None:
    """No confident ASIN: still give the merge step a real title / author."""
    title = b64("TITLE_B64")
    if not title:
        return
    write_tags_ffmeta(
        os.environ.get("TAGS_OUT", "/work/tags.ffmeta"), title, b64("AUTHOR_B64")
    )


def main() -> None:
    min_score = float(os.environ.get("MIN_SCORE", "0.75"))
    title = b64("TITLE_B64")
    author = b64("AUTHOR_B64")
    source = b64("SOURCE_PATH_B64") or os.environ.get("SOURCE_PATH", "")

    embedded = asin_from_source(source) if source else None
    if embedded:
        log(f"using ASIN from source name: {embedded}")
        emit(embedded, "de")
        return

    if not title:
        log("no title provided - cannot search")
        return

    want_toks = toks(title)
    want_norm = norm(title)
    if not want_toks:
        log(f"title {title!r} has no significant tokens - cannot match safely")
        return

    # Most precise filter first, keyword mash only as a last resort.
    queries: list[dict] = []
    if author:
        queries.append({"title": title, "author": author})
    queries.append({"title": title})
    queries.append({"keywords": f"{title} {author}".strip()})

    best = (0.0, None, None)
    for region, base_url in REGIONS:
        seen: set[str] = set()
        for params in queries:
            try:
                products = search(base_url, params)
            except Exception as exc:  # noqa: BLE001
                log(f"search failed on {region} {params}: {exc}")
                continue
            for product in products:
                asin = product.get("asin")
                if not asin or asin in seen:
                    continue
                seen.add(asin)
                ratio = score(product, want_toks, want_norm, author)
                log(f"  {region} {asin} {ratio:.2f} {product.get('title')!r}")
                if ratio > best[0]:
                    best = (ratio, asin, region)
            if best[0] >= min_score:
                break
        if best[0] >= min_score:
            break

    ratio, asin, region = best
    if asin and ratio >= min_score:
        log(f"selected {asin} ({region}) score={ratio:.2f}")
        emit(asin, region)
    else:
        log(f"no confident match (best score={ratio:.2f}) - continuing without ASIN")
        emit_fallback_tags()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        log(f"unexpected error: {exc}")
    sys.exit(0)
