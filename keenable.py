"""Keenable web search (https://docs.keenable.ai/). Stdlib only.

    KEENABLE_API_KEY   required (sent as X-API-Key); without it available() is False
    KEENABLE_DISABLE=1 force off

    search(query, max_results=5) -> [{title, url, snippet}]
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE_URL = "https://api.keenable.ai"


class KeenableError(RuntimeError):
    pass


def available() -> bool:
    return bool(os.getenv("KEENABLE_API_KEY")) and os.getenv("KEENABLE_DISABLE") != "1"


def search(query: str, max_results: int = 5, snippet_max_length: int = 400, **filters) -> list[dict]:
    """filters: site, exclude_domains, published_after, mode ("pro" | "realtime"), ..."""
    key = os.getenv("KEENABLE_API_KEY")
    if not key:
        raise KeenableError("KEENABLE_API_KEY is not set")
    body = {"query": query, "max_results": max(1, min(int(max_results), 50)),
            "snippet_max_length": max(180, snippet_max_length), **filters}
    req = urllib.request.Request(BASE_URL + "/v1/search", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "X-API-Key": key}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=float(os.getenv("KEENABLE_TIMEOUT_S") or 15)) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read())
            raise KeenableError(f"{exc.code} {detail.get('code', '')}: {detail.get('message', exc.reason)}") from None
        except (ValueError, AttributeError):
            raise KeenableError(f"HTTP {exc.code} {exc.reason}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise KeenableError(str(exc)) from None
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("snippet", "")}
            for r in data.get("results", [])]
