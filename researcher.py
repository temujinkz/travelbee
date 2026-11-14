"""Researcher: real web results via Keenable. Owner: P4.

    research(trip, request) -> list[Option]      request = {type, query, max_price, avoid_tags}

Stays and activities: Keenable web search (https://docs.keenable.ai). One Option per result: title, cheapest
dollar price in the snippet, url, and shared tags detected in the text (beach, hostel, snorkel, keys, ...).
Flights: real round-trip Google Flights fares for the trip's dates via fast-flights (no key), tagged
nonstop/layover, red-eye and morning/afternoon/evening from the actual departure time.
Every live result is saved to demo/cached_options.json and replayed if Keenable is down (demo safety net).

KEENABLE_API_KEY in .env uses the keyed API (100k requests/month free); without it we use the public
endpoint (1,000 requests/hour per IP).
    python researcher.py     run the demo searches and print what comes back
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import sys
import urllib.request
from pathlib import Path

import protocol  # noqa: F401  (loads .env)

CACHE = Path(__file__).resolve().parent / "demo" / "cached_options.json"
MAX_RESULTS = 8

# shared tag list (TEAM_SPLIT.md) + what the Advocates check; detected from title + snippet text
TAG_WORDS = {
    "beach": ["beach", "oceanfront", "beachfront", "ocean view"],
    "hostel": ["hostel", "dorm"],
    "hotel": ["hotel", "resort", "inn"],
    "rental": ["rental", "airbnb", "vrbo", "apartment", "condo"],
    "snorkel": ["snorkel"],
    "airboat": ["airboat"],
    "everglades": ["everglades"],
    "keys": ["key largo", "key west", "islamorada", "marathon", "florida keys", "pennekamp"],
    "fine-dining": ["fine dining", "tasting menu", "michelin", "chef's"],
    "seafood": ["seafood"],
}
LONG_DRIVE_PLACES = ["key west", "marathon", "islamorada", "key largo", "pennekamp"]  # 1h15+ from Miami


def _ssl():
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _search(query: str) -> list[dict]:
    key = os.getenv("KEENABLE_API_KEY")
    url = "https://api.keenable.ai/v1/search" + ("" if key else "/public")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    else:
        headers["X-Keenable-Title"] = "Travelbee"
    body = json.dumps({"query": query, "max_results": MAX_RESULTS}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, context=_ssl(), timeout=20) as resp:
        return json.load(resp).get("results", [])


def _price(text: str) -> float | None:
    """Best guess at the price: "from $39" or "$120 per night" first, else the first plausible amount."""
    def ok(values):
        values = [float(v.replace(",", "")) for v in values]
        return [v for v in values if 25 <= v <= 3000]

    for pattern in (r"from \$\s?(\d[\d,]*)", r"\$\s?(\d[\d,]*)\s*(?:/|per|a)\s*(?:night|person|adult|pp)"):
        found = ok(re.findall(pattern, text, flags=re.I))
        if found:
            return min(found)
    found = ok(re.findall(r"\$\s?(\d[\d,]*)", text))
    return found[0] if found else None


def _has(low: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}s?\b", low) is not None


def _tags(text: str, typ: str) -> list[str]:
    low = text.lower()
    tags = [tag for tag, words in TAG_WORDS.items() if any(_has(low, w) for w in words)]
    if typ == "activity" and any(_has(low, p) for p in LONG_DRIVE_PLACES):
        tags.append("long-drive")
    return tags


def _query_tags(query: str) -> list[str]:
    """The slot's own tag (e.g. 'snorkel') so the Coordinator files the result under the right slot."""
    return [tag for tag, words in TAG_WORDS.items() if tag in query.lower() or any(w in query.lower() for w in words)]


LIST_PAGE = re.compile(r"\b(best|top|guide|\d+ (restaurants|hotels|things|places)|near me|deals in|reviews of"
                       r"|hotels in|stays in|search results|book your hotel|cheap hotels|vacation rentals)\b", re.I)

# Short, specific searches per slot work much better than the Coordinator's long trip description.
SLOT_QUERIES = {
    "stay": "hotel Miami Beach price per night",
    "snorkel": "snorkel tour Key Largo price per person",
    "airboat": "Everglades airboat tour price per person",
    "fine-dining": "fine dining restaurant Miami Beach tasting menu price",
    "beach": "South Beach Miami beach chair umbrella rental price",
}


def _slot_query(typ: str, query: str) -> str:
    if typ == "stay":
        return SLOT_QUERIES["stay"]
    for tag in _query_tags(query):
        if tag in SLOT_QUERIES:
            return SLOT_QUERIES[tag]
    return query


def _to_option(result: dict, typ: str, query: str) -> dict | None:
    if LIST_PAGE.search(result.get("title", "")):
        return None  # "10 best hotels" pages aren't something the group can book
    text = f"{result.get('title', '')} {result.get('snippet', '')}"
    price = _price(text)
    if price is None:
        return None
    title = re.sub(r"\s*[|·-]\s*(Booking\.com|KAYAK|Tripadvisor|Expedia|Viator).*$", "", result.get("title", "")).strip()
    tags = sorted(set(_tags(text, typ)) | (set(_query_tags(query)) if typ == "activity" else set()))
    return {
        "id": f"{typ}_{hashlib.sha1(result['url'].encode()).hexdigest()[:8]}",
        "type": typ,
        "title": title[:80] or result["url"],
        "price": round(price),
        "url": result["url"],
        "tags": tags,
    }


def _load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(query: str, options: list[dict]) -> None:
    cache = _load_cache()
    cache[query] = options
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))


def _live_or_cached(typ: str, query: str) -> list[dict]:
    if os.getenv("RESEARCH_MODE") == "cache":  # demo safety: replay saved real results, no network
        return _load_cache().get(query, [])
    try:
        options = [o for r in _search(query) if (o := _to_option(r, typ, query))]
        if options:
            _save_cache(query, options)
            return options
    except Exception as exc:
        print(f"[researcher] Keenable failed ({exc}); using cached results", file=sys.stderr, flush=True)
    return _load_cache().get(query, [])


def _flight_tags(dep_hour: int, stops: int) -> list[str]:
    tags = ["nonstop"] if stops == 0 else ["layover"]
    if dep_hour >= 21 or dep_hour < 5:
        tags.append("red-eye")
    tags.append("morning" if 5 <= dep_hour < 12 else "afternoon" if 12 <= dep_hour < 18 else "evening")
    return tags


def _google_flights(trip: dict) -> list[dict]:
    """Real round-trip fares from Google Flights for the trip's exact dates (fast-flights, no key)."""
    import fast_flights as ff

    origin, dest = os.getenv("FLIGHT_FROM", "SFO"), os.getenv("FLIGHT_TO", "MIA")
    q = ff.create_query(
        flights=[ff.FlightQuery(date=trip["start"], from_airport=origin, to_airport=dest),
                 ff.FlightQuery(date=trip["end"], from_airport=dest, to_airport=origin)],
        trip="round-trip", passengers=ff.Passengers(adults=1), currency="USD", language="en-US")
    options = []
    for r in ff.get_flights(q):
        legs = r.flights
        h, m = legs[0].departure.time
        stops = len(legs) - 1
        route = "nonstop" if stops == 0 else f"{stops}-stop via {', '.join(l.to_airport.code for l in legs[:-1])}"
        options.append({
            "id": f"flight_{hashlib.sha1(f'{r.airlines}{h}{m}{r.price}'.encode()).hexdigest()[:8]}",
            "type": "flight",
            "title": f"{', '.join(r.airlines)} {route}, {h % 12 or 12}:{m:02d}{'am' if h < 12 else 'pm'}",
            "price": r.price,
            "url": f"https://www.google.com/travel/flights?q=Flights%20from%20{origin}%20to%20{dest}%20on%20{trip['start']}%20through%20{trip['end']}",
            "tags": _flight_tags(h, stops),
        })
    return options


def _flights(trip: dict, request: dict) -> list[dict]:
    key = f"flights {trip['start']} {trip['end']}"
    if os.getenv("RESEARCH_MODE") != "cache":
        try:
            options = _google_flights(trip)
            if options:
                _save_cache(key, options)
        except Exception as exc:
            print(f"[researcher] Google Flights failed ({exc}); using cached flights", file=sys.stderr, flush=True)
    options = _load_cache().get(key)
    if not options:
        import mocks

        return mocks.research(trip, request)
    avoid = set(request.get("avoid_tags") or [])
    return [o for o in options if not avoid & set(o["tags"])]


def research(trip: dict, request: dict) -> list[dict]:
    typ = request["type"]
    if typ == "flight":
        return _flights(trip, request)
    query = _slot_query(typ, request["query"])
    if request.get("max_price"):
        query += f" under ${request['max_price']:.0f}"
    options = _live_or_cached(typ, query)
    avoid = set(request.get("avoid_tags") or [])
    max_price = request.get("max_price")
    return [o for o in options
            if not (avoid & set(o["tags"])) and (max_price is None or o["price"] <= max_price)]


if __name__ == "__main__":
    import mocks

    trip = mocks.TRIP
    for req in [
        {"type": "stay", "query": "stay for 4 people in Miami Beach", "max_price": None, "avoid_tags": []},
        {"type": "stay", "query": "stay for 4 people in Miami Beach", "max_price": 90, "avoid_tags": ["hostel"]},
        {"type": "activity", "query": "snorkel near Miami Florida Keys", "max_price": None, "avoid_tags": []},
        {"type": "activity", "query": "airboat near Miami Everglades", "max_price": None, "avoid_tags": []},
        {"type": "activity", "query": "fine dining near Miami", "max_price": None, "avoid_tags": []},
    ]:
        print(f"\n== {req['type']}: {req['query']} (max {req['max_price']}, avoid {req['avoid_tags']})")
        for o in research(trip, req):
            print(f"  ${o['price']:>5}  {o['title'][:60]:60s} {o['tags']}")
