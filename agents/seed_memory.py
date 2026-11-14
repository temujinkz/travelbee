"""Seed the Advocates' shared room memory (COGNEE_BAND_DATASET, default travelbee_rooms) with the trip and each
traveler's questionnaire, so every Advocate's auto-recall knows the others' preferences from the first message.
Cognee Cloud answers 404 on recall until a dataset has data, so run this once per tenant (or after a reset).

    .venv/bin/python agents/seed_memory.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != ROOT] + [ROOT]
os.environ.setdefault("MEMORY_BACKEND", "dict")  # only for reading .env; seeding goes through cognee-band
import memory  # noqa: E402,F401  (loads .env)

os.environ.setdefault("COGNEE_BASE_URL", os.getenv("COGNEE_SERVICE_URL", ""))
os.environ.setdefault("COGNEE_BAND_DATASET", "travelbee_rooms")
from cognee_band import CogneeClient, CogneeSettings  # noqa: E402

import json  # noqa: E402
from pathlib import Path  # noqa: E402

trips = sorted((Path(ROOT) / "data" / "trips").glob("*.json"), key=lambda f: f.stat().st_mtime)
if not trips:
    sys.exit("no saved trip in data/trips: create one in the web app and register travelers first")
t = json.loads(trips[-1].read_text())
users = [json.loads((Path(ROOT) / "data" / "users" / f"{uid}.json").read_text()) for uid in t["members"]]
client = CogneeClient(CogneeSettings.resolve())
docs = [f"Travelbee trip '{t['id']}': {t['destination']}, from {t['start']} to {t['end']}. Travelers: "
        + ", ".join(u["name"] for u in users)
        + ". Each traveler has an AI Advocate agent in the Band room."]
for u in users:
    a = u.get("answers") or {}
    docs.append(f"{u['name']} (traveler id {u['id']}) flies from {u.get('home_city') or 'an unknown city'}. Budget: ${u.get('budget_per_night')} per "
                f"night ({a.get('budget')}). Pace: {a.get('pace')}. Food: {a.get('food')}. Lodging: "
                f"{a.get('lodging') or 'no preference'}. Wants to do: {', '.join(a.get('activities') or []) or 'nothing specific'}. "
                f"Dealbreakers: {', '.join(a.get('dealbreakers') or []) or 'none'}. {u['name']}'s Advocate defends these preferences.")
for d in docs:
    print(client.remember(d), d[:70])
print(f"seeded {len(docs)} documents into {client.settings.dataset} on {client.settings.base_url}")
