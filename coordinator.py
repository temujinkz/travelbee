"""Coordinator & negotiation. Owner: P3.

    python coordinator.py         full negotiation in the terminal, scripted human answers
    python coordinator.py --ask   you answer the human prompts yourself

Contracts (TEAM_SPLIT.md):
    run_trip(trip, users, ask_human) -> plan      runs everything, incl. approval + re-plans, returns the final plan
    replan(trip, users, plan, rejection, ask_human) -> plan     rejection = {option_id, reason, by}
    ask_human(question, choices) -> str           returns one of `choices`; choices == [] means free text
    plan = {slot: Option | None}; slots = "flight", "stay", "activity:<tag>" (one per favorite activity)

Negotiation rules (every step is a Message in LOG):
  1. The Researcher searches every slot; each Advocate votes (proposal) and vetoes (objection).
  2. A slot goes to the veto-free option with the most votes (tie: cheaper). Advocates whose
     favorite was vetoed by someone else concede.
  3. Everything vetoed: research again with tighter constraints (lowest budget, nobody's dealbreakers).
  4. Research finds nothing new, or 3 rounds pass: deadlock, the humans decide.
  5. The plan goes to the humans. A rejection is saved to memory, improve() runs, and that slot is re-negotiated.
"""
from __future__ import annotations

import importlib
import re
import os
import sys
import unicodedata
from datetime import date

import itinerary
import sie
from protocol import Message, post

MAX_ROUNDS = 3


def _real_or_mock(module: str):
    """Use the teammate's real module as soon as the file exists; mocks.py until then."""
    try:
        mod = importlib.import_module(module)
        print(f"[coordinator] using real {module}.py", flush=True)
        return mod
    except ModuleNotFoundError as exc:
        if exc.name != module:  # their file exists but one of ITS imports is missing: fail loudly
            raise
        return importlib.import_module("mocks")


researcher = _real_or_mock("researcher")  # research(trip, request) -> list[Option]
advocate = _real_or_mock("advocate")      # evaluate(user, options, round), concede(user, lost, chosen, round)
memory = _real_or_mock("memory")          # remember(dataset, text), improve(dataset)

_STATE: dict[str, dict] = {}  # per trip id: options seen, rejected ids, vetoes, overrides, concessions, round


# ---------------------------------------------------------------- helpers
def _wanted(u: dict) -> list[str]:
    """Activity tags a traveler wants in the plan: their activities, plus one nice dinner if food is fine-dining."""
    a = u["answers"]
    return list(a.get("activities") or []) + (["fine-dining"] if a.get("food") == "fine-dining" else [])


_CITY_NAMES: dict[str, str] = {}  # city slug -> home city as typed


def _city_slug(city: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _plain(city or "")).strip("-")


def _flight_slots(users: list[dict]) -> list[str]:
    """One flight per home city ("flight:paris", "flight:new-york"); a single "flight" when everyone shares one."""
    slugs = []
    for u in users:
        sl = _city_slug(u.get("home_city") or "")
        if sl and sl not in slugs:
            slugs.append(sl)
            _CITY_NAMES[sl] = (u.get("home_city") or "").strip()
    return ["flight"] if len(slugs) <= 1 else [f"flight:{sl}" for sl in slugs]


def slots_for(users: list[dict]) -> list[str]:
    slots = _flight_slots(users) + ["stay"]
    for u in users:
        for tag in _wanted(u):
            if f"activity:{tag}" not in slots:
                slots.append(f"activity:{tag}")
    if not any(s.startswith("activity:") for s in slots):
        slots.append("activity:sightseeing")  # nobody picked one: the destination's own highlight
    return slots


def _voters(users: list[dict], slot: str) -> list[dict]:
    """Only travelers leaving from a city vote on that city's flight; everyone votes on the rest."""
    if slot.startswith("flight:"):
        return [u for u in users if _city_slug(u.get("home_city") or "") == slot.split(":", 1)[1]] or users
    return users


def _mine(u: dict, slot: str) -> bool:
    return not slot.startswith("flight:") or _city_slug(u.get("home_city") or "") == slot.split(":", 1)[1]


def label(slot: str) -> str:
    if slot.startswith("flight:"):
        sl = slot.split(":", 1)[1]
        return f"Flight from {_CITY_NAMES.get(sl) or sl.replace('-', ' ').title()}"
    if slot.startswith("activity:"):
        return slot.split(":", 1)[1].replace("-", " ").capitalize()
    return slot.capitalize()


def price(o: dict) -> str:
    unit = {"stay": "/night", "flight": " round-trip"}.get(o["type"], " pp")
    return f"${o['price']:,.0f}{unit}"


def _type(slot: str) -> str:
    return "activity" if slot.startswith("activity:") else "flight" if slot.startswith("flight") else slot


def _fits(option: dict, slot: str) -> bool:
    if option["type"] != _type(slot):
        return False
    if slot.startswith("flight:"):
        return f"from:{slot.split(':', 1)[1]}" in option.get("tags", [])
    return not slot.startswith("activity:") or slot.split(":", 1)[1] in option.get("tags", [])


def _name(users: list[dict], uid: str) -> str:
    return next((u["name"] for u in users if u["id"] == uid), "The group")


def _plain(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()


def _per_person(trip: dict, plan: dict) -> float:
    try:
        nights = max(1, (date.fromisoformat(trip["end"]) - date.fromisoformat(trip["start"])).days)
    except (KeyError, ValueError):
        nights = 1
    flights = [o["price"] for o in plan.values() if o and o["type"] == "flight"]
    rest = sum(o["price"] * (nights if o["type"] == "stay" else 1) for o in plan.values() if o and o["type"] != "flight")
    return rest + (sum(flights) / len(flights) if flights else 0)  # each traveler pays their own flight


def _candidates(st: dict, slot: str) -> list[dict]:
    return sorted((o for o in st["options"].values() if _fits(o, slot) and o["id"] not in st["rejected"]),
                  key=lambda o: o["price"])


# ---------------------------------------------------------------- research
def _request(trip: dict, users: list[dict], slot: str, constrained: bool) -> dict:
    t = _type(slot)
    where = f"{trip['destination']}, {trip['start']} to {trip['end']}"
    origin = _CITY_NAMES.get(slot.split(":", 1)[1], "") if slot.startswith("flight:") else (users[0].get("home_city") or "")
    query = {"flight": f"round-trip flights from {origin} to {where}",
             "stay": f"stay for {len(users)} people in {where}"}.get(t, f"{label(slot).lower()} near {trip['destination']}")
    req = {"type": t, "query": query, "max_price": None, "avoid_tags": [],
           "tag": slot.split(":", 1)[1] if t == "activity" else None,      # new Researcher: activity tag
           "origin": origin, "destination": trip["destination"]}
    if constrained:
        budgets = [u["budget_per_night"] for u in users if u.get("budget_per_night")]
        if t != "flight" and budgets:
            req["max_price"] = min(budgets)
        req["avoid_tags"] = sorted({d for u in users for d in u["answers"].get("dealbreakers", [])})
    return req


def _research(trip: dict, users: list[dict], slots: list[str], rnd: int, constrained: bool) -> dict[str, list[dict]]:
    st = _STATE[trip["id"]]
    reqs = {s: _request(trip, users, s, constrained) for s in slots}
    if constrained:
        r = next(iter(reqs.values()))
        limits = (f"under ${r['max_price']}" if r["max_price"] else "") + (f", no {', '.join(r['avoid_tags'])}" if r["avoid_tags"] else "")
        text = f"Every option was vetoed for {', '.join(label(s).lower() for s in slots)}. Search again: {limits.strip(', ')}"
    else:
        text = "Find options for: " + ", ".join(label(s).lower() for s in slots)
    post(Message(rnd, "coordinator", "researcher", "research_request", text))
    found = {}
    for slot, req in reqs.items():
        try:
            results = researcher.research(trip, req)
            if slot.startswith("flight:"):  # remember which home city this fare leaves from
                results = [dict(o, tags=list(o.get("tags", [])) + [f"from:{slot.split(':', 1)[1]}"]) for o in results]
            results = [o for o in results if _fits(o, slot)]
        except Exception as exc:  # e.g. a Superlinked / Keenable outage: keep negotiating the other slots
            print(f"[coordinator] research failed for {slot}: {exc}", flush=True)
            results = []
        new = [o for o in results if o["id"] not in st["options"]]
        for o in new:
            st["options"][o["id"]] = o
        found[slot] = new
        content = (f"{label(slot)}: " + "; ".join(f"{o['title']} ({price(o)})" for o in new)) if new \
            else f"{label(slot)}: nothing new matches those constraints"
        post(Message(rnd, "researcher", "coordinator", "research_result", content, [o["id"] for o in new]))
    return found


# ---------------------------------------------------------------- negotiation
def _collect(users: list[dict], cands: list[dict], rnd: int):
    votes, tops, vetoes = {}, {}, {}
    if not cands:
        return votes, tops, vetoes
    for u in users:
        for msg in advocate.evaluate(u, cands, rnd) or []:
            post(msg)
            if msg.type == "proposal" and msg.refs:
                votes[msg.refs[0]] = votes.get(msg.refs[0], 0) + 1
                tops[u["id"]] = msg.refs[0]
            elif msg.type == "objection":
                for ref in msg.refs:
                    vetoes.setdefault(ref, []).append((u["id"], msg.content))
    return votes, tops, vetoes


def _concessions(st: dict, users: list[dict], best: dict, tops: dict, vetoes: dict, rnd: int) -> None:
    for u in users:
        top = tops.get(u["id"])
        if not top or top == best["id"] or not vetoes.get(top) or (u["id"], top) in st["conceded"]:
            continue
        st["conceded"].add((u["id"], top))
        msg = advocate.concede(u, st["options"][top], best, rnd)
        if msg:
            post(msg)


def _negotiate(trip: dict, users: list[dict], slots: list[str], plan: dict, ask_human, start_round: int) -> None:
    st = _STATE[trip["id"]]
    open_slots = list(slots)
    for rnd in range(start_round, start_round + MAX_ROUNDS):
        st["round"] = rnd
        blocked = []
        for slot in open_slots:
            cands = _candidates(st, slot)
            voters = _voters(users, slot)
            votes, tops, vetoes = _collect(voters, cands, rnd)
            st["vetoes"][slot] = vetoes
            feasible = [o for o in cands if not vetoes.get(o["id"])]
            if not feasible:
                blocked.append(slot)
                continue
            best = max(feasible, key=lambda o: (votes.get(o["id"], 0), -o["price"]))
            _concessions(st, voters, best, tops, vetoes, rnd)
            plan[slot] = best
            n = votes.get(best["id"], 0)
            post(Message(rnd, "coordinator", "all", "decision",
                         f"{label(slot)}: {best['title']} ({price(best)}), {n} vote{'' if n == 1 else 's'}, no vetoes",
                         [best["id"]]))
        if not blocked:
            return
        last_round = rnd == start_round + MAX_ROUNDS - 1
        found = {} if last_round else _research(trip, users, blocked, rnd, constrained=True)
        for slot in blocked:
            if not found.get(slot):
                _deadlock(trip, users, slot, plan, ask_human, rnd)
        open_slots = [s for s in blocked if found.get(s)]
        if not open_slots:
            return


def _deadlock(trip: dict, users: list[dict], slot: str, plan: dict, ask_human, rnd: int) -> None:
    st = _STATE[trip["id"]]
    cands = _candidates(st, slot)
    if not cands:
        alt = _alternative(trip, st, slot)
        if alt:  # plan B: another activity this destination offers
            post(Message(rnd, "coordinator", "all", "decision",
                         f"{label(slot)}: nothing fits everyone's limits, so the agents try {label(alt).lower()} instead"))
            if slot in st["slots"]:
                st["slots"][st["slots"].index(slot)] = alt
            plan.pop(slot, None)
            _research(trip, users, [alt], rnd, constrained=False)
            _negotiate(trip, users, [alt], plan, ask_human, start_round=rnd + 1)
            return
        plan[slot] = None
        post(Message(rnd, "coordinator", "all", "decision", f"{label(slot)}: no options found, skipped"))
        return
    vetoes = st["vetoes"].get(slot, {})

    def blockers(o: dict) -> str:
        return ", ".join(sorted({_name(users, uid) for uid, _ in vetoes.get(o["id"], [])})) or "nobody"

    parts = [f"{o['title']} ({price(o)}) is vetoed by {blockers(o)}" for o in cands]
    question = f"Your agents can't agree on {label(slot).lower()}: " + "; ".join(parts) + ". Which one, or skip it?"
    choices = [o["title"] for o in cands] + [f"Skip {label(slot).lower()}"]
    post(Message(rnd, "coordinator", "human", "human_request", question, [o["id"] for o in cands]))
    answer = ask_human(question, choices)
    chosen = next((o for o in cands if o["title"] == answer), None)
    post(Message(rnd, "human", "coordinator", "human_response", answer, [chosen["id"]] if chosen else []))
    plan[slot] = chosen
    if chosen:
        overridden = sorted({uid for uid, _ in vetoes.get(chosen["id"], [])})
        st["overrides"][slot] = overridden
        names = ", ".join(_name(users, uid) for uid in overridden)
        text = f"Humans chose {chosen['title']} for {label(slot).lower()}" + (f", overriding {names}'s veto" if names else "")
    else:
        overridden = []
        text = f"Humans skipped {label(slot).lower()}"
    post(Message(rnd, "coordinator", "all", "decision", text, [chosen["id"]] if chosen else []))
    for ds in [f"trip_{trip['id']}"] + [f"user_{uid}" for uid in overridden]:
        memory.remember(ds, text)


def _alternative(trip: dict, st: dict, slot: str) -> str | None:
    """Next activity from the destination's list that isn't planned or tried yet (at most 2 swaps per trip)."""
    if not slot.startswith("activity:") or st["alternatives"] >= 2:
        return None
    st["tried"].add(slot)
    for sl, _label in trip.get("activities") or []:
        cand = f"activity:{sl}"
        if cand not in st["slots"] and cand not in st["tried"]:
            st["alternatives"] += 1
            st["tried"].add(cand)
            return cand
    return None


# ---------------------------------------------------------------- approval + re-plan
def _who_and_why(text: str, users: list[dict]) -> tuple[str, str]:
    head, sep, tail = text.partition(":")
    if sep:
        for u in users:
            if _plain(head) in (_plain(u["name"]), _plain(u["id"])):
                return u["id"], tail.strip() or text
    return "group", text.strip()


def _ask_approval(trip: dict, users: list[dict], plan: dict, ask_human) -> dict | None:
    st = _STATE[trip["id"]]
    rnd = st["round"]
    st["plan"] = dict(plan)  # the web bridge shows these items on the approval screen
    items = [(s, o) for s, o in plan.items() if o]
    summary = "; ".join(f"{label(s)}: {o['title']} ({price(o)})" for s, o in items)
    skipped = [label(s).lower() for s, o in plan.items() if not o]
    if skipped:
        summary += "; skipped: " + ", ".join(skipped)
    ids = [o["id"] for _, o in items]
    post(Message(rnd, "coordinator", "all", "decision",
                 f"Proposed plan, about ${_per_person(trip, plan):,.0f} per person. {summary}", ids))
    question = "Approve this plan? You can reject one item and the agents will re-plan around it."
    choices = ["Approve"] + [f"Reject {label(s).lower()}" for s, _ in items]
    post(Message(rnd, "coordinator", "human", "human_request", question, ids))
    answer = ask_human(question, choices)
    slot = next((s for s, _ in items if answer == f"Reject {label(s).lower()}"), None)
    if slot is None:
        post(Message(rnd, "human", "coordinator", "human_response", "Approved"))
        return None
    option = plan[slot]
    text = ask_human(f"Who is rejecting {option['title']}, and why? (e.g. “Ben: no layovers”)", [])
    by, reason = _who_and_why(text, users)
    post(Message(rnd, "human", "coordinator", "human_response",
                 f"{_name(users, by)} rejects {option['title']}: “{reason}”", [option["id"]]))
    return {"option_id": option["id"], "reason": reason, "by": by}


def replan(trip: dict, users: list[dict], plan: dict, rejection: dict, ask_human) -> dict:
    st = _STATE[trip["id"]]
    option = st["options"][rejection["option_id"]]
    slot = next(s for s, o in plan.items() if o and o["id"] == option["id"])
    st["rejected"].add(option["id"])
    by = rejection.get("by") or "group"
    fact = f"{_name(users, by)} rejected {option['title']} because: {rejection['reason']}"
    datasets = [f"trip_{trip['id']}"] + ([f"user_{by}"] if by != "group" else [])
    for ds in datasets:
        memory.remember(ds, fact)
        memory.improve(ds)
    st["round"] += 1
    post(Message(st["round"], "coordinator", "advocates", "decision",
                 f"Re-planning {label(slot).lower()}. Saved to memory ({', '.join(datasets)}) and ran improve(): “{fact}”",
                 [option["id"]]))
    new_plan = {s: o for s, o in plan.items() if s != slot}
    _negotiate(trip, users, [slot], new_plan, ask_human, start_round=st["round"])
    return {s: new_plan.get(s) for s in plan}


def _checks(u: dict, plan: dict, st: dict) -> list[str]:
    out = []
    stay, budget = plan.get("stay"), u.get("budget_per_night")
    if stay and budget:
        out.append(("✓" if stay["price"] <= budget else "✗") + f" stay {price(stay)} (limit ${budget})")
    for d in u["answers"].get("dealbreakers", []):
        hits = [s for s, o in plan.items() if o and d in o.get("tags", []) and _mine(u, s)]
        if not hits:
            out.append(f"✓ no {d}")
        elif all(u["id"] in st["overrides"].get(s, []) for s in hits):
            out.append(f"⚠ {d} accepted in a group decision")
        else:
            out.append(f"✗ {d}")
    for tag in _wanted(u):
        out.append(("✓ " if plan.get(f"activity:{tag}") else "✗ no ") + tag.replace("-", " "))
    return out


def run_trip(trip: dict, users: list[dict], ask_human) -> dict:
    _STATE[trip["id"]] = {"options": {}, "rejected": set(), "vetoes": {}, "overrides": {}, "conceded": set(), "round": 1,
                          "tried": set(), "alternatives": 0}
    st = _STATE[trip["id"]]
    if os.getenv("TRANSPORT") == "band":
        import band

        band.assign_seats(users)  # each traveler's messages are posted by their own Advocate on Band
    slots = slots_for(users)
    st["slots"] = slots  # plan B can swap an activity slot for another one
    plan: dict = {}
    _research(trip, users, list(slots), 1, constrained=False)
    _negotiate(trip, users, list(slots), plan, ask_human, start_round=1)
    plan = {s: plan.get(s) for s in st["slots"]}
    while (rejection := _ask_approval(trip, users, plan, ask_human)) is not None:
        plan = replan(trip, users, plan, rejection, ask_human)
    lines = [f"{u['name']}: {', '.join(_checks(u, plan, st))}" for u in users]
    post(Message(st["round"], "coordinator", "all", "decision",
                 f"Final plan approved, about ${_per_person(trip, plan):,.0f} per person. " + " | ".join(lines),
                 [o["id"] for o in plan.values() if o]))
    memory.remember(f"trip_{trip['id']}", "Final plan: " + "; ".join(f"{label(s)}: {o['title']}" for s, o in plan.items() if o))
    try:
        if sie.available():  # Qwen3.8 27B writes the itinerary + where-to-stay pick; the template fallback adds nothing new
            it = itinerary.build_itinerary(trip, users, plan)
            st["itinerary"] = it
            rec = it["stay_recommendation"]
            post(Message(st["round"], "coordinator", "all", "decision",
                         f"Itinerary: {it['summary']} Where to stay: {rec['why']}", [rec["option_id"]] if rec["option_id"] else []))
    except Exception as exc:
        print(f"[coordinator] itinerary skipped: {exc}", flush=True)
    return plan


def _console_human(question: str, choices: list[str]) -> str:
    print(f"\n🙋 {question}")
    if not choices:
        return input("> ").strip()
    for i, c in enumerate(choices, 1):
        print(f"   {i}. {c}")
    while True:
        raw = input("> ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]


def _load_saved(trip_id: str) -> tuple[dict, list[dict]]:
    import json
    from pathlib import Path

    data = Path(__file__).resolve().parent / "data"
    try:
        trip = json.loads((data / "trips" / f"{trip_id}.json").read_text())
        users = [json.loads((data / "users" / f"{uid}.json").read_text()) for uid in trip["members"]]
    except FileNotFoundError as exc:
        sys.exit(f"Not found: {exc.filename}. Create the trip and have travelers answer in the web app first.")
    return trip, users


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python coordinator.py <trip_id>   (trips come from the web app: python3 web/server.py)")
    os.environ.setdefault("PRINT_LOG", "1")
    trip, users = _load_saved(sys.argv[1])
    final = run_trip(trip, users, _console_human)
    print("\nFINAL PLAN")
    for slot, o in final.items():
        print(f"  {label(slot):12s} {o['title']} ({price(o)})")
    if os.getenv("TRANSPORT") == "band":
        import band

        band.flush()
