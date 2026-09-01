#!/usr/bin/env python3
"""Resolve an Audible ASIN for a downloaded audiobook.

Inputs (env):
  TITLE_B64, AUTHOR_B64   - base64 of the Shelfmark-provided title / author
  SOURCE_PATH_B64         - base64 of the downloaded file or folder path
  ASIN_OUT               - file to write the result to (default /work/asin)
  CHAPTERS_OUT          - ffmetadata chapter file to write (default /work/chapters.ffmeta)
  MIN_SCORE             - minimum match score to accept (default 0.75)

Output: writes "<ASIN> <region>" to ASIN_OUT when a confident match is found,
otherwise writes nothing. On a match it also fetches that region's chapters from
Audnex and writes them to CHAPTERS_OUT in ffmetadata form - m4b-merge's own
Audnex client sends no region and 404s on any non-us ASIN, so the merge step
embeds this file instead. Always exits 0 - a missing ASIN just means the merge
runs without Audible enrichment, which is far better than the wrong book.

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


def emit(asin: str, region: str) -> None:
    with open(os.environ.get("ASIN_OUT", "/work/asin"), "w") as fh:
        fh.write(f"{asin} {region}")
    write_chapters(asin, region, os.environ.get("CHAPTERS_OUT", "/work/chapters.ffmeta"))


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


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        log(f"unexpected error: {exc}")
    sys.exit(0)
