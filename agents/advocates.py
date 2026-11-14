"""Live Advocates: every traveler who registers in the web app gets their own Band agent, with shared Cognee memory.
Owner: P2. Run it on the computer that runs web/server.py (it reads the app's data/users and data/trips):

    .venv/bin/python agents/advocates.py

Every few seconds it looks at data/users/*.json. For each new traveler it
  1. finds their Band Advocate: data/advocates.json (created earlier), else an existing agent named
     "<Name>'s Advocate" among BAND_ADVOCATE_n_KEY, else registers a new one with BAND_USER_API_KEY
     (POST /me/agents/register) and saves its credentials to data/advocates.json (git-ignored),
  2. adds it to the Band room (BAND_ROOM_ID),
  3. writes the traveler's questionnaire into the shared room memory (Cognee),
  4. starts it: band-sdk's ClaudeSDKAdapter (Claude, through `claude login`, no API key) wrapped in
     cognee-integration-band's CogneeMemoryAdapter (pattern of github.com/jure-cognee/band-cognee-workshop):
     auto-recall before each message, auto-capture after each reply, rooms consolidated into the graph.
Tools: cognee_search / cognee_remember, plus the P2 tools on that traveler's live profile and trip
(user_<id> = the traveler's memory, trip_<trip id> = the group's memory).

.env: COGNEE_SERVICE_URL, COGNEE_API_KEY, BAND_USER_API_KEY, BAND_ROOM_ID (optional: BAND_ADVOCATE_n_KEY,
COGNEE_BAND_DATASET=travelbee_rooms, ADVOCATE_MODEL=sonnet, P2_DEMO=1 to serve the four demo travelers instead).
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.request

# python.org Python on macOS has no system CA certificates: point every HTTPS client (urllib, band-sdk's
# websocket, cognee) at certifi's bundle before anything opens a connection.
try:
    import certifi as _certifi

    os.environ.setdefault("SSL_CERT_FILE", _certifi.where())
except ImportError:
    pass


# The repo has a band.py (P3's transport) that would shadow the band-sdk package if the repo root came first.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != ROOT] + [ROOT]

import memory  # noqa: E402  (also loads ROOT/.env into the environment)

if os.getenv("COGNEE_SERVICE_URL") and not os.getenv("COGNEE_BASE_URL"):
    os.environ["COGNEE_BASE_URL"] = os.environ["COGNEE_SERVICE_URL"]
os.environ.setdefault("COGNEE_BAND_DATASET", "travelbee_rooms")
os.environ.setdefault("P2_DEMO", "0")  # live travelers only: nobody registered = no Advocate running

from band import Agent, configure_logging  # noqa: E402
from band.adapters import ClaudeSDKAdapter, ClaudeSDKAdapterConfig  # noqa: E402
from cognee_band import CogneeClient, CogneeMemoryAdapter, CogneeSettings, cognee_tools  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import p2cli  # noqa: E402

BAND_REST = os.getenv("BAND_REST_URL", "https://app.band.ai/api/v1").rstrip("/")
CREDS_FILE = os.path.join(ROOT, "data", "advocates.json")
POLL_S = float(os.getenv("ADVOCATES_POLL_S") or 5)


def _plain(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().strip()


# ---------------------------------------------------------------- Band REST
def _band(method: str, path: str, key: str, body: dict | None = None):
    req = urllib.request.Request(BAND_REST + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-API-Key": key, "Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}") from None
    data = json.loads(raw) if raw else {}
    return data.get("data", data) if isinstance(data, dict) else data


def _load_creds() -> dict:
    try:
        with open(CREDS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_creds(creds: dict) -> None:
    os.makedirs(os.path.dirname(CREDS_FILE), exist_ok=True)
    tmp = CREDS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(creds, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CREDS_FILE)


_legacy: list[dict] | None = None


def _legacy_agents() -> list[dict]:
    """Agents already configured as BAND_ADVOCATE_n_KEY (e.g. "Léa's Advocate"), reused for a traveler of that name."""
    global _legacy
    if _legacy is None:
        _legacy = []
        for k in sorted(os.environ):
            if k.startswith("BAND_ADVOCATE_") and k.endswith("_KEY") and os.environ[k]:
                try:
                    me = _band("GET", "/agent/me", os.environ[k])
                    _legacy.append({"agent_id": me["id"], "api_key": os.environ[k], "name": me.get("name", ""),
                                    "handle": me.get("handle", "")})
                except Exception as exc:
                    print(f"[advocates] {k} rejected by Band: {exc}", flush=True)
    return _legacy


def ensure_agent(user: dict, creds: dict) -> dict | None:
    """The traveler's Band Advocate credentials, creating the agent if needed."""
    uid, name = user["id"], user["name"]
    if uid in creds:
        return creds[uid]
    taken = {c["agent_id"] for c in creds.values()}
    for a in _legacy_agents():
        if a["agent_id"] not in taken and _plain(a["name"]) == _plain(f"{name}'s Advocate"):
            creds[uid] = dict(a, traveler=name)
            _save_creds(creds)
            return creds[uid]
    user_key = os.getenv("BAND_USER_API_KEY")
    if not user_key:
        print(f"[advocates] {name} registered, but BAND_USER_API_KEY is not set: cannot create their Band agent",
              flush=True)
        return None
    agent_name = f"{name}'s Advocate"[:100]
    if len(agent_name) < 3:
        agent_name = f"Advocate {name}"
    reg = _band("POST", "/me/agents/register", user_key, {"agent": {
        "name": agent_name,
        "description": f"Travelbee: {name}'s trip advocate. Defends their preferences in the group negotiation "
                       f"and remembers their feedback (Cognee memory)."[:500]}})
    agent, key = reg["agent"], reg["credentials"]["api_key"]
    creds[uid] = {"agent_id": agent["id"], "api_key": key, "name": agent.get("name", agent_name),
                  "handle": agent.get("handle", ""), "traveler": name}
    _save_creds(creds)
    print(f"[advocates] created Band agent {agent.get('name')} for {name}", flush=True)
    return creds[uid]


def add_to_room(c: dict) -> None:
    room, user_key = os.getenv("BAND_ROOM_ID"), os.getenv("BAND_USER_API_KEY")
    if not room or not user_key or c.get("room") == room:
        return
    try:
        _band("POST", f"/me/chats/{room}/participants", user_key,
              {"participant": {"participant_id": c["agent_id"], "role": "member"}})
    except RuntimeError as exc:
        if "409" not in str(exc) and "already" not in str(exc).lower():
            print(f"[advocates] could not add {c['name']} to the room: {exc}", flush=True)
            return
    c["room"] = room


# ---------------------------------------------------------------- tools + prompt
def _cli(*args: str) -> str:
    """Run one p2cli command in-process and return what it printed."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            p2cli.main(list(args))
        except SystemExit as exc:
            if exc.code not in (None, 0):
                print(exc.code)
    return out.getvalue().strip() or "(no output)"


def p2_tools(uid: str) -> list:
    """P2 tools bound to one traveler. Tool name comes from the class name (FooBarInput -> foo_bar)."""

    class TravelerProfileInput(BaseModel):
        """Your traveler's questionnaire answers (budget, pace, food, lodging, activities, dealbreakers) and
        everything remembered about them. Call it at the start of a conversation."""

    class TravelerRecallInput(BaseModel):
        """Search your traveler's own memory (preferences, past rejections, concessions)."""
        query: str = Field(description="What to look for")

    class TravelerRememberInput(BaseModel):
        """Save a NEW preference your traveler told you (only when the speaker speaks for your traveler)."""
        fact: str = Field(description="The preference, self-contained, e.g. 'would love a pool'")

    class TravelerRejectInput(BaseModel):
        """Save something your traveler does not want ('no early flights', 'no layovers'). From now on you veto
        every option it matches. Also use it when the Coordinator announces that your traveler rejected an item."""
        reason: str = Field(description="The reason in the traveler's words")

    class PeekTravelerInput(BaseModel):
        """Read ANOTHER traveler's memory (read-only) to understand them and find common ground."""
        traveler: str = Field(description="The other traveler's name or id")
        query: str = Field(description="What to look for")

    class GroupRecallInput(BaseModel):
        """Search the group's shared memory: decisions, group rejections, notes other Advocates shared."""
        query: str = Field(description="What to look for")

    class GroupShareInput(BaseModel):
        """Share a note with every Advocate (a compromise your traveler accepts, an agreement reached in the room)."""
        note: str = Field(description="The note, self-contained")

    class GroupRejectInput(BaseModel):
        """ONLY when the whole room clearly agreed to rule something out: every Advocate will veto it."""
        reason: str = Field(description="What the group rules out and why")

    class EvaluateOptionsInput(BaseModel):
        """Your votes and vetoes, with reasons, on the options for your trip. Re-run after any memory change."""
        option_type: str = Field(default="", description="flight, stay, activity, or empty for all")

    class ListOptionsInput(BaseModel):
        """The options for your trip (id, type, price, title, tags)."""
        option_type: str = Field(default="", description="flight, stay, activity, or empty for all")

    def wrap(*argv):
        async def run(inp) -> str:
            args = [a.format(**inp.model_dump()) for a in argv]
            return await asyncio.to_thread(_cli, *[a for a in args if a != ""])
        return run

    return [
        (TravelerProfileInput, wrap("profile", uid)),
        (TravelerRecallInput, wrap("recall", uid, "{query}")),
        (TravelerRememberInput, wrap("remember", uid, "{fact}")),
        (TravelerRejectInput, wrap("reject", uid, "{reason}")),
        (PeekTravelerInput, wrap("peek", "{traveler}", "{query}")),
        (GroupRecallInput, wrap("group-recall", uid, "{query}")),
        (GroupShareInput, wrap("group-share", uid, "{note}")),
        (GroupRejectInput, wrap("group-reject", uid, "{reason}")),
        (EvaluateOptionsInput, wrap("evaluate", uid, "{option_type}")),
        (ListOptionsInput, wrap("options", uid, "{option_type}")),
    ]


def instructions(user: dict) -> str:
    name, uid = user["name"], user["id"]
    trip = p2cli.trip_of(uid)
    names = {u["id"]: u["name"] for u in p2cli._users()}
    others = ", ".join(names.get(m, m) for m in trip.get("members", []) if m != uid) or "not known yet"
    where = trip.get("destination") or "a destination still to decide"
    when = f", {trip['start']} to {trip['end']}" if trip.get("start") else ""
    return f"""You are {name}'s Advocate in travelbee: each traveler has an AI Advocate, and the Advocates negotiate
a fair group trip ({where}{when}). You represent {name} (traveler id {uid}) and only {name}. Fellow travelers: {others}.
Be warm, short and concrete (1-4 sentences), in the language the person writes in, first person as {name}'s advocate.

Memory. <cognee_memory> blocks are context recalled from past Band rooms, shared by all Advocates: treat it as your
own memory. Use cognee_search for explicit lookups and cognee_remember for facts the whole team should keep.
{name}'s own memory and the group's memory are behind the traveler_* and group_* tools.
- Ground every claim about preferences, votes or past decisions in tool output or recalled memory; quote memory
  when it explains a veto. Start a conversation with traveler_profile.
- Save to {name}'s memory (traveler_remember / traveler_reject) only when the speaker speaks for {name}, or when
  the Coordinator announces {name}'s rejection. Refuse politely if someone else tries to change {name}'s preferences.
- When the room agrees on something, record it with group_share (or group_reject for an agreed exclusion).
- Vote with evaluate_options; re-run it after a memory change to show the effect. You may concede when the group's
  pick is acceptable for {name} (no dealbreaker, within budget).
- Use only your tools. Do not try to read or edit files or run commands.
- Mention other agents or people by @handle so the message reaches them."""


def seed_profile(client: CogneeClient, user: dict) -> None:
    a = user.get("answers") or {}
    trip = p2cli.trip_of(user["id"])
    text = (f"{user['name']} (traveler id {user['id']}) is on the trip to {trip.get('destination') or 'TBD'}"
            f"{' from ' + trip['start'] + ' to ' + trip['end'] if trip.get('start') else ''}. Flies from "
            f"{user.get('home_city') or 'unknown'}. Budget: ${user.get('budget_per_night') or '?'} per night "
            f"({a.get('budget') or 'unspecified'}). Pace: {a.get('pace') or '-'}. Food: {a.get('food') or '-'}. "
            f"Lodging: {a.get('lodging') or 'no preference'}. Wants to do: {', '.join(a.get('activities') or []) or '-'}. "
            f"Dealbreakers: {', '.join(a.get('dealbreakers') or []) or 'none'}. {user['name']}'s Advocate defends these.")
    client.remember(text)


# ---------------------------------------------------------------- main loop
async def serve() -> None:
    configure_logging()
    settings = CogneeSettings.resolve()
    client = CogneeClient(settings)
    running: dict[str, asyncio.Task] = {}
    print(f"[advocates] watching {p2cli.USERS_DIR} for travelers (memory {settings.dataset} on {settings.base_url}, "
          f"P2 memory backend {memory.backend()})", flush=True)
    while True:
        creds = _load_creds()
        for user in p2cli._users():
            uid = user["id"]
            if uid in running and not running[uid].done():
                continue
            try:
                c = await asyncio.to_thread(ensure_agent, user, creds)
                if not c:
                    continue
                await asyncio.to_thread(add_to_room, c)
                if not c.get("seeded"):
                    await asyncio.to_thread(seed_profile, client, user)
                    c["seeded"] = True
                _save_creds(creds)
            except Exception as exc:
                print(f"[advocates] {user['name']}: {exc}", flush=True)
                continue
            inner = ClaudeSDKAdapter(
                ClaudeSDKAdapterConfig(model=os.getenv("ADVOCATE_MODEL", "sonnet"),
                                       custom_section=instructions(user),
                                       permission_mode="dontAsk"),  # native tools (Bash, Edit, ...) denied
                additional_tools=cognee_tools(client) + p2_tools(uid),
            )
            agent = Agent.create(adapter=CogneeMemoryAdapter(inner, settings=settings, client=client),
                                 agent_id=c["agent_id"], api_key=c["api_key"])
            running[uid] = asyncio.create_task(agent.run(), name=f"advocate-{uid}")
            print(f"[advocates] {c['name']} is live for {user['name']} ({uid})", flush=True)
        await asyncio.sleep(POLL_S)


if __name__ == "__main__":
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
