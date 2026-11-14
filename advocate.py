"""Advocate agents: one per traveler, each with its own memory dataset "user_<id>". Owner: P2.

    evaluate(user, options, round) -> list[Message]   proposals + objections, sent to the coordinator
    concede(user, lost, chosen, round) -> Message | None

How an Advocate decides, for each option:
  1. Hard rules from the questionnaire: over budget_per_night (not for flights), or a dealbreaker tag -> objection.
  2. Memory: it recall()s what this traveler rejected before (the Coordinator saves
     "<name> rejected <title> because: <reason>" into user_<id> on a human rejection). Words in the reason are
     mapped onto the shared tag list, so "no layovers" vetoes every option tagged "layover". The objection
     quotes the memory, so the demo shows memory changing behaviour.
  3. Among the options it accepts, it proposes its favourite: ranking.rank (P4) if that file exists, else a
     local score (preference tags matched minus a price penalty that depends on the budget answer).
"""
from __future__ import annotations

import importlib
import json
import os
import re

import memory
from protocol import Message

# ---------------------------------------------------------------- ranking (P4 when available)
try:
    _ranking = importlib.import_module("ranking")
except ModuleNotFoundError as exc:
    if exc.name != "ranking":
        raise
    _ranking = None

PRICE_WEIGHT = {"tight": 1.0, "comfortable": 0.5, "splurge": -0.1}  # splurge: the nicer (pricier) option wins

# Words people use in a rejection reason -> tags from the shared tag list.
TAG_WORDS = {
    "red-eye": ["red-eye", "red eye", "redeye", "overnight flight", "night flight"],
    "layover": ["layover", "connection", "connecting", "stopover", "1-stop", "one stop", "no stops"],
    "nonstop": [],
    "morning": ["early", "morning", "6am", "6:15", "sunrise", "8am"],
    "afternoon": ["afternoon"],
    "long-drive": ["long drive", "long-drive", "driving", "drive", "far away", "too far"],
    "hostel": ["hostel", "dorm", "bunk"],
    "hotel": ["hotel"],
    "rental": ["rental", "airbnb"],
    "nightlife": ["nightlife", "noisy", "noise", "loud", "party"],
    "beach": [],  # too ambiguous ("far from the beach" is not a beach veto)
    "pool": [],
    "airboat": ["airboat"],
    "snorkel": ["snorkel"],
    "fine-dining": ["fine dining", "fine-dining", "fancy", "upscale"],
    "tasting-menu": ["tasting menu", "tasting-menu"],
    "seafood": ["seafood", "fish"],
    "everglades": ["everglades", "alligator", "swamp"],
}


def _tags_in(reason: str) -> set[str]:
    r = reason.lower()
    out = set()
    for tag, words in TAG_WORDS.items():
        if any(re.search(r"(?<![a-z])" + re.escape(w), r) for w in words):
            out.add(tag)
    return out


# The group's shared memory (also the Band room's memory). P3's Coordinator writes "trip_<trip id>".
TRIP_DATASET = f"trip_{os.getenv('TRIP_ID', 'miami')}"  # demo default
_TRIPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trips")


def trip_dataset(user_id: str | None = None) -> str:
    """trip_<id> of the newest web-app trip this traveler belongs to; TRIP_ID or the demo trip otherwise."""
    if os.getenv("TRIP_ID") or not user_id or not os.path.isdir(_TRIPS_DIR):
        return TRIP_DATASET
    best = None
    for name in os.listdir(_TRIPS_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(_TRIPS_DIR, name), encoding="utf-8") as f:
                t = json.load(f)
        except (OSError, ValueError):
            continue
        if user_id in (t.get("members") or []) and (best is None or (t.get("created_at") or "") > (best.get("created_at") or "")):
            best = t
    return f"trip_{best['id']}" if best else TRIP_DATASET

# ---------------------------------------------------------------- onboarding into memory
_onboarded: set[str] = set()


def onboard(user: dict) -> None:
    """Write the questionnaire into user_<id> once, so every preference is recallable (and lands in cognee)."""
    uid = user["id"]
    if uid in _onboarded:
        return
    _onboarded.add(uid)
    a, name, ds = user.get("answers") or {}, user["name"], f"user_{uid}"
    if user.get("budget_per_night"):
        memory.remember(ds, f"{name}'s budget is ${user['budget_per_night']} per night ({a.get('budget') or 'unspecified'})", share=False)
    if user.get("home_city"):
        memory.remember(ds, f"{name} flies from {user['home_city']}", share=False)
    for key in ("pace", "food", "lodging"):
        if a.get(key):
            memory.remember(ds, f"{name}'s preferred {key}: {a[key]}", share=False)
    if a.get("activities"):
        memory.remember(ds, f"{name} wants to do: {', '.join(a['activities'])}", share=False)
    if a.get("dealbreakers"):
        memory.remember(ds, f"{name}'s dealbreakers: {', '.join(a['dealbreakers'])}", share=False)


def _reasons(facts: list[str], who: str) -> list[tuple[str, set[str], str]]:
    out = []
    for fact in facts:
        if "because:" not in fact:
            continue
        reason = fact.split("because:", 1)[1].strip().strip("“”\"")
        tags = _tags_in(reason)
        if tags:
            out.append((reason, tags, who))
    return out


def _learned(user: dict) -> list[tuple[str, set[str], str]]:
    """(reason, tags, whose) for each earlier rejection that should steer this Advocate:
    the traveler's own (user_<id>) and the whole group's (trip_<id>, facts starting "The group ...").
    A single traveler's rejection that the Coordinator also logged in trip memory does NOT bind the others."""
    own = memory.recall(f"user_{user['id']}", "rejected because", top_k=10)
    group = [f for f in memory.recall(trip_dataset(user["id"]), "group rejected because", top_k=10)
             if f.lower().startswith("the group")]
    return _reasons(own, user["name"]) + _reasons(group, "the group")


# ---------------------------------------------------------------- scoring + vetoes
def _prefs(user: dict) -> set[str]:
    a = user["answers"]
    return (set(a.get("activities") or []) | {a.get("lodging"), a.get("food"), a.get("pace")}) - {None, "", "any"}


def _utility(user: dict, o: dict) -> float:
    weight = PRICE_WEIGHT.get(user["answers"].get("budget"), 0.5)
    return len(_prefs(user) & set(o.get("tags") or [])) - weight * o["price"] / (user.get("budget_per_night") or 100)


def _rank(user: dict, options: list[dict]) -> list[dict]:
    if _ranking is not None:
        try:
            return [o for o, _ in _ranking.rank(user, options)]
        except Exception as exc:  # P4's ranker broke mid-demo: keep negotiating with ours
            print(f"[advocate] ranking.rank failed ({type(exc).__name__}: {exc}); using local score", flush=True)
    return sorted(options, key=lambda o: _utility(user, o), reverse=True)


def _veto_reasons(user: dict, o: dict, learned: list[tuple[str, set[str]]]) -> list[str]:
    reasons, tags = [], set(o.get("tags") or [])
    budget = user.get("budget_per_night")
    if o["type"] != "flight" and budget and o["price"] > budget:
        reasons.append(f"${o['price']:,.0f} is over {user['name']}'s ${budget} limit")
    for tag in sorted(tags & set(user["answers"].get("dealbreakers") or [])):
        reasons.append(f"{tag} is a dealbreaker for {user['name']}")
    for reason, bad, who in learned:
        hit = bad & tags
        if hit:
            where = f"memory: {user['name']} said" if who == user["name"] else "group memory says"
            reasons.append(f"{where} “{reason}” ({', '.join(sorted(hit))})")
            break
    return reasons


# ---------------------------------------------------------------- contract
def evaluate(user: dict, options: list[dict], round: int) -> list[Message]:
    onboard(user)
    me = f"advocate_{user['id']}"
    learned = _learned(user)
    msgs, acceptable = [], []
    for o in options:
        reasons = _veto_reasons(user, o, learned)
        if reasons:
            msgs.append(Message(round, me, "coordinator", "objection",
                                f"Veto {o['title']}: {'; '.join(reasons)}", [o["id"]]))
        else:
            acceptable.append(o)
    if acceptable:
        best = _rank(user, acceptable)[0]
        why = sorted(_prefs(user) & set(best.get("tags") or []))
        note = f", matches {', '.join(why)}" if why else ""
        msgs.insert(0, Message(round, me, "coordinator", "proposal",
                               f"{user['name']} votes for {best['title']} (${best['price']:,.0f}{note})", [best["id"]]))
    return msgs


def concede(user: dict, lost: dict, chosen: dict, round: int) -> Message | None:
    """Our favourite was vetoed by someone else. Accept the group's pick unless it breaks one of OUR hard rules
    (then stay silent: our objection already stands)."""
    if _veto_reasons(user, chosen, _learned(user)):
        return None
    memory.remember(f"user_{user['id']}",
                    f"{user['name']} conceded {lost['title']} for {chosen['title']} in round {round}")
    return Message(round, f"advocate_{user['id']}", "coordinator", "concession",
                   f"{user['name']} gives up {lost['title']} (${lost['price']:,.0f}) and accepts "
                   f"{chosen['title']} (${chosen['price']:,.0f})", [lost["id"], chosen["id"]])
