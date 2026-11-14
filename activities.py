"""Activities that fit a destination, for the questionnaire. Owner: P1/P3.

    for_destination("Paris") -> [["seine-river-cruise", "Seine river cruise"], ["louvre-museum", "Louvre museum"], ...]

Qwen (Superlinked SIE) proposes 10-12 activities people actually book there; each becomes a slot tag
("activity:<slug>") that the Researcher searches for at that destination. If Qwen is unavailable, a generic
list that works anywhere is used, so the questionnaire always has choices.
"""
from __future__ import annotations

import json
import re

GENERIC = [
    ["museums", "Museums"], ["city-walking-tour", "City walking tour"], ["food-tour", "Food tour"],
    ["boat-cruise", "Boat cruise"], ["beach", "Beach"], ["hiking", "Hiking"], ["nightlife", "Nightlife"],
    ["local-show", "Local show"], ["day-trip", "Day trip"], ["shopping", "Shopping"],
]

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["activities"], "properties": {
    "activities": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                              "required": ["label"], "properties": {"label": {"type": "string"}}}}}}


def slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40]


def for_destination(destination: str) -> list[list[str]]:
    if not (destination or "").strip():
        return [list(a) for a in GENERIC]
    try:
        import sie

        if not sie.available():
            raise RuntimeError("SUPERLINKED_API_KEY not set")
        msg = sie.chat(
            [{"role": "system", "content": "List 10 to 12 distinct activities travelers commonly book at the given "
              "destination: iconic sights, tours, outdoor and food experiences specific to that place. Each label is "
              "2-5 words in English, searchable on booking sites (e.g. 'Seine river cruise', 'Louvre museum', "
              "'Helicopter tour', 'Sugarloaf cable car'). No generic filler, no duplicates."},
             {"role": "user", "content": f"Destination: {destination}"}],
            model=sie.WRITER_MODEL, max_tokens=600, temperature=0.2,
            response_format={"type": "json_schema", "json_schema": {"name": "activities", "strict": True, "schema": SCHEMA}})
        text = (msg.get("content") or "").strip().strip("`").removeprefix("json").strip()
        items, seen = [], set()
        for a in json.loads(text).get("activities", []):
            label = " ".join(str(a.get("label", "")).split())[:40]
            s = slug(label)
            if label and s and s not in seen:
                seen.add(s)
                items.append([s, label])
        if len(items) >= 4:
            return items[:12]
        raise ValueError(f"only {len(items)} activities")
    except Exception as exc:  # never leave the questionnaire without choices
        print(f"[activities] using the generic list for {destination!r}: {exc}", flush=True)
        return [list(a) for a in GENERIC]
