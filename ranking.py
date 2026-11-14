"""Ranking and group decision. Owner: P4.

    rank(user, options, notes=()) -> list[(Option, float)]     one traveler's view, best first
    decide(users, options, notes=None) -> list[Decision]       the group's view, best first

Superlinked SIE, two stages (sie.py):
  1. Qwen3 Embedding 4B: embed the traveler's profile and each option, keep the closest by cosine similarity.
  2. Qwen3 Reranker 4B: reorder the shortlist against the profile (answers, budget, dealbreakers, and any
     notes such as "rejected X because: Y"), blended with a price-vs-budget term.
decide() scores every option for every traveler, rescales each traveler's scores to 0..1 so nobody
dominates, and averages them. There is no fallback scorer: if Superlinked is unreachable, SIEError is raised.
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import sie

SHORTLIST = 8
PRICE_WEIGHT = {"tight": 1.0, "comfortable": 0.5, "splurge": -0.1}  # splurge: the nicer (pricier) option wins
_vec_cache: dict[str, list[float]] = {}


@dataclass
class Decision:
    option: dict
    score: float                                              # group score, 0..1
    by_user: dict[str, float] = field(default_factory=dict)   # traveler id -> 0..1


def profile_text(user: dict, notes=()) -> str:
    a = user.get("answers") or {}
    parts = [f"{user['name']} travels from {user.get('home_city') or 'unknown'}."]
    if user.get("budget_per_night"):
        parts.append(f"Budget ${user['budget_per_night']} per night ({a.get('budget') or 'unspecified'}).")
    for key, text in (("pace", "Pace"), ("food", "Food"), ("lodging", "Lodging")):
        if a.get(key):
            parts.append(f"{text}: {a[key]}.")
    if a.get("activities"):
        parts.append("Wants: " + ", ".join(a["activities"]) + ".")
    if a.get("dealbreakers"):
        parts.append("Dealbreakers (must avoid): " + ", ".join(a["dealbreakers"]) + ".")
    parts += [f"Feedback: {n}" for n in notes]
    return " ".join(parts)


def option_text(o: dict) -> str:
    return f"{o['type']}: {o['title']}. ${o['price']:,.0f}. Tags: {', '.join(o.get('tags') or [])}."


def _cos(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def _price_term(user: dict, o: dict) -> float:
    weight = PRICE_WEIGHT.get((user.get("answers") or {}).get("budget"), 0.5)
    return -weight * o["price"] / (user.get("budget_per_night") or 100)


def rank(user: dict, options: list[dict], notes=()) -> list[tuple[dict, float]]:
    if len(options) < 2:
        return [(o, 0.0) for o in options]
    profile = profile_text(user, notes)
    texts = {o["id"]: option_text(o) for o in options}
    missing = [i for i, t in texts.items() if t not in _vec_cache]
    if missing:
        for i, vec in zip(missing, sie.encode([texts[i] for i in missing])):
            _vec_cache[texts[i]] = vec
    query_vec = sie.encode([profile], is_query=True,
                           instruction="Given a traveler profile, find trip options that fit them")[0]
    by_sim = sorted(options, key=lambda o: _cos(query_vec, _vec_cache[texts[o["id"]]]), reverse=True)
    short, rest = by_sim[:SHORTLIST], by_sim[SHORTLIST:]
    rel = sie.score(profile, [texts[o["id"]] for o in short],
                    instruction="Judge whether the trip option suits the traveler's budget, preferences, "
                                "dealbreakers and feedback")
    ranked = sorted(((o, r + 0.15 * _price_term(user, o)) for o, r in zip(short, rel)),
                    key=lambda p: p[1], reverse=True)
    floor = min((s for _, s in ranked), default=0.0) - 1.0
    return ranked + [(o, floor - i) for i, o in enumerate(rest)]  # the tail keeps its embedding order


def _unit(scores: dict[str, float]) -> dict[str, float]:
    lo, hi = min(scores.values()), max(scores.values())
    return {k: 0.5 if hi == lo else (v - lo) / (hi - lo) for k, v in scores.items()}


def decide(users: list[dict], options: list[dict], notes: dict[str, list[str]] | None = None) -> list[Decision]:
    """Group ranking. Ties go to the cheaper option."""
    if not options:
        return []
    if len(options) == 1:  # nothing to compare: it is the only option
        return [Decision(options[0], 1.0, {u["id"]: 1.0 for u in users})]
    notes = notes or {}
    with ThreadPoolExecutor(max_workers=min(4, len(users))) as pool:
        ranked = list(pool.map(lambda u: rank(u, options, notes.get(u["id"], ())), users))
    per_user = {u["id"]: _unit({o["id"]: s for o, s in r}) for u, r in zip(users, ranked)}
    out = [Decision(o, sum(per_user[u["id"]][o["id"]] for u in users) / len(users),
                    {u["id"]: per_user[u["id"]][o["id"]] for u in users}) for o in options]
    return sorted(out, key=lambda d: (-d.score, d.option["price"]))
