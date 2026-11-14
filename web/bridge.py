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


def _wait(s, tid: str, kind: str, question: str, choices=None, plan=None) -> dict:
    """Open a question box for the organizer and wait for the answer (server.ask_human without its log line,
    because the coordinator already posts the human_request itself)."""
    qid = secrets.token_hex(4)
    ev = threading.Event()
    s.ANSWERS[tid] = {"qid": qid, "event": ev, "answer": None}
    s.set_state(tid, status="awaiting", pending={"id": qid, "to": "organizer", "kind": kind,
                                                  "question": question, "choices": choices, "plan": plan})
    answered = ev.wait(1800)
    s.set_state(tid, status="planning", pending=None)
    if not answered:
        raise TimeoutError()
    return s.ANSWERS[tid]["answer"]


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
            "end": t.get("end") or "", "members": [u["id"] for u in users]}

    def who(agent: str) -> str:
        if agent.startswith("advocate_"):
            return "advocate:" + names.get(agent[len("advocate_"):], "traveler")
        if agent == "human":
            return "human:organizer"
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
            ans = _wait(s, tid, "approval", "Here is the plan. Approve it, or reject one item with a reason "
                        "(start with a name, e.g. “Ben: no layovers”, so that traveler's agent remembers it).",
                        plan=items)
            if ans.get("choice") != "reject":
                return "Approve"
            slot = next((i["slot"] for i in items if i["id"] == ans.get("item_id")), None)
            if slot is None:
                return "Approve"
            pending_reason["text"] = ans.get("reason") or "no reason given"
            s.set_state(tid, plan=items)
            return f"Reject {coordinator.label(slot).lower()}"
        ans = _wait(s, tid, "choice", question, choices=[{"value": c, "label": c} for c in choices])
        return ans.get("choice", choices[0])

    protocol.clear_log()
    protocol.LISTENERS.append(copy_to_web_log)
    try:
        plan = coordinator.run_trip(trip, users, ask_human)
    finally:
        protocol.LISTENERS.remove(copy_to_web_log)
    s.set_state(tid, plan=[o for o in plan.values() if o], status="done", pending=None)
