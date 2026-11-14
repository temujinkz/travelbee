"""Stand-ins for P2 (advocate.py, memory.py) and P4 (researcher.py) so the Coordinator runs on its own.
coordinator.py switches to the real module as soon as that file exists. Delete this once everything is real.

Demo data uses the frozen formats. The conflicts are deliberate:
  cheapest flight is a red-eye (Léa's dealbreaker), cheap stay is far from the beach (Ben's dealbreaker),
  beach stays are over Léa's and Chloé's budgets, every snorkel trip is a long drive (Ben's dealbreaker),
  and a cheap beach rental only shows up on a second, constrained search.
"""
from __future__ import annotations

from protocol import Message

TRIP = {"id": "miami", "destination": "Miami + Everglades + Florida Keys",
        "start": "2026-12-10", "end": "2026-12-15", "members": ["lea", "ben", "chloe", "diego"]}

USERS = [
    {"id": "lea", "name": "Léa", "budget_per_night": 100, "home_city": "San Francisco",
     "answers": {"pace": "relaxed", "budget": "tight", "food": "local", "activities": ["airboat"],
                 "lodging": "any", "dealbreakers": ["red-eye"]}},
    {"id": "ben", "name": "Ben", "budget_per_night": 150, "home_city": "San Francisco",
     "answers": {"pace": "relaxed", "budget": "medium", "food": "seafood", "activities": ["airboat"],
                 "lodging": "beach", "dealbreakers": ["long-drive", "far-from-beach"]}},
    {"id": "chloe", "name": "Chloé", "budget_per_night": 90, "home_city": "Oakland",
     "answers": {"pace": "packed", "budget": "tight", "food": "any", "activities": ["snorkel"],
                 "lodging": "any", "dealbreakers": []}},
    {"id": "diego", "name": "Diego", "budget_per_night": 250, "home_city": "San Francisco",
     "answers": {"pace": "morning", "budget": "flexible", "food": "tasting-menu", "activities": ["fine-dining"],
                 "lodging": "hotel", "dealbreakers": ["hostel"]}},
]

# Option: {id, type, title, price, url, tags}. "hidden" is mock-only: found only by a constrained search.
# Prices are per person: flights round-trip, stays per night (4 sharing), activities per person.
OPTIONS = [
    {"id": "flight_redeye", "type": "flight", "title": "Red-eye nonstop, 10:50pm", "price": 189, "tags": ["red-eye", "nonstop"]},
    {"id": "flight_atl", "type": "flight", "title": "1-stop via ATL, 6:15am", "price": 214, "tags": ["layover", "morning"]},
    {"id": "flight_dfw", "type": "flight", "title": "1-stop via DFW, 1:40pm", "price": 221, "tags": ["layover", "afternoon"]},
    {"id": "flight_nonstop", "type": "flight", "title": "Nonstop SFO→MIA, 7:05am", "price": 259, "tags": ["nonstop", "morning"]},
    {"id": "stay_resort", "type": "stay", "title": "Oceanfront resort, Mid-Beach", "price": 185, "tags": ["beach", "hotel", "pool"]},
    {"id": "stay_boutique", "type": "stay", "title": "Boutique hotel, South Beach", "price": 135, "tags": ["beach", "hotel", "nightlife"]},
    {"id": "stay_inn", "type": "stay", "title": "Budget inn, Downtown Miami", "price": 79, "tags": ["city", "far-from-beach"]},
    {"id": "stay_hostel", "type": "stay", "title": "Hostel dorm, South Beach", "price": 45, "tags": ["beach", "hostel"], "hidden": True},
    {"id": "stay_rental", "type": "stay", "title": "4-bedroom rental, North Beach", "price": 88, "tags": ["beach", "kitchen"], "hidden": True},
    {"id": "act_airboat_sunrise", "type": "activity", "title": "Everglades airboat tour, 8am", "price": 49, "tags": ["airboat", "everglades", "morning"]},
    {"id": "act_airboat_afternoon", "type": "activity", "title": "Everglades airboat tour, 2pm", "price": 45, "tags": ["airboat", "everglades", "afternoon"]},
    {"id": "act_snorkel_keywest", "type": "activity", "title": "Key West reef snorkel trip", "price": 65, "tags": ["snorkel", "keys", "long-drive"]},
    {"id": "act_snorkel_keylargo", "type": "activity", "title": "Key Largo reef snorkel (John Pennekamp)", "price": 55, "tags": ["snorkel", "keys", "long-drive"]},
    {"id": "act_tasting_menu", "type": "activity", "title": "Chef's tasting menu", "price": 185, "tags": ["fine-dining", "tasting-menu"]},
    {"id": "act_seafood_dinner", "type": "activity", "title": "Seafood dinner, bay-view terrace", "price": 72, "tags": ["fine-dining", "seafood"]},
]
for _o in OPTIONS:
    _o.setdefault("url", f"https://example.com/mock/{_o['id']}")


# ---------------------------------------------------------------- P4: researcher.research
def research(trip: dict, request: dict) -> list[dict]:
    """request = {type, query, max_price, avoid_tags}. A plain search returns the visible options;
    a constrained one (max_price or avoid_tags set) also finds hidden options that satisfy it."""
    constrained = request.get("max_price") is not None or bool(request.get("avoid_tags"))
    avoid = set(request.get("avoid_tags") or [])
    out = []
    for o in OPTIONS:
        if o["type"] != request["type"] or (o.get("hidden") and not constrained):
            continue
        if request.get("max_price") is not None and o["price"] > request["max_price"]:
            continue
        if avoid & set(o["tags"]):
            continue
        out.append({k: v for k, v in o.items() if k != "hidden"})
    return out


# ---------------------------------------------------------------- P2: memory
_MEMORY: dict[str, list[str]] = {}


def remember(dataset: str, text: str) -> None:
    _MEMORY.setdefault(dataset, []).append(text)


def recall(dataset: str, query: str) -> list[str]:
    words = [w for w in query.lower().split() if len(w) > 2]
    return [t for t in _MEMORY.get(dataset, []) if any(w in t.lower() for w in words)]


def improve(dataset: str) -> None:
    pass


def forget(dataset: str) -> None:
    _MEMORY.pop(dataset, None)


# ---------------------------------------------------------------- P2: advocate.evaluate / concede
PRICE_WEIGHT = {"tight": 1.0, "medium": 0.5, "flexible": 0.1}


def _prefs(user: dict) -> set[str]:
    a = user["answers"]
    return (set(a.get("activities", [])) | {a.get("lodging"), a.get("food"), a.get("pace")}) - {None, "", "any"}


def _utility(user: dict, o: dict) -> float:
    weight = PRICE_WEIGHT.get(user["answers"].get("budget"), 0.5)
    return len(_prefs(user) & set(o["tags"])) - weight * o["price"] / (user.get("budget_per_night") or 100)


def _veto_reasons(user: dict, o: dict, remembered: list[str]) -> list[str]:
    reasons = []
    budget = user.get("budget_per_night")
    if o["type"] != "flight" and budget and o["price"] > budget:
        reasons.append(f"${o['price']} is over {user['name']}'s ${budget} limit")
    for tag in o["tags"]:
        if tag in user["answers"].get("dealbreakers", []):
            reasons.append(f"{tag} is a dealbreaker for {user['name']}")
    for reason in remembered:  # learned from earlier human feedback, e.g. "no layovers" blocks tag "layover"
        if any(tag in reason.lower() for tag in o["tags"]):
            reasons.append(f"I remember {user['name']} said “{reason}”")
            break
    return reasons


def evaluate(user: dict, options: list[dict], round: int) -> list[Message]:
    me = f"advocate_{user['id']}"
    remembered = [f.split("because:", 1)[1].strip()
                  for f in recall(f"user_{user['id']}", "rejected because") if "because:" in f]
    msgs, acceptable = [], []
    for o in options:
        reasons = _veto_reasons(user, o, remembered)
        if reasons:
            msgs.append(Message(round, me, "coordinator", "objection", f"Veto {o['title']}: {'; '.join(reasons)}", [o["id"]]))
        else:
            acceptable.append(o)
    if acceptable:
        best = max(acceptable, key=lambda o: _utility(user, o))
        msgs.insert(0, Message(round, me, "coordinator", "proposal",
                               f"{user['name']} votes for {best['title']} (${best['price']})", [best["id"]]))
    return msgs


def concede(user: dict, lost: dict, chosen: dict, round: int) -> Message | None:
    return Message(round, f"advocate_{user['id']}", "coordinator", "concession",
                   f"{user['name']} gives up {lost['title']} (${lost['price']}) and accepts {chosen['title']} (${chosen['price']})",
                   [lost["id"], chosen["id"]])
