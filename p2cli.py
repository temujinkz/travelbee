"""Command line for the Advocate agents that live in Band (Owner: P2).

Each Band Advocate is a Claude Code agent whose instructions (band/advocate.md) tell it to call this tool,
so its answers come from the same memory and the same rules the Coordinator uses.

Run it with the project's Python 3.12 (it has cognee): .venv/bin/python p2cli.py ...

  Individual memory (user_<id>), written only by that traveler's Advocate:
    profile  <user_id>                 questionnaire answers + everything remembered
    recall   <user_id> "<query>"       search that traveler's memory
    remember <user_id> "<fact>"        save a new preference, then improve()
    reject   <user_id> "<reason>"      save a rejection: their Advocate vetoes matching options from now on
    peek     <other_user_id> "<query>" read ANOTHER traveler's memory (read-only), to find common ground

  Group memory (trip_<id>), shared by every Advocate and the Band room:
    group-recall <user_id> "<query>"       search the group's memory (of that traveler's trip)
    group-share  <user_id> "<note>"        add a note to the group's memory (who shared it is recorded)
    group-reject <user_id> "<reason>"      a rejection the WHOLE group agreed on: every Advocate vetoes it

  Negotiation:
    evaluate <user_id> [type]          how this Advocate votes on the current options
    options  <user_id> [type]          the options for that traveler's trip (flight | stay | activity)
    status                             which memory backend is live (dict, cognee-local, cognee-cloud)
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import advocate  # noqa: E402
import memory  # noqa: E402


USERS_DIR = os.path.join(HERE, "data", "users")   # written by web/server.py when someone registers
TRIPS_DIR = os.path.join(HERE, "data", "trips")


def _read_dir(path: str) -> list[dict]:
    out = []
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if name.endswith(".json"):
                try:
                    with open(os.path.join(path, name), encoding="utf-8") as f:
                        out.append(json.load(f))
                except (OSError, ValueError):
                    pass  # being written right now
    return out


def _users() -> list[dict]:
    """Travelers registered in the web app (data/users/*.json). There is no demo data."""
    live = [u for u in _read_dir(USERS_DIR) if u.get("id") and u.get("name")]
    for u in live:
        u.setdefault("answers", {})
        b = u.get("budget_per_night")
        if isinstance(b, float) and b.is_integer():
            u["budget_per_night"] = int(b)
    return live


def trip_of(uid: str) -> dict:
    """The newest trip this traveler belongs to (web app data)."""
    trips = [t for t in _read_dir(TRIPS_DIR) if uid in (t.get("members") or [])]
    if not trips:
        sys.exit(f"no saved trip for traveler {uid!r}: create the trip and register travelers in the web app first")
    t = max(trips, key=lambda t: t.get("created_at") or t.get("id"))
    return {"id": t["id"], "destination": t.get("destination") or "", "start": t.get("start") or "",
            "end": t.get("end") or "", "members": list(t.get("members") or [])}


def _user(uid: str) -> dict:
    for u in _users():
        if u["id"] == uid or u["id"].lower() == uid.lower() or u["name"].lower() == uid.lower():
            return u
    sys.exit(f"unknown traveler {uid!r}; known: {', '.join(u['name'] + ' (' + u['id'] + ')' for u in _users())}")


def _options(kind: str | None, uid: str | None = None) -> list[dict]:
    """Live options for the traveler's trip: the same searches the Coordinator runs."""
    import researcher

    if not uid:
        sys.exit("options needs a traveler id: it searches that traveler's trip")
    trip = trip_of(uid)
    home = _user(uid).get("home_city") or ""
    kinds = [kind] if kind else ["flight", "stay", "activity"]
    out = []
    for k in kinds:
        try:
            out += researcher.research(trip, {"type": k, "query": k, "tag": None, "max_price": None, "avoid_tags": [],
                                              "origin": home, "destination": trip["destination"]})
        except Exception as exc:  # a live search failing must not kill the agent's tool
            print(f"(research for {k} failed: {exc})")
    return out


def main(argv: list[str]) -> None:
    if len(argv) < 1 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    if cmd == "status":
        print("backend:", memory.backend(), "| travelers:", ", ".join(f"{u['name']} ({u['id']})" for u in _users()))
        return
    if cmd == "options":
        uid = _user(args[0])["id"] if args else None
        for o in _options(args[1] if len(args) > 1 else None, uid):
            print(f"{o['id']:24s} {o['type']:8s} ${o['price']:>4}  {o['title']}  tags={','.join(o['tags'])}")
        return
    if not args:
        sys.exit(f"{cmd} needs a user id")
    u = _user(args[0])
    advocate.onboard(u)
    ds = f"user_{u['id']}"
    group = advocate.trip_dataset(u["id"])
    if cmd == "group-recall":
        for fact in memory.recall(group, " ".join(args[1:]) or "decision plan rejected", top_k=8):
            print(" -", fact)
        print(f"(group memory {group}, source: {memory.LAST_SOURCE.get(group, 'dict')})")
        return
    if cmd == "profile":
        print(json.dumps({k: u[k] for k in ("id", "name", "budget_per_night", "home_city", "answers") if k in u},
                         ensure_ascii=False, indent=1))
        print(f"\nMemory ({memory.backend()}, {ds}):")
        for fact in memory.dump(ds):
            print(" -", fact)
    elif cmd == "recall":
        for fact in memory.recall(ds, " ".join(args[1:]) or "preferences", top_k=8):
            print(" -", fact)
        print(f"(source: {memory.LAST_SOURCE.get(ds, 'dict')})")
    elif cmd in ("remember", "reject"):
        text = " ".join(args[1:]).strip()
        if not text:
            sys.exit(f"{cmd} needs some text")
        fact = f"{u['name']} rejected (said in Band chat) because: {text}" if cmd == "reject" else f"{u['name']}: {text}"
        memory.remember(ds, fact)
        memory.improve(ds)
        print("saved:", fact)
        if cmd == "reject":
            tags = sorted(advocate._tags_in(text))
            print("will veto options tagged:", ", ".join(tags) if tags else "(no known tag matched; add the tag word)")
    elif cmd == "peek":
        for fact in memory.recall(ds, " ".join(args[1:]) or "preferences dealbreakers budget", top_k=8):
            print(" -", fact)
        print(f"(read-only view of {u['name']}'s memory, source: {memory.LAST_SOURCE.get(ds, 'dict')})")
    elif cmd in ("group-share", "group-reject"):
        text = " ".join(args[1:]).strip()
        if not text:
            sys.exit(f"{cmd} needs some text")
        if cmd == "group-reject":
            fact = f"The group rejected (agreed in the Band room, raised by {u['name']}'s Advocate) because: {text}"
        else:
            fact = f"{u['name']}'s Advocate shared with the group: {text}"
        memory.remember(group, fact)
        memory.improve(group)
        print("saved to group memory:", fact)
        if cmd == "group-reject":
            tags = sorted(advocate._tags_in(text))
            print("every Advocate will veto options tagged:", ", ".join(tags) if tags else "(no known tag matched)")
    elif cmd == "evaluate":
        for m in advocate.evaluate(u, _options(args[1] if len(args) > 1 else None, u["id"]), 0):
            print(f"{m.type.upper():10s} {m.content}")
    else:
        sys.exit(f"unknown command {cmd!r}\n{__doc__}")


if __name__ == "__main__":
    main(sys.argv[1:])
