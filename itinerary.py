"""Final itinerary + where-to-stay recommendation as schema-valid JSON, written by Qwen3.8 27B on SIE.

    build_itinerary(trip, users, plan) -> dict   (always returns the schema; template fallback without a key)

Schema:
    {"summary": str,
     "stay_recommendation": {"option_id": str, "why": str},
     "days": [{"date": str, "title": str, "items": [{"time": str, "what": str, "option_id": str|null}]}],
     "notes": [str]}
The model may only reference option ids that are in the plan; anything else is rejected and we use the template.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import sie

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "stay_recommendation", "days", "notes"],
    "properties": {
        "summary": {"type": "string"},
        "stay_recommendation": {"type": "object", "additionalProperties": False, "required": ["option_id", "why"],
                                "properties": {"option_id": {"type": "string"}, "why": {"type": "string"}}},
        "days": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["date", "title", "items"],
            "properties": {"date": {"type": "string"}, "title": {"type": "string"}, "items": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["time", "what", "option_id"],
                "properties": {"time": {"type": "string"}, "what": {"type": "string"},
                               "option_id": {"type": ["string", "null"]}}}}}}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
}


def _days(trip: dict) -> list[str]:
    try:
        d0, d1 = date.fromisoformat(trip["start"]), date.fromisoformat(trip["end"])
        return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
    except (KeyError, ValueError):
        return [trip.get("start", "day 1")]


def _template(trip: dict, plan: dict) -> dict:
    chosen = [o for o in plan.values() if o]
    stay = next((o for o in chosen if o["type"] == "stay"), None)
    flight = next((o for o in chosen if o["type"] == "flight"), None)
    acts = [o for o in chosen if o["type"] == "activity"]
    days = _days(trip)
    out = [{"date": d, "title": f"Day {i + 1}", "items": []} for i, d in enumerate(days)]
    if flight:
        out[0]["items"].append({"time": "arrival", "what": flight["title"], "option_id": flight["id"]})
    for i, a in enumerate(acts):
        out[min(i + (1 if len(days) > 1 else 0), len(out) - 1)]["items"].append(
            {"time": "day", "what": a["title"], "option_id": a["id"]})
    return {"summary": f"{len(days)} days in {trip.get('destination', 'your destination')}.",
            "stay_recommendation": {"option_id": stay["id"] if stay else "", "why": "Chosen by the group's agents."},
            "days": out, "notes": []}


def _valid(result: dict, plan: dict) -> bool:
    ids = {o["id"] for o in plan.values() if o}
    try:
        stay = result["stay_recommendation"]["option_id"]
        refs = [i["option_id"] for d in result["days"] for i in d["items"]]
        return (isinstance(result["summary"], str) and isinstance(result["notes"], list)
                and stay in ids | {""} and all(r is None or r in ids for r in refs))
    except (KeyError, TypeError):
        return False


def _ask(messages: list[dict], use_schema: bool) -> dict:
    rf = {"type": "json_schema", "json_schema": {"name": "itinerary", "strict": True, "schema": SCHEMA}} if use_schema else None
    text = sie.chat(messages, model=sie.WRITER_MODEL, response_format=rf, max_tokens=2500).get("content") or ""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)


def build_itinerary(trip: dict, users: list[dict], plan: dict) -> dict:
    if not sie.available():
        return _template(trip, plan)
    chosen = [{"option_id": o["id"], "slot": s, "type": o["type"], "title": o["title"], "price": o["price"],
               "tags": o.get("tags", [])} for s, o in plan.items() if o]
    travelers = [{"name": u["name"], "budget_per_night": u.get("budget_per_night"),
                  "dealbreakers": u["answers"].get("dealbreakers", []), "wants": u["answers"].get("activities", [])}
                 for u in users]
    messages = [
        {"role": "system", "content": "You write trip itineraries. Reply with JSON only, matching this JSON Schema: "
         + json.dumps(SCHEMA) + " Use only option_ids from the approved plan; never invent bookings. "
         "One entry in days per date given. stay_recommendation explains why the chosen stay fits this group."},
        {"role": "user", "content": json.dumps({"trip": trip, "dates": _days(trip), "travelers": travelers,
                                                "approved_plan": chosen})}]
    for use_schema in (True, False):  # some models reject response_format: retry with the prompt-only schema
        try:
            result = _ask(messages, use_schema)
            if _valid(result, plan):
                return result
        except Exception as exc:  # noqa: BLE001
            print(f"[itinerary] Qwen call failed ({type(exc).__name__}: {exc})", flush=True)
    return _template(trip, plan)
