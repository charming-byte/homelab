#!/usr/bin/env python3
"""Shelfmark custom script: notify the audiobook-m4b webhook after a download.

Shelfmark invokes this as:  notify-webhook.py "<target_path>"
with the JSON task payload on stdin (CUSTOM_SCRIPT_JSON_PAYLOAD=true).

It performs a cheap, fast HTTP POST to the webhook which in turn creates a
Kubernetes Job that does the actual (long-running) m4b conversion. Any error is
logged and swallowed - this script must never exit non-zero, otherwise Shelfmark
marks the download as failed.
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import urllib.request

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus", ".aac", ".wma"}
CONVERTIBLE_EXTS = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma"}
TIMEOUT = 10


def log(msg: str) -> None:
    print(f"[notify-webhook] {msg}", file=sys.stderr, flush=True)


def read_payload() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception as exc:  # noqa: BLE001 - best effort
        log(f"could not parse stdin payload: {exc}")
        return {}


def should_skip(target: str, payload: dict) -> str | None:
    """Return a reason string if we should NOT trigger a conversion, else None."""
    task = payload.get("task", {}) if isinstance(payload, dict) else {}
    content_type = (task.get("content_type") or "").lower()
    fmt = (task.get("format") or "").lower().lstrip(".")

    if content_type and content_type != "audiobook":
        if fmt and f".{fmt}" not in AUDIO_EXTS:
            return f"not an audiobook (content_type={content_type!r}, format={fmt!r})"

    if os.path.isfile(target):
        if os.path.splitext(target)[1].lower() == ".m4b":
            return "source is already a single .m4b file"
        return None

    if os.path.isdir(target):
        has_m4b = has_convertible = False
        for root, _dirs, files in os.walk(target):
            for name in files:
                ext = os.path.splitext(name)[1].lower()
                if ext == ".m4b":
                    has_m4b = True
                elif ext in CONVERTIBLE_EXTS:
                    has_convertible = True
        if has_m4b and not has_convertible:
            return "source directory already contains .m4b and no convertible audio"
        if not has_convertible:
            return "source directory contains no convertible audio files"
        return None

    return f"target path does not exist: {target}"


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1]:
        log("no target path argument given")
        return

    target = sys.argv[1]
    payload = read_payload()

    reason = should_skip(target, payload)
    if reason:
        log(f"skipping: {reason}")
        return

    url = os.environ.get("AUDIOBOOK_WEBHOOK_URL")
    secret = os.environ.get("SHELFMARK_WEBHOOK_SECRET")
    if not url or not secret:
        log("AUDIOBOOK_WEBHOOK_URL or SHELFMARK_WEBHOOK_SECRET not set - skipping")
        return

    task = payload.get("task", {}) if isinstance(payload, dict) else {}

    def b64(value: str) -> str:
        return base64.b64encode((value or "").encode()).decode()

    # Everything is base64 so the webhook can splice it into a Job manifest
    # without any shell/YAML injection risk from odd book titles or paths.
    body = json.dumps(
        {
            "source_path": b64(target),
            "title": b64(task.get("title")),
            "author": b64(task.get("author")),
        },
        separators=(",", ":"),
    ).encode()

    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Signature": f"sha256={signature}",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            log(f"webhook responded {resp.status} for {target}")
    except Exception as exc:  # noqa: BLE001 - never fail the download
        log(f"webhook call failed for {target}: {exc}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        log(f"unexpected error: {exc}")
    sys.exit(0)
