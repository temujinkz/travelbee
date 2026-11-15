"""Fix typos in place names: "Pariss" -> "Paris", "new yrok" -> "New York". Owner: P1/P3.

Qwen (Superlinked SIE) answers with the canonical English city name; anything that fails (no key, timeout,
not a place) keeps what the person typed, so it can never block the form.
"""
from __future__ import annotations

import json

_cache: dict[str, str] = {}

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["name", "is_place"],
          "properties": {"name": {"type": "string"}, "is_place": {"type": "boolean"}}}


def normalize(text: str) -> str:
    raw = " ".join((text or "").split())
    if len(raw) < 2:
        return raw
    if raw.lower() in _cache:
        return _cache[raw.lower()]
    fixed = raw
    try:
        import sie

        if sie.available():
            msg = sie.chat(
                [{"role": "system", "content": "The user typed a city or travel destination, maybe with typos or "
                  "odd casing. Return its usual English name (e.g. 'pariss' -> 'Paris', 'new yrok' -> 'New York', "
                  "'rio de janiero' -> 'Rio de Janeiro', 'sf' -> 'San Francisco'). Keep a region or country if one "
                  "was given. is_place is false if it is not a real place."},
                 {"role": "user", "content": raw}],
                model=sie.WRITER_MODEL, max_tokens=60, temperature=0.0,
                response_format={"type": "json_schema", "json_schema": {"name": "place", "strict": True, "schema": SCHEMA}})
            out = json.loads((msg.get("content") or "").strip().strip("`").removeprefix("json").strip())
            name = " ".join(str(out.get("name", "")).split())[:80]
            if out.get("is_place") and 2 <= len(name) <= 80:
                fixed = name
    except Exception as exc:  # keep what was typed
        print(f"[places] kept {raw!r}: {exc}", flush=True)
    _cache[raw.lower()] = fixed
    return fixed
