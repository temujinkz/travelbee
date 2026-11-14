"""The four travelbee Advocates as Band agents with a shared Cognee memory. Owner: P2.

Pattern from the cognee BAND workshop (github.com/jure-cognee/band-cognee-workshop, step 6):
a normal Band agent, wrapped in `CogneeMemoryAdapter` (cognee-integration-band), so that
  - before each room message, relevant memory is recalled and added to it (auto-recall),
  - after each reply, the question and answer are stored (auto-capture),
  - when a room ends or the agent stops, the room is turned into graph memory (improve).
Every Advocate uses the same room dataset, so one Advocate knows what another was told, in a room it was never in.

The model is Claude Code through band-sdk's ClaudeSDKAdapter, logged in with `claude login` (no API key needed).
On top of cognee's own tools (cognee_search, cognee_remember) each Advocate gets the P2 tools, so it votes with
the same rules and the same per-traveler / group memory as the Coordinator:
  user_<id>   that traveler's own memory          trip_<id>   the group's shared memory

Run (from the repository root; Band Desktop's hosted Advocates must be stopped first, see README_P2.md):
    .venv/bin/python agents/advocates.py            all four
    .venv/bin/python agents/advocates.py ben lea    some of them

.env (git-ignored) needs:
    COGNEE_SERVICE_URL (or COGNEE_BASE_URL), COGNEE_API_KEY     the Cognee Cloud tenant
    BAND_ADVOCATE_1_KEY ... BAND_ADVOCATE_4_KEY                   the Advocates' Band API keys (any order)
Optional: COGNEE_BAND_DATASET (default travelbee_rooms), ADVOCATE_MODEL (default sonnet), TRIP_ID (default miami).
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import unicodedata
import urllib.request

# The repo has a band.py (P3's transport) that would shadow the band-sdk package if the repo root came first.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != ROOT] + [ROOT]

import memory  # noqa: E402  (also loads ROOT/.env into the environment)

if os.getenv("COGNEE_SERVICE_URL") and not os.getenv("COGNEE_BASE_URL"):
    os.environ["COGNEE_BASE_URL"] = os.environ["COGNEE_SERVICE_URL"]
os.environ.setdefault("COGNEE_BAND_DATASET", f"travelbee_rooms")

from band import Agent, configure_logging  # noqa: E402
from band.adapters import ClaudeSDKAdapter, ClaudeSDKAdapterConfig  # noqa: E402
from cognee_band import CogneeClient, CogneeMemoryAdapter, CogneeSettings, cognee_tools  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import p2cli  # noqa: E402

BAND_REST = os.getenv("BAND_REST_URL", "https://app.band.ai/api/v1").rstrip("/")


def _plain(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


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
        traveler_id: str = Field(description="lea, ben, chloe or diego")
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
        """Your votes and vetoes, with reasons, on the options on the table. Re-run after any memory change."""
        option_type: str = Field(default="", description="flight, stay, activity, or empty for all")

    class ListOptionsInput(BaseModel):
        """The options on the table (id, type, price, title, tags)."""
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
        (PeekTravelerInput, wrap("peek", "{traveler_id}", "{query}")),
        (GroupRecallInput, wrap("group-recall", "{query}")),
        (GroupShareInput, wrap("group-share", uid, "{note}")),
        (GroupRejectInput, wrap("group-reject", uid, "{reason}")),
        (EvaluateOptionsInput, wrap("evaluate", uid, "{option_type}")),
        (ListOptionsInput, wrap("options", "{option_type}")),
    ]


def instructions(user: dict, others: list[dict]) -> str:
    name, uid = user["name"], user["id"]
    who = ", ".join(f"{o['name']} ({o['id']})" for o in others)
    return f"""You are {name}'s Advocate in travelbee: four AI Advocates plan a group trip (Miami + Everglades +
Florida Keys, Dec 10-15). You represent {name} (traveler id {uid}) and only {name}. The others: {who}.
Be warm, short and concrete (1-4 sentences), in the language the person writes in, first person as {name}'s advocate.

Memory. <cognee_memory> blocks are context recalled from past Band rooms, shared by all four Advocates: treat it
as your own memory. Use cognee_search for explicit lookups and cognee_remember for facts the whole team should keep.
Your traveler's own memory and the group's memory are behind the traveler_* and group_* tools.
- Ground every claim about preferences, votes or past decisions in tool output or recalled memory; quote memory
  when it explains a veto. Start a conversation with traveler_profile.
- Save to {name}'s memory (traveler_remember / traveler_reject) only when the speaker speaks for {name}, or when
  the Coordinator announces {name}'s rejection. Refuse politely if someone else tries to change {name}'s preferences.
- When the room agrees on something, record it with group_share (or group_reject for an agreed exclusion).
- Vote with evaluate_options; re-run it after a memory change to show the effect. You may concede when the group's
  pick is acceptable for {name} (no dealbreaker, within budget).
- Use only your tools. Do not try to read or edit files or run commands.
- Mention other agents or people by @handle so the message reaches them."""


def _whoami(key: str) -> dict:
    req = urllib.request.Request(f"{BAND_REST}/agent/me", headers={"X-API-Key": key, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    return data.get("data", data)


async def main(only: list[str]) -> None:
    configure_logging()
    users = p2cli._users()
    keys = [os.environ[k] for k in sorted(os.environ) if k.startswith("BAND_ADVOCATE_") and k.endswith("_KEY") and os.environ[k]]
    if not keys:
        sys.exit("No BAND_ADVOCATE_n_KEY in .env: add the four Advocates' Band API keys (see .env.example).")
    if not os.getenv("COGNEE_BASE_URL"):
        print("[advocates] no COGNEE_SERVICE_URL / COGNEE_BASE_URL: cognee-band will try a local server", flush=True)

    settings = CogneeSettings.resolve()
    client = CogneeClient(settings)
    agents = []
    for key in keys:
        me = _whoami(key)
        user = next((u for u in users if _plain(u["name"]) in _plain(me.get("name", ""))), None)
        if not user or (only and user["id"] not in only):
            continue
        others = [u for u in users if u["id"] != user["id"]]
        inner = ClaudeSDKAdapter(
            ClaudeSDKAdapterConfig(
                model=os.getenv("ADVOCATE_MODEL", "sonnet"),
                custom_section=instructions(user, others),
                permission_mode="dontAsk",  # native Claude Code tools (Bash, Edit, ...) are denied; our tools work
            ),
            additional_tools=cognee_tools(client) + p2_tools(user["id"]),
        )
        agent = Agent.create(adapter=CogneeMemoryAdapter(inner, settings=settings, client=client),
                             agent_id=me["id"], api_key=key)
        print(f"[advocates] {me.get('name')} -> traveler {user['id']} (memory dataset {settings.dataset} on "
              f"{settings.base_url}; P2 memory backend {memory.backend()})", flush=True)
        agents.append(agent)
    if not agents:
        sys.exit("None of the keys belongs to an Advocate named after a traveler (e.g. \"Ben's Advocate\").")
    await asyncio.gather(*(a.run() for a in agents))


if __name__ == "__main__":
    try:
        asyncio.run(main([a.lower() for a in sys.argv[1:]]))
    except KeyboardInterrupt:
        pass
