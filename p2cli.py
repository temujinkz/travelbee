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
    group-recall "<query>"                 search the group's memory
    group-share  <user_id> "<note>"        add a note to the group's memory (who shared it is recorded)
    group-reject <user_id> "<reason>"      a rejection the WHOLE group agreed on: every Advocate vetoes it

  Negotiation:
    evaluate <user_id> [type]          how this Advocate votes on the current options
    options  [type]                    the options on the table (flight | stay | activity)
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


def _users() -> list[dict]:
    for path in ("demo/users.json", "data/users.json"):
        full = os.path.join(HERE, path)
        if os.path.exists(full):
            with open(full, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else list(data.values())
    from pathlib import Path
    return [json.loads(f.read_text()) for f in sorted((Path(HERE) / "data" / "users").glob("*.json"))]


def _user(uid: str) -> dict:
    for u in _users():
        if u["id"] == uid.lower() or u["name"].lower() == uid.lower():
            return u
    sys.exit(f"unknown user {uid!r}; known: {', '.join(u['id'] for u in _users())}")


def _options(kind: str | None) -> list[dict]:
    """Live options for the first saved trip (data/trips): the same search the Coordinator runs."""
    import researcher
    from pathlib import Path

    trips = sorted((Path(HERE) / "data" / "trips").glob("*.json"))
    if not trips:
        sys.exit("no saved trip: create one in the web app first")
    trip = json.loads(trips[0].read_text())
    home = (_users() or [{}])[0].get("home_city") or ""
    kinds = [kind] if kind else ["flight", "stay", "activity"]
    return [o for k in kinds for o in researcher.research(
        trip, {"type": k, "query": "", "tag": None, "max_price": None, "avoid_tags": [],
               "origin": home, "destination": trip["destination"]})]


def main(argv: list[str]) -> None:
    if len(argv) < 1 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    group = advocate.TRIP_DATASET
    if cmd == "status":
        print("backend:", memory.backend(), "| group dataset:", group)
        return
    if cmd == "group-recall":
        for fact in memory.recall(group, " ".join(args) or "decision plan rejected", top_k=8):
            print(" -", fact)
        print(f"(source: {memory.LAST_SOURCE.get(group, 'dict')})")
        return
    if cmd == "options":
        for o in _options(args[0] if args else None):
            print(f"{o['id']:24s} {o['type']:8s} ${o['price']:>4}  {o['title']}  tags={','.join(o['tags'])}")
        return
    if not args:
        sys.exit(f"{cmd} needs a user id")
    u = _user(args[0])
    advocate.onboard(u)
    ds = f"user_{u['id']}"
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
        for m in advocate.evaluate(u, _options(args[1] if len(args) > 1 else None), 0):
            print(f"{m.type.upper():10s} {m.content}")
    else:
        sys.exit(f"unknown command {cmd!r}\n{__doc__}")


if __name__ == "__main__":
    main(sys.argv[1:])
