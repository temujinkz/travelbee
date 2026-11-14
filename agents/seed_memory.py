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
import mocks  # noqa: E402

os.environ.setdefault("COGNEE_BASE_URL", os.getenv("COGNEE_SERVICE_URL", ""))
os.environ.setdefault("COGNEE_BAND_DATASET", "travelbee_rooms")
from cognee_band import CogneeClient, CogneeSettings  # noqa: E402

client = CogneeClient(CogneeSettings.resolve())
t = mocks.TRIP
docs = [f"Travelbee trip '{t['id']}': {t['destination']}, from {t['start']} to {t['end']}. Travelers: "
        + ", ".join(u["name"] for u in mocks.USERS)
        + ". Each traveler has an AI Advocate agent in the Band room that negotiates the plan."]
for u in mocks.USERS:
    a = u["answers"]
    docs.append(f"{u['name']} (traveler id {u['id']}) flies from {u['home_city']}. Budget: ${u['budget_per_night']} per "
                f"night ({a.get('budget')}). Pace: {a.get('pace')}. Food: {a.get('food')}. Lodging: "
                f"{a.get('lodging') or 'no preference'}. Wants to do: {', '.join(a.get('activities') or []) or 'nothing specific'}. "
                f"Dealbreakers: {', '.join(a.get('dealbreakers') or []) or 'none'}. {u['name']}'s Advocate defends these preferences.")
for d in docs:
    print(client.remember(d), d[:70])
print(f"seeded {len(docs)} documents into {client.settings.dataset} on {client.settings.base_url}")
