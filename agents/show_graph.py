"""Open the Cognee Cloud knowledge graph of a dataset in your browser.

    .venv/bin/python agents/show_graph.py                    # travelbee_rooms (the Advocates' shared room memory)
    .venv/bin/python agents/show_graph.py user_ben trip_miami

Downloads the tenant's own graph page (GET /api/v1/visualize?dataset_id=...) to data/graph_<dataset>.html
and opens it. Reads COGNEE_SERVICE_URL and COGNEE_API_KEY from .env.
"""
import json
import os
import sys
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = {}
for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
url, key = env.get("COGNEE_SERVICE_URL", "").rstrip("/"), env.get("COGNEE_API_KEY", "")
if not url or not key:
    sys.exit("COGNEE_SERVICE_URL / COGNEE_API_KEY missing in .env")


def get(path: str) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url + path, headers={"X-Api-Key": key}), timeout=120).read()


datasets = {d["name"]: d["id"] for d in json.loads(get("/api/v1/datasets"))}
print("datasets on your tenant:", ", ".join(datasets))
for name in sys.argv[1:] or ["travelbee_rooms"]:
    if name not in datasets:
        print(f"- {name}: not on the tenant yet")
        continue
    try:
        html = get(f"/api/v1/visualize?dataset_id={datasets[name]}")
    except urllib.error.HTTPError as e:
        print(f"- {name}: HTTP {e.code} (no graph yet: data is still only in session memory)")
        continue
    out = os.path.join(ROOT, "data", f"graph_{name}.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
        f.write(html)
    print(f"- {name}: opened {out}")
    webbrowser.open("file://" + out)
