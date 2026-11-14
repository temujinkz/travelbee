"""Command line for the Advocate agents that live in Band (Owner: P2).

Each Band Advocate is a Claude Code agent whose instructions (band/advocate.md) tell it to call this tool,
so its answers come from the same memory and the same rules the Coordinator uses.

    python3 p2cli.py profile  <user_id>              questionnaire answers + everything remembered
    python3 p2cli.py recall   <user_id> "<query>"    memory search
    python3 p2cli.py remember <user_id> "<fact>"     save a new preference, then improve()
    python3 p2cli.py reject   <user_id> "<reason>"   save a rejection: future negotiations veto matching options
    python3 p2cli.py evaluate <user_id> [type]       how this Advocate votes on the current options
    python3 p2cli.py options  [type]                 the options on the table (flight | stay | activity)
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
    import mocks
    return mocks.USERS


def _user(uid: str) -> dict:
    for u in _users():
        if u["id"] == uid.lower() or u["name"].lower() == uid.lower():
            return u
    sys.exit(f"unknown user {uid!r}; known: {', '.join(u['id'] for u in _users())}")


def _options(kind: str | None) -> list[dict]:
    try:
        import researcher
        research = researcher.research
    except ModuleNotFoundError:
        import mocks
        research = mocks.research
    import mocks
    trip = mocks.TRIP
    kinds = [kind] if kind else ["flight", "stay", "activity"]
    return [o for k in kinds for o in research(trip, {"type": k, "query": "", "max_price": None, "avoid_tags": []})]


def main(argv: list[str]) -> None:
    if len(argv) < 1 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
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
    elif cmd == "evaluate":
        for m in advocate.evaluate(u, _options(args[1] if len(args) > 1 else None), 0):
            print(f"{m.type.upper():10s} {m.content}")
    else:
        sys.exit(f"unknown command {cmd!r}\n{__doc__}")


if __name__ == "__main__":
    main(sys.argv[1:])
