"""Ranking. Owner: P4.   rank(user, options) -> list[(Option, float)], best first.

Superlinked SIE, two stages (sie.py):
  1. Qwen3 Embedding 4B: embed the traveler's profile and each option, keep options by cosine similarity.
  2. Qwen3 Reranker 4B: reorder the shortlist against the profile + budget.
The final score blends the rerank score with a price-vs-budget term, so budget-conscious travelers still
prefer cheaper options. Any SIE failure (no key, network, bad response) drops to the pure-Python scorer,
so the demo never stalls. Set SIE_DISABLE=1 to force the fallback.
"""
from __future__ import annotations

import math

import sie

SHORTLIST = 8
PRICE_WEIGHT = {"tight": 1.0, "comfortable": 0.5, "splurge": -0.1}  # splurge: the nicer (pricier) option wins
_vec_cache: dict[str, list[float]] = {}
_warned = False


def profile_text(user: dict) -> str:
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
    return " ".join(parts)


def option_text(o: dict) -> str:
    return f"{o['type']}: {o['title']}. ${o['price']:,.0f}. Tags: {', '.join(o.get('tags') or [])}."


def _cos(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def _price_term(user: dict, o: dict) -> float:
    weight = PRICE_WEIGHT.get((user.get("answers") or {}).get("budget"), 0.5)
    return -weight * o["price"] / (user.get("budget_per_night") or 100)


def _fallback(user: dict, options: list[dict]) -> list[tuple[dict, float]]:
    a = user.get("answers") or {}
    prefs = (set(a.get("activities") or []) | {a.get("lodging"), a.get("food"), a.get("pace")}) - {None, "", "any"}
    scored = [(o, len(prefs & set(o.get("tags") or [])) + _price_term(user, o)) for o in options]
    return sorted(scored, key=lambda p: p[1], reverse=True)


def _superlinked(user: dict, options: list[dict]) -> list[tuple[dict, float]]:
    profile = profile_text(user)
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
                    instruction="Judge whether the trip option suits the traveler's budget, preferences and dealbreakers")
    ranked = sorted(((o, r + 0.15 * _price_term(user, o)) for o, r in zip(short, rel)),
                    key=lambda p: p[1], reverse=True)
    floor = min((s for _, s in ranked), default=0.0) - 1.0
    return ranked + [(o, floor - i) for i, o in enumerate(rest)]  # the tail keeps its embedding order


def rank(user: dict, options: list[dict]) -> list[tuple[dict, float]]:
    global _warned
    if len(options) < 2 or not sie.available():
        return _fallback(user, options)
    try:
        return _superlinked(user, options)
    except Exception as exc:  # noqa: BLE001 - never let the ranker kill a negotiation
        if not _warned:
            print(f"[ranking] Superlinked failed ({type(exc).__name__}: {exc}); using local score", flush=True)
            _warned = True
        return _fallback(user, options)
