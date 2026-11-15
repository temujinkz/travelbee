"""Bridge from the web app (server.py) to the real agents (coordinator.run_trip).

server.py calls run(trip_id, server_module) from its planning thread. We:
  - load the trip and its travelers from data/ (the questionnaire answers are the Users the agents see;
    each Advocate writes its traveler's answers into memory on first use),
  - copy every agent message into the trip's web log as it is posted (protocol.LISTENERS),
  - turn the coordinator's ask_human(question, choices) into the web app's question boxes:
      deadlock -> "choice" buttons; approval -> Approve / Reject an item + reason.
    A reason written as "Ben: no layovers" is attributed to Ben, so his Advocate remembers it.
Band mirroring (TRANSPORT=band) keeps working, since it hangs off the same protocol.post().
"""
from __future__ import annotations

import os
import secrets
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PACE_S", "0.45")  # readable live log, like the old mock

import coordinator  # noqa: E402
import protocol  # noqa: E402

_one_at_a_time = threading.Lock()  # protocol.LOG is shared by the whole process


def summarize(trip: dict, users: list[dict], plan: dict) -> dict:
    """What the plan means for each traveler: whose flight is whose, the stay over all nights, activities,
    the cost per person and for the group, and each traveler's checks."""
    from datetime import date

    try:
        nights = max(1, (date.fromisoformat(trip["end"]) - date.fromisoformat(trip["start"])).days)
    except (KeyError, ValueError):
        nights = 1
    st = coordinator._STATE.get(trip["id"], {})
    flights, stay, acts = [], None, []
    for slot, o in plan.items():
        if not o:
            continue
        item = {"id": o["id"], "title": o["title"], "price": o["price"], "url": o.get("url", ""), "slot": slot}
        if o["type"] == "flight":
            who = [u for u in users if coordinator._mine(u, slot)] if slot.startswith("flight:") else list(users)
            flights.append(dict(item, travelers=[u["name"] for u in who],
                                origin=(who[0].get("home_city") if who else "") or ""))
        elif o["type"] == "stay":
            stay = dict(item, nights=nights, total=round(o["price"] * nights, 2))
        else:
            names = {sl: lb for sl, lb in (trip.get("activities") or [])}  # keep accents: "Belém Tower visit"
            acts.append(dict(item, label=names.get(slot.split(":", 1)[-1]) or coordinator.label(slot)))
    people = []
    for u in users:
        mine = next((f for f in flights if u["name"] in f["travelers"]), None)
        cost = (mine["price"] if mine else 0) + (stay["total"] if stay else 0) + sum(a["price"] for a in acts)
        checks = coordinator._checks(u, plan, st) if st else []
        people.append({"name": u["name"], "home_city": u.get("home_city") or "", "flight": mine["title"] if mine else None,
                       "total": round(cost, 2), "checks": checks,
                       "happy": bool(checks) and all(not c.startswith("✗") for c in checks)})
    return {"nights": nights, "flights": flights, "stay": stay, "activities": acts, "people": people,
            "group_total": round(sum(p["total"] for p in people), 2),
            "per_person": round(sum(p["total"] for p in people) / max(1, len(people)), 2)}


def _wait(s, tid: str, kind: str, question: str, choices=None, plan=None, voters=None, summary=None) -> dict:
    """Open a question box for the organizer and wait for the answer (server.ask_human without its log line,
    because the coordinator already posts the human_request itself)."""
    qid = secrets.token_hex(4)
    ev = threading.Event()
    s.ANSWERS[tid] = {"qid": qid, "event": ev, "answer": None}
    if (s.load_trip(tid) or {}).get("status") == "cancelled":
        raise RuntimeError("trip cancelled")
    s.set_state(tid, status="awaiting", pending={"id": qid, "to": "group", "voters": voters or [], "votes": {},
                                                  "kind": kind, "question": question, "choices": choices,
                                                  "plan": plan, "summary": summary})
    answered = ev.wait(1800)
    ans = s.ANSWERS.get(tid, {}).get("answer") if answered else None
    if (ans or {}).get("cancel") or (s.load_trip(tid) or {}).get("status", "cancelled") == "cancelled":
        raise RuntimeError("trip cancelled")  # leave the trip cancelled; server.run_plan stays quiet
    s.set_state(tid, status="planning", pending=None)
    if not answered:
        raise TimeoutError()
    return ans


def run(tid: str, s) -> None:
    if not _one_at_a_time.acquire(blocking=False):
        raise RuntimeError("another trip is being planned right now; try again in a minute")
    try:
        _run(tid, s)
    finally:
        _one_at_a_time.release()


def _run(tid: str, s) -> None:
    t = s.load_trip(tid)
    users = [u for u in (s.load_user(m) for m in t["members"]) if u]
    for u in users:  # the web app stores 100.0; agents should say "$100", not "$100.0"
        b = u.get("budget_per_night")
        if isinstance(b, float) and b.is_integer():
            u["budget_per_night"] = int(b)
    names = {u["id"]: u["name"] for u in users}
    trip = {"id": tid, "destination": t.get("destination") or "", "start": t.get("start") or "",
            "end": t.get("end") or "", "members": [u["id"] for u in users],
            "activities": t.get("activities") or []}  # the destination's activity list (plan B alternatives)

    def who(agent: str) -> str:
        if agent.startswith("advocate_"):
            return "advocate:" + names.get(agent[len("advocate_"):], "traveler")
        if agent == "human":
            return "human:the group"  # decisions are group votes now
        return agent

    def copy_to_web_log(msg) -> None:
        s.add_log(tid, msg.round, who(msg.from_), who(msg.to), msg.type, msg.content, msg.refs, sleep=False)

    pending_reason: dict[str, str] = {}

    def ask_human(question: str, choices: list[str]) -> str:
        if not choices:  # the coordinator asks "who and why" right after a rejection
            return pending_reason.pop("text", "no reason given")
        if choices[0] == "Approve":
            plan = coordinator._STATE.get(tid, {}).get("plan") or {}
            items = [dict(o, slot=slot) for slot, o in plan.items() if o]
            ans = _wait(s, tid, "approval", "Here is the plan. Everyone votes: approve it, or reject one item "
                        "with a reason. The majority decides, and the agents remember the reasons.",
                        plan=items, voters=[u["id"] for u in users], summary=summarize(trip, users, plan))
            if ans.get("choice") != "reject":
                return "Approve"
            slot = next((i["slot"] for i in items if i["id"] == ans.get("item_id")), None)
            if slot is None:
                return "Approve"
            pending_reason["text"] = ans.get("reason") or "no reason given"
            s.set_state(tid, plan=items)
            return f"Reject {coordinator.label(slot).lower()}"
        ans = _wait(s, tid, "choice", question, choices=[{"value": c, "label": c} for c in choices],
                    voters=[u["id"] for u in users])
        return ans.get("choice", choices[0])

    protocol.clear_log()
    protocol.LISTENERS.append(copy_to_web_log)
    try:
        plan = coordinator.run_trip(trip, users, ask_human)
    finally:
        protocol.LISTENERS.remove(copy_to_web_log)
    if (s.load_trip(tid) or {}).get("status") == "cancelled":
        return
    s.set_state(tid, plan=[o for o in plan.values() if o], summary=summarize(trip, users, plan),
                status="done", pending=None)
