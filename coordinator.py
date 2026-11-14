"""Coordinator. Owner: P3.  Live pipeline: research, Superlinked decides, humans approve, re-plan on rejection.

    python coordinator.py <trip_id>      plan a trip saved by the web app (data/trips, data/users), answering in the terminal

Contracts (TEAM_SPLIT.md):
    run_trip(trip, users, ask_human=None, approve=None) -> plan   research -> decide -> approval -> final plan
    replan(trip, users, plan, rejection, ask_human=None) -> plan  rejection = {option_id, reason, by}
    ask_human(question, choices) -> str           returns one of `choices`; choices == [] means free text
    approve(plan) -> None | rejection             None = approved. Default asks through ask_human.
    plan = {slot: Option}; slots = "flight", "stay", "activity:<tag>" (one per wanted activity)

Flow (every step is a Message in protocol.LOG):
  1. Researcher: Google Flights for flights, Keenable for stays and activities (researcher.py).
  2. Superlinked decides each slot (ranking.decide): every traveler's profile is scored against every
     option, scores are averaged across the group, the best option wins. Budget and dealbreaker conflicts are
     reported as warnings; they are not vetoes.
  3. The plan goes to the humans. A rejection is saved to memory (remember + improve), added to that
     traveler's profile as feedback, and the slot is decided again without the rejected option.
There is no mock data and no fallback: if a service fails the error is raised so the caller can show it.
"""
from __future__ import annotations

import os
import sys
import unicodedata
from datetime import date

import itinerary
import memory
import ranking
import researcher
from protocol import Message, post


class PlanError(RuntimeError):
    pass


_STATE: dict[str, dict] = {}  # per trip id: options per slot, rejected ids, notes per traveler, round


# ---------------------------------------------------------------- helpers
def _wanted(u: dict) -> list[str]:
    """Activity tags a traveler wants in the plan: their activities, plus one nice dinner if food is fine-dining."""
    a = u["answers"]
    return list(a.get("activities") or []) + (["fine-dining"] if a.get("food") == "fine-dining" else [])


def slots_for(users: list[dict]) -> list[str]:
    slots = ["flight", "stay"]
    for u in users:
        for tag in _wanted(u):
            if f"activity:{tag}" not in slots:
                slots.append(f"activity:{tag}")
    return slots


def label(slot: str) -> str:
    if slot.startswith("activity:"):
        return slot.split(":", 1)[1].replace("-", " ").capitalize()
    return slot.capitalize()


def price(o: dict) -> str:
    unit = {"stay": "/night", "flight": " round-trip"}.get(o["type"], " pp")
    return f"${o['price']:,.0f}{unit}"


def _type(slot: str) -> str:
    return "activity" if slot.startswith("activity:") else slot


def _name(users: list[dict], uid: str) -> str:
    return next((u["name"] for u in users if u["id"] == uid), "The group")


def _plain(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()


def _per_person(trip: dict, plan: dict) -> float:
    try:
        nights = max(1, (date.fromisoformat(trip["end"]) - date.fromisoformat(trip["start"])).days)
    except (KeyError, ValueError):
        nights = 1
    return sum(o["price"] * (nights if o["type"] == "stay" else 1) for o in plan.values() if o)


def _flags(users: list[dict], o: dict) -> list[str]:
    """Budget and dealbreaker conflicts of one option, as short warnings."""
    out = []
    for u in users:
        budget = u.get("budget_per_night")
        if o["type"] == "stay" and budget and o["price"] > budget:
            out.append(f"over {u['name']}'s ${budget}/night")
        for tag in sorted(set(o.get("tags") or []) & set(u["answers"].get("dealbreakers") or [])):
            out.append(f"{tag} is a dealbreaker for {u['name']}")
    return out


def _notes(users: list[dict], st: dict) -> dict[str, list[str]]:
    """Feedback per traveler: what they rejected in this trip, plus rejections remembered from earlier trips."""
    out = {}
    for u in users:
        recalled = [f.split("because:", 1)[1].strip() for f in memory.recall(f"user_{u['id']}", "rejected because")
                    if "because:" in f]
        out[u["id"]] = list(dict.fromkeys(st["notes"].get(u["id"], []) + recalled))
    return out


# ---------------------------------------------------------------- research
def _request(trip: dict, users: list[dict], slot: str) -> dict:
    t = _type(slot)
    return {"type": t, "query": label(slot).lower(), "tag": slot.split(":", 1)[1] if t == "activity" else None,
            "max_price": None, "avoid_tags": [], "origin": users[0].get("home_city") or "",
            "destination": trip["destination"]}


def _research(trip: dict, users: list[dict], slot: str, rnd: int) -> bool:
    st = _STATE[trip["id"]]
    post(Message(rnd, "coordinator", "researcher", "research_request",
                 f"Find {label(slot).lower()} options for {trip['destination']}, {trip['start']} to {trip['end']}"))
    found = [o for o in researcher.research(trip, _request(trip, users, slot)) if o["id"] not in st["rejected"]]
    known = {o["id"] for o in st["options"].setdefault(slot, [])}
    st["options"][slot] += [o for o in found if o["id"] not in known]
    if not st["options"][slot]:
        if _type(slot) != "activity":  # a trip needs a flight and a stay: stop and say so
            raise PlanError(f"No {label(slot).lower()} options found for {trip['destination']}.")
        post(Message(rnd, "researcher", "coordinator", "research_result",
                     f"{label(slot)}: no priced options found for {trip['destination']}, skipping this item"))
        return False
    post(Message(rnd, "researcher", "coordinator", "research_result",
                 f"{label(slot)}: " + "; ".join(f"{o['title']} ({price(o)})" for o in found[:6])
                 + (f"; and {len(found) - 6} more" if len(found) > 6 else ""), [o["id"] for o in found]))
    return True


# ---------------------------------------------------------------- decision (Superlinked)
def _decide(trip: dict, users: list[dict], slot: str, plan: dict, rnd: int) -> None:
    st = _STATE[trip["id"]]
    cands = [o for o in st["options"][slot] if o["id"] not in st["rejected"]]
    if not cands:
        _research(trip, users, slot, rnd)
        cands = [o for o in st["options"][slot] if o["id"] not in st["rejected"]]
        if not cands:
            plan.pop(slot, None)
            post(Message(rnd, "coordinator", "all", "decision", f"{label(slot)}: nothing left to choose from, skipped"))
            return
    ranked = ranking.decide(users, cands, _notes(users, st))
    top = ranked[0]
    names = {u["id"]: u["name"] for u in users}
    post(Message(rnd, "ranker", "coordinator", "proposal",
                 f"{label(slot)}, group fit: " + "; ".join(f"{d.option['title']} {d.score:.0%}" for d in ranked[:3]),
                 [d.option["id"] for d in ranked[:3]]))
    fit = ", ".join(f"{names[uid]} {v:.0%}" for uid, v in top.by_user.items())
    flags = _flags(users, top.option)
    plan[slot] = {**top.option, "why": f"group fit {top.score:.0%} ({fit})" + (f"; warnings: {'; '.join(flags)}" if flags else "")}
    post(Message(rnd, "coordinator", "all", "decision",
                 f"{label(slot)}: {top.option['title']} ({price(top.option)}), group fit {top.score:.0%} ({fit})"
                 + (f". Warnings: {'; '.join(flags)}" if flags else ""), [top.option["id"]]))


# ---------------------------------------------------------------- approval + re-plan
def _who_and_why(text: str, users: list[dict]) -> tuple[str, str]:
    head, sep, tail = text.partition(":")
    if sep:
        for u in users:
            if _plain(head) in (_plain(u["name"]), _plain(u["id"])):
                return u["id"], tail.strip() or text
    return "group", text.strip()


def _announce_plan(trip: dict, plan: dict, rnd: int) -> list[str]:
    items = [(s, o) for s, o in plan.items() if o]
    summary = "; ".join(f"{label(s)}: {o['title']} ({price(o)})" for s, o in items)
    ids = [o["id"] for _, o in items]
    post(Message(rnd, "coordinator", "all", "decision",
                 f"Proposed plan, about ${_per_person(trip, plan):,.0f} per person. {summary}", ids))
    return ids


def _ask_via_human(trip: dict, users: list[dict], ask_human):
    def approve(plan: dict) -> dict | None:
        rnd = _STATE[trip["id"]]["round"]
        _STATE[trip["id"]]["plan"] = dict(plan)  # web/bridge.py shows these items on the approval screen
        items = [(s, o) for s, o in plan.items() if o]
        ids = [o["id"] for _, o in items]
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

    return approve


def replan(trip: dict, users: list[dict], plan: dict, rejection: dict, ask_human=None) -> dict:
    st = _STATE[trip["id"]]
    option = next(o for opts in st["options"].values() for o in opts if o["id"] == rejection["option_id"])
    slot = next(s for s, o in plan.items() if o and o["id"] == option["id"])
    st["rejected"].add(option["id"])
    by = rejection.get("by") or "group"
    fact = f"{_name(users, by)} rejected {option['title']} because: {rejection['reason']}"
    datasets = [f"trip_{trip['id']}"] + ([f"user_{by}"] if by != "group" else [])
    for ds in datasets:
        memory.remember(ds, fact)
        memory.improve(ds)
    for uid in ([by] if by != "group" else [u["id"] for u in users]):
        st["notes"].setdefault(uid, []).append(f"rejected {option['title']} because: {rejection['reason']}")
    st["round"] += 1
    post(Message(st["round"], "coordinator", "all", "decision",
                 f"Re-planning {label(slot).lower()}. Saved to memory ({', '.join(datasets)}): “{fact}”", [option["id"]]))
    new_plan = {s: o for s, o in plan.items() if s != slot}
    _decide(trip, users, slot, new_plan, st["round"])
    return {s: new_plan.get(s) for s in plan}


def _checks(u: dict, plan: dict) -> list[str]:
    out = []
    stay, budget = plan.get("stay"), u.get("budget_per_night")
    if stay and budget:
        out.append(("✓" if stay["price"] <= budget else "✗") + f" stay {price(stay)} (limit ${budget})")
    for d in u["answers"].get("dealbreakers", []):
        out.append(("✗ " if any(d in (o.get("tags") or []) for o in plan.values() if o) else "✓ no ") + d)
    for tag in _wanted(u):
        out.append(("✓ " if plan.get(f"activity:{tag}") else "✗ no ") + tag.replace("-", " "))
    return out


def run_trip(trip: dict, users: list[dict], ask_human=None, approve=None) -> dict:
    if not users:
        raise PlanError("The trip has no travelers yet.")
    st = _STATE[trip["id"]] = {"options": {}, "rejected": set(), "notes": {}, "round": 1}
    if os.getenv("TRANSPORT") == "band":
        import band

        band.assign_seats(users)  # keeps each traveler's Band Advocate seat; ranker/coordinator post as the Coordinator
    approve = approve or _ask_via_human(trip, users, ask_human or _console_human)
    plan: dict = {}
    for slot in slots_for(users):
        if _research(trip, users, slot, 1):
            _decide(trip, users, slot, plan, 1)
    while True:
        _announce_plan(trip, plan, st["round"])
        rejection = approve(plan)
        if rejection is None:
            break
        plan = replan(trip, users, plan, rejection)
    lines = [f"{u['name']}: {', '.join(_checks(u, plan))}" for u in users]
    post(Message(st["round"], "coordinator", "all", "decision",
                 f"Final plan approved, about ${_per_person(trip, plan):,.0f} per person. " + " | ".join(lines),
                 [o["id"] for o in plan.values() if o]))
    memory.remember(f"trip_{trip['id']}", "Final plan: " + "; ".join(f"{label(s)}: {o['title']}" for s, o in plan.items() if o))
    it = itinerary.build_itinerary(trip, users, plan)  # Qwen3.8 27B: itinerary + where-to-stay pick
    st["itinerary"] = it
    rec = it["stay_recommendation"]
    post(Message(st["round"], "coordinator", "all", "decision",
                 f"Itinerary: {it['summary']} Where to stay: {rec['why']}", [rec["option_id"]] if rec["option_id"] else []))
    return plan


def itinerary_for(trip_id: str) -> dict | None:
    return _STATE.get(trip_id, {}).get("itinerary")


# ---------------------------------------------------------------- terminal
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
