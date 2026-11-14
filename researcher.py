"""Researcher: live data only. Owner: P4.

    research(trip, request) -> list[Option]
    request = {type, query, max_price, avoid_tags, tag?, origin?, destination?}

Flights:  Google Flights round-trip fares for the trip's dates (fast-flights, no key), tagged
          nonstop/layover, red-eye and morning/afternoon/evening from the real departure time.
          Airports are resolved from the traveler's home city and the trip destination (Qwen, validated
          as 3-letter codes); FLIGHT_FROM / FLIGHT_TO override them.
Stays and activities: Keenable web search (KEENABLE_API_KEY); Qwen3.8 27B reads the snippets into Options
          (extract_options.py: every price must be quoted from the page, urls come from the results).

There is no cached or invented data. If a service fails, the error is raised (ResearchError) so the
caller can show it.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re

import extract_options
import keenable
import sie

MAX_RESULTS = 10


class ResearchError(RuntimeError):
    pass


ACTIVITY_SEARCH = {  # what to search for each questionnaire activity / food tag
    "beach": "beach day chair umbrella rental",
    "snorkel": "snorkeling tour",
    "airboat": "airboat tour",
    "fine-dining": "fine dining restaurant tasting menu",
    "sightseeing": "top sightseeing tour or boat cruise",  # the destination's own highlight, for everyone
}


# ---------------------------------------------------------------- stays and activities (Keenable + Qwen)
def _search(query: str) -> list[dict]:
    try:
        return keenable.search(query, max_results=MAX_RESULTS, snippet_max_length=600)
    except keenable.KeenableError as exc:
        raise ResearchError(f"Keenable search failed for “{query}”: {exc}") from None


def _web(trip: dict, request: dict) -> list[dict]:
    typ, dest = request["type"], request.get("destination") or trip["destination"]
    cap = f" under ${request['max_price']:.0f}" if request.get("max_price") else ""
    tag = request.get("tag") or request["query"]
    if typ == "stay":  # phrasings that surface pages naming a specific hotel and its rate
        queries = [f"{dest} hotel names with price per night from ${cap}", f"{dest} boutique hotel nightly rate from ${cap}"]
    else:
        what = ACTIVITY_SEARCH.get(tag, tag.replace("-", " "))
        queries = [f"{what} {dest} price per person{cap}", f"{dest} {what} tickets or menu from ${cap}"]
    hits: dict[str, dict] = {}
    for q in queries:
        for h in _search(q):
            hits.setdefault(h["url"], h)
    try:
        return extract_options.extract(list(hits.values()), typ, request.get("tag"), queries[0])
    except Exception as exc:  # noqa: BLE001
        raise ResearchError(f"Could not read the search results for “{queries[0]}”: {exc}") from None


# ---------------------------------------------------------------- flights (Google Flights)
@functools.lru_cache(maxsize=64)
def _airport(place: str) -> str:
    """3-letter IATA code of the main airport for a city or area name."""
    if re.fullmatch(r"[A-Za-z]{3}", place.strip()):
        return place.strip().upper()
    try:
        msg = sie.chat([{"role": "user", "content": f"Main commercial airport nearest to “{place}”. "
                         'Reply with JSON only: {"iata": "XXX"}'}], model=sie.WRITER_MODEL, max_tokens=40, temperature=0.0)
        code = str(json.loads(re.search(r"\{.*\}", msg.get("content") or "", re.S).group(0))["iata"]).upper()
    except Exception as exc:  # noqa: BLE001
        raise ResearchError(f"Could not resolve an airport for “{place}”: {exc}") from None
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ResearchError(f"Could not resolve an airport for “{place}” (got {code!r})")
    return code


def _flight_tags(dep_hour: int, stops: int) -> list[str]:
    tags = ["nonstop"] if stops == 0 else ["layover"]
    if dep_hour >= 21 or dep_hour < 5:
        tags.append("red-eye")
    tags.append("morning" if 5 <= dep_hour < 12 else "afternoon" if 12 <= dep_hour < 18 else "evening")
    return tags


def _flights(trip: dict, request: dict) -> list[dict]:
    try:
        import fast_flights as ff
    except ImportError:
        raise ResearchError("fast-flights is not installed: pip install -r requirements.txt") from None
    origin = os.getenv("FLIGHT_FROM") or _airport(request.get("origin") or "")
    dest = os.getenv("FLIGHT_TO") or _airport(request.get("destination") or trip["destination"])
    try:
        q = ff.create_query(
            flights=[ff.FlightQuery(date=trip["start"], from_airport=origin, to_airport=dest),
                     ff.FlightQuery(date=trip["end"], from_airport=dest, to_airport=origin)],
            trip="round-trip", passengers=ff.Passengers(adults=1), currency="USD", language="en-US")
        results = ff.get_flights(q)
    except Exception as exc:  # noqa: BLE001
        raise ResearchError(f"Google Flights failed for {origin} to {dest}: {exc}") from None
    options = []
    for r in results:
        legs = r.flights
        h, m = legs[0].departure.time
        stops = len(legs) - 1
        route = "nonstop" if stops == 0 else f"{stops}-stop via {', '.join(l.to_airport.code for l in legs[:-1])}"
        options.append({
            "id": f"flight_{hashlib.sha1(f'{r.airlines}{h}{m}{r.price}'.encode()).hexdigest()[:8]}",
            "type": "flight",
            "title": f"{', '.join(r.airlines)} {origin} to {dest} {route}, {h % 12 or 12}:{m:02d}{'am' if h < 12 else 'pm'}",
            "price": r.price,
            "url": f"https://www.google.com/travel/flights?q=Flights%20from%20{origin}%20to%20{dest}%20on%20{trip['start']}%20through%20{trip['end']}",
            "tags": _flight_tags(h, stops),
        })
    return options


# ---------------------------------------------------------------- contract
def research(trip: dict, request: dict) -> list[dict]:
    options = _flights(trip, request) if request["type"] == "flight" else _web(trip, request)
    avoid, max_price = set(request.get("avoid_tags") or []), request.get("max_price")
    return [o for o in options if not (avoid & set(o["tags"])) and (max_price is None or o["price"] <= max_price)]
