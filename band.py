"""Band transport. Owner: P3.

With TRANSPORT=band, every protocol.post() is also sent to the trip's Band room, posted by the agent that
sent it (its own Band identity) and @mentioning the recipient. LOG stays the source of truth: sending runs in
a background thread, and a Band failure never stops a run.

Setup, in .env (see .env.example):
    TRANSPORT=band
    BAND_ROOM_ID=<chat room id>                  `python band.py` lists each agent's rooms with their IDs
    BAND_COORDINATOR_KEY, BAND_RESEARCHER_KEY, BAND_ADVOCATE_1_KEY ... BAND_ADVOCATE_4_KEY (any order)
    Each key's agent is looked up with GET /agent/me. A traveler's messages are posted by the Advocate whose
    name contains the traveler's name (e.g. "Léa's Advocate"); travelers without one get a free Advocate in order.
    BAND_REST_URL=https://app.band.ai/api/v1
    BAND_DRY_RUN=1                                print the requests instead of sending them

    python band.py          check keys, room, and which Advocate speaks for which demo traveler
    python band.py hello    the Coordinator posts a test message to the room
"""
from __future__ import annotations

import json
import os
import queue
import ssl
import sys
import threading
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

ROLES = ["coordinator", "researcher", "advocate_1", "advocate_2", "advocate_3", "advocate_4"]

_seats: dict[str, str] = {}
_names: dict[str, str] = {}               # "advocate_<user id>" -> traveler name, for relayed messages
_personal: dict[str, dict] = {}           # "advocate_<user id>" -> that traveler's own Band agent {id, name, api_key}
_identities: dict[str, dict] | None = None  # role -> {"id", "name", "handle"} from GET /agent/me
_participants: list[dict] | None = None
_queue: queue.Queue = queue.Queue()
_worker: threading.Thread | None = None
_errors = 0


def _ssl_context() -> ssl.SSLContext:
    """python.org Python on macOS ships without system CA certs; use certifi's bundle when available."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL = _ssl_context()


def _dry() -> bool:
    return os.getenv("BAND_DRY_RUN") == "1"


def _room() -> str | None:
    return os.getenv("BAND_ROOM_ID")


def _key(role: str) -> str | None:
    if role in _personal:
        return _personal[role]["api_key"]
    return os.getenv(f"BAND_{role.upper()}_KEY") or ("dry-run" if _dry() else None)


def _plain(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().strip()


def _http(method: str, path: str, key: str, body: dict | None = None):
    url = os.getenv("BAND_REST_URL", "https://app.band.ai/api/v1").rstrip("/") + path
    if _dry():
        print(f"[band dry-run] {method} {path} {json.dumps(body, ensure_ascii=False) if body else ''}", flush=True)
        return None
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "X-API-Key": key, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10, context=_SSL) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"{method} {url} -> HTTP {exc.code}: {detail}") from None


def _body(data):
    return data.get("data", data) if isinstance(data, dict) else data


# ---------------------------------------------------------------- who is who
def identities() -> dict[str, dict]:
    """Ask Band who each configured key is (GET /agent/me), once."""
    global _identities
    if _identities is None:
        found = {}
        for role in ROLES:
            key = _key(role)
            if not key:
                continue
            if _dry():
                name = role.replace("_", " ").title()
                found[role] = {"id": f"dry-{role}", "name": name, "handle": f"team/{role}"}
                continue
            try:
                me = _body(_http("GET", "/agent/me", key)) or {}
                found[role] = {"id": me.get("id"), "name": me.get("name"), "handle": me.get("handle")}
            except Exception as exc:
                _report(exc)
        _identities = found
    return _identities


# ---------------------------------------------------------------- one Band agent per traveler
AGENTS_FILE = Path(__file__).resolve().parent / "data" / "band_agents.json"  # holds agent API keys: git-ignored


def _load_agents() -> dict:
    try:
        return json.loads(AGENTS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _describe(u: dict) -> str:
    a = u.get("answers") or {}
    bits = [f"Travelbee Advocate for {u['name']}."]
    if u.get("budget_per_night"):
        bits.append(f"Budget ${u['budget_per_night']:.0f}/night ({a.get('budget') or 'unspecified'}).")
    if a.get("activities"):
        bits.append("Loves: " + ", ".join(a["activities"]) + ".")
    if a.get("food") or a.get("lodging") or a.get("pace"):
        bits.append(f"Food: {a.get('food') or '-'}, lodging: {a.get('lodging') or '-'}, pace: {a.get('pace') or '-'}.")
    if a.get("dealbreakers"):
        bits.append("Dealbreakers: " + ", ".join(a["dealbreakers"]) + ".")
    return " ".join(bits)[:500]


_agents_lock = threading.Lock()


def ensure_agent(u: dict) -> dict | None:
    """Create "<Name>'s Advocate" in Band for this traveler (once), add it to the room, remember its key.
    Needs BAND_USER_API_KEY (a band_u_ key). Returns None, and changes nothing, if that isn't possible."""
    user_key, room = os.getenv("BAND_USER_API_KEY"), _room()
    with _agents_lock:
        store = _load_agents()
        if u["id"] in store:
            return store[u["id"]]
        if not user_key or not room or _dry():
            return None
        try:
            res = _body(_http("POST", "/me/agents/register", user_key,
                              {"agent": {"name": f"{u['name']}'s Advocate"[:24], "description": _describe(u)}})) or {}
            agent, creds = res.get("agent") or {}, res.get("credentials") or {}
            if not agent.get("id") or not creds.get("api_key"):
                raise RuntimeError(f"unexpected register response: {str(res)[:200]}")
            entry = {"id": agent["id"], "name": agent.get("name"), "handle": agent.get("handle"), "api_key": creds["api_key"]}
            store[u["id"]] = entry
            AGENTS_FILE.parent.mkdir(exist_ok=True)
            AGENTS_FILE.write_text(json.dumps(store, indent=2))
            os.chmod(AGENTS_FILE, 0o600)
        except Exception as exc:
            _report(RuntimeError(f"could not create a Band agent for {u['name']} ({exc}); their messages go through the Coordinator"))
            return None
    try:  # our Coordinator adds its sibling agent to the room
        _http("POST", f"/agent/chats/{room}/participants", _key("coordinator"),
              {"participant": {"participant_id": entry["id"], "role": "member"}})
    except Exception as exc:
        _report(RuntimeError(f"created {entry['name']} but could not add it to the room ({exc})"))
    return entry


def use_personal_agents(users: list[dict]) -> None:
    """Map each traveler to their own Band agent (creating it if needed)."""
    _personal.clear()
    for u in users:
        entry = ensure_agent(u)
        if entry:
            _personal[f"advocate_{u['id']}"] = entry


def assign_seats(users: list[dict]) -> None:
    """A traveler speaks through the Band Advocate named after them (e.g. "Léa's Advocate"). Everyone else is
    relayed by the Coordinator under their own name, so any number of travelers works and nobody gets a wrong name."""
    _seats.clear()
    _names.clear()
    use_personal_agents(users)
    ids = identities()
    free = [r for r in ROLES[2:] if r in ids]
    for u in users:
        _names[f"advocate_{u['id']}"] = u["name"]
        if f"advocate_{u['id']}" in _personal:
            continue
        for role in free:
            if _plain(u["name"]) and _plain(u["name"]) in _plain(ids.get(role, {}).get("name", "")):
                _seats[f"advocate_{u['id']}"] = role
                free.remove(role)
                break


def _role(agent_id: str) -> str:
    if agent_id in ("coordinator", "researcher"):
        return agent_id
    if agent_id in _personal:  # the traveler's own agent, created from their questionnaire
        return agent_id
    # a Band Advocate named after them if there is one; otherwise (and for "human") the Coordinator relays it
    return _seats.get(agent_id, "coordinator")


def participants(refresh: bool = False) -> list[dict]:
    global _participants
    if _participants is None or refresh:
        if _dry():
            _participants = [{"id": "dry-human", "name": "Traveler", "handle": "traveler", "type": "User"}]
        else:
            rows = _body(_http("GET", f"/agent/chats/{_room()}/participants", _key("coordinator")))
            _participants = rows if isinstance(rows, list) else []
    return _participants


def _mentions(msg, sender_role: str) -> list[dict]:
    ids = {**identities(), **_personal}
    sender_id = ids.get(sender_role, {}).get("id")
    if msg.to in ("human", "all"):
        targets = [p for p in participants() if p.get("type") == "User"]
    elif msg.to == "advocates":  # only the Band Advocates of this trip's travelers
        targets = [ids[r] for r in sorted(set(_seats.values())) if r in ids] + list(_personal.values())
    else:
        role = _role(msg.to)
        targets = [ids[role]] if role in ids else []
    targets = [t for t in targets if t.get("id") and t.get("id") != sender_id]
    if not targets:  # Band needs at least one mention of someone other than the sender
        # prefer our own Researcher agent (no live session, so the mention pings nobody), then a human
        quiet = [ids[r] for r in ("researcher", "coordinator") if r in ids and ids[r].get("id") != sender_id]
        people = [p for p in participants() if p.get("id") != sender_id]
        targets = (quiet or [p for p in people if p.get("type") == "User"] or people)[:1]
    return [{"id": t["id"], "name": t.get("name"), "handle": t.get("handle")} for t in targets]


def _content(msg, mentions: list[dict]) -> str:
    who = " ".join(f"@{m['name']}" for m in mentions)
    head = f"[{msg.type} · R{msg.round}]"
    if msg.from_ == "human":
        head += " Humans said:"
    elif msg.from_.startswith("advocate_") and msg.from_ not in _seats and msg.from_ not in _personal:  # relayed by the Coordinator
        head += f" {_names.get(msg.from_, 'A traveler')}'s Advocate:"
    refs = f" (refs: {', '.join(msg.refs)})" if msg.refs else ""
    return f"{who} {head} {msg.content}{refs}".strip()


# ---------------------------------------------------------------- sending
def send(msg) -> None:
    """Queue `msg` for the Band room. Non-blocking; a single worker keeps messages in order."""
    global _worker
    if _worker is None:
        _worker = threading.Thread(target=_run, name="band-sender", daemon=True)
        _worker.start()
    _queue.put(msg)


def flush() -> None:
    """Wait until every queued message was sent (CLI runs call this before exiting)."""
    if _worker is not None:
        _queue.join()


def _run() -> None:
    while True:
        msg = _queue.get()
        try:
            _deliver(msg)
        except Exception as exc:
            _report(exc)
        finally:
            _queue.task_done()


def _deliver(msg) -> None:
    role = _role(msg.from_)
    key = _key(role) if role else None
    if not _room() or not key:
        raise RuntimeError(f"set BAND_ROOM_ID and BAND_{(role or msg.from_).upper()}_KEY")
    mentions = _mentions(msg, role)
    _http("POST", f"/agent/chats/{_room()}/messages", key,
          {"message": {"content": _content(msg, mentions), "mentions": mentions}})


def _report(exc: Exception) -> None:
    global _errors
    _errors += 1
    if _errors <= 3:
        print(f"[band] {exc}. LOG is unaffected.", file=sys.stderr, flush=True)
    elif _errors == 4:
        print("[band] more errors, muting further ones", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- setup check
def check() -> None:
    room = _room()
    print(f"REST base: {os.getenv('BAND_REST_URL', 'https://app.band.ai/api/v1')}   room: {room or 'NOT SET'}")
    ids = identities()
    for role in ROLES:
        key = os.getenv(f"BAND_{role.upper()}_KEY")
        if not key:
            print(f"  ✗ {role:12s} BAND_{role.upper()}_KEY not set")
            continue
        if role not in ids:
            print(f"  ✗ {role:12s} key rejected by GET /agent/me")
            continue
        try:
            chats = _body(_http("GET", "/agent/chats?page_size=100", key)) or []
            in_room = any(c.get("id") == room for c in chats)
            rooms = ", ".join(f"{c.get('title') or '(untitled)'} = {c.get('id')}" for c in chats[:5])
            print(f"  {'✓' if in_room else '✗'} {role:12s} is “{ids[role]['name']}”; in room: {in_room}. Its rooms: {rooms or 'none'}")
        except Exception as exc:
            print(f"  ✗ {role:12s} {exc}")
    if room and os.getenv("BAND_COORDINATOR_KEY"):
        try:
            people = [p for p in participants(refresh=True) if p.get("type") == "User"]
            print("Humans in the room (pinged on human requests and decisions):", ", ".join(p.get("name", "?") for p in people) or "none")
        except Exception as exc:
            print(f"  ✗ listing participants: {exc}")
    import mocks

    assign_seats(mocks.USERS)
    for u in mocks.USERS:
        role = _seats.get(f"advocate_{u['id']}")
        print(f"  demo traveler {u['name']:6s} → {ids.get(role, {}).get('name', role or 'no Advocate')}")


if __name__ == "__main__":
    import protocol  # loads .env

    if sys.argv[1:] == ["hello"]:
        send(protocol.Message(0, "coordinator", "human", "decision", "Hello from the Travelbee Coordinator 👋"))
        flush()
        print("sent (check the room)" if _errors == 0 else "failed, see above")
    else:
        check()
