#!/usr/bin/env python3
"""Resolve an Audible ASIN for a downloaded audiobook.

Inputs (env):
  TITLE_B64, AUTHOR_B64   - base64 of the Shelfmark-provided title / author
  SOURCE_PATH_B64         - base64 of the downloaded file or folder path
  ASIN_OUT               - file to write the result to (default /work/asin)
  MIN_RATIO              - minimum title similarity to accept (default 0.60)

Output: writes "<ASIN> <region>" to ASIN_OUT when a confident match is found,
otherwise writes nothing. Always exits 0 - a missing ASIN just means m4b-merge
runs without Audible enrichment.
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


def log(msg: str) -> None:
    print(f"[asin-lookup] {msg}", file=sys.stderr, flush=True)


def b64(name: str) -> str:
    try:
        return base64.b64decode(os.environ.get(name, "")).decode().strip()
    except Exception:  # noqa: BLE001
        return ""


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


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


def search(base_url: str, query: str) -> list[dict]:
    url = (
        f"{base_url}/1.0/catalog/products?"
        + urllib.parse.urlencode(
            {
                "keywords": query,
                "num_results": "10",
                "products_sort_by": "Relevance",
                "response_groups": "contributors,product_attrs,product_desc,series",
            }
        )
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.load(resp)
    return data.get("products", []) or []


def score(product: dict, want_title: str, want_author: str) -> float:
    title = norm(product.get("title") or "")
    ratio = SequenceMatcher(None, norm(want_title), title).ratio()
    if want_author:
        authors = norm(" ".join(a.get("name", "") for a in product.get("authors", []) or []))
        if authors and want_author.split()[-1].lower() in authors:
            ratio = min(1.0, ratio + 0.1)
    return ratio


def main() -> None:
    out = os.environ.get("ASIN_OUT", "/work/asin")
    min_ratio = float(os.environ.get("MIN_RATIO", "0.60"))
    title = b64("TITLE_B64")
    author = b64("AUTHOR_B64")
    source = b64("SOURCE_PATH_B64") or os.environ.get("SOURCE_PATH", "")

    embedded = asin_from_source(source) if source else None
    if embedded:
        log(f"using ASIN from source name: {embedded}")
        with open(out, "w") as fh:
            fh.write(f"{embedded} de")
        return

    if not title:
        log("no title provided - cannot search")
        return

    query = f"{author} {title}".strip()
    best = (0.0, None, None)
    for region, base_url in REGIONS:
        try:
            products = search(base_url, query)
        except Exception as exc:  # noqa: BLE001
            log(f"search failed on {region}: {exc}")
            continue
        for product in products:
            asin = product.get("asin")
            if not asin:
                continue
            ratio = score(product, title, author)
            log(f"  {region} {asin} {ratio:.2f} {product.get('title')!r}")
            if ratio > best[0]:
                best = (ratio, asin, region)
        if best[0] >= min_ratio:
            break

    ratio, asin, region = best
    if asin and ratio >= min_ratio:
        log(f"selected {asin} ({region}) ratio={ratio:.2f}")
        with open(out, "w") as fh:
            fh.write(f"{asin} {region}")
    else:
        log(f"no confident match (best ratio={ratio:.2f}) - continuing without ASIN")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        log(f"unexpected error: {exc}")
    sys.exit(0)
