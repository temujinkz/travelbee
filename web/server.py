#!/usr/bin/env python3
"""Travelbee server: serves web/index.html and a small JSON API. Standard library only.

    python3 web/server.py            # http://127.0.0.1:8123

Storage follows TEAM_SPLIT.md: one JSON file per trip and per user under data/.
Access is by link, not by login:
  join link       ?trip=<id>                  anyone, opens the questionnaire
  organizer link  ?trip=<id>&admin=<secret>   the creator, sees everyone and starts planning
  personal link   ?trip=<id>&u=<user id>      one traveler, answers questions and sees the plan

The negotiation in run_plan() is a MOCK that follows the Message format from TEAM_SPLIT.md.
Replace _choose_type()/run_plan() with coordinator.run_trip() when the real agents exist.
"""
import datetime
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

WEB = Path(__file__).resolve().parent
DATA = WEB.parent / "data"
TRIPS = DATA / "trips"
USERS = DATA / "users"
LOCK = threading.RLock()
ANSWERS = {}  # trip id -> {"qid", "event", "answer"} for the question the agents are waiting on

ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alike characters
MAX_BODY = 64 * 1024
MAX_MEMBERS = 12
STEP_DELAY = 0.45  # seconds between mock agent messages, so the log feels live

# Single source for the questionnaire. Tag values must match the shared tag list in TEAM_SPLIT.md
# (beach, hostel, red-eye, nonstop, long-drive, airboat, snorkel, fine-dining).
QUESTIONS = {
    "pace": [["relaxed", "Relaxed"], ["balanced", "Balanced"], ["packed", "Packed"]],
    "budget": [["tight", "Keep it cheap"], ["comfortable", "Comfortable"], ["splurge", "Splurge"]],
    "food": [["casual", "Casual and local"], ["fine-dining", "Fine dining"]],
    "activities": [["beach", "Beach"], ["snorkel", "Snorkeling"], ["airboat", "Airboat"]],
    "lodging": [["hostel", "Hostel"], ["hotel", "Hotel"], ["rental", "Rental"]],
    "dealbreakers": [["red-eye", "Red-eye flights"], ["long-drive", "Long drives"], ["hostel", "Hostels"]],
}
SINGLE = ("pace", "budget", "food", "lodging")
MULTI = ("activities", "dealbreakers")


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


# ---------------------------------------------------------------- storage

def _read(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def load_trip(tid):
    if not ID_RE.match(tid or ""):
        return None
    return _read(TRIPS / f"{tid}.json")


def save_trip(t):
    _write(TRIPS / f"{t['id']}.json", t)


def load_user(uid):
    if not ID_RE.match(uid or ""):
        return None
    return _read(USERS / f"{uid}.json")


def save_user(u):
    _write(USERS / f"{u['id']}.json", u)


def need_trip(tid):
    t = load_trip(tid)
    if not t:
        raise ApiError(404, "That trip link does not exist.")
    return t


def check_admin(t, token):
    if not isinstance(token, str) or not hmac.compare_digest(token, t["admin_token"]):
        raise ApiError(403, "That organizer link is not valid.")


# ---------------------------------------------------------------- validation

def clean(v, n):
    return v.strip()[:n] if isinstance(v, str) else ""


def clean_date(v):
    v = clean(v, 10)
    if not v:
        return ""
    if not DATE_RE.match(v):
        raise ApiError(400, "Dates must look like 2026-11-20.")
    try:
        datetime.date.fromisoformat(v)
    except ValueError:
        raise ApiError(400, "That date does not exist.")
    return v


def apply_details(t, body):
    dest = clean(body.get("destination"), 80)
    start, end = clean_date(body.get("start")), clean_date(body.get("end"))
    if start and end and end < start:
        raise ApiError(400, "The end date is before the start date.")
    t["destination"], t["start"], t["end"] = dest, start, end


def clean_answers(raw):
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key, options in QUESTIONS.items():
        allowed = {o[0] for o in options}
        v = raw.get(key)
        if key in SINGLE:
            out[key] = v if isinstance(v, str) and v in allowed else None
        else:
            out[key] = [x for x in (v if isinstance(v, list) else []) if isinstance(x, str) and x in allowed]
    return out


# ---------------------------------------------------------------- views

def public_trip(t):
    return {
        "id": t["id"], "destination": t["destination"], "start": t["start"], "end": t["end"],
        "member_count": len(t["members"]), "status": t["status"],
    }


def pending_view(t, uid=None, admin=False):
    p = t.get("pending")
    if not p:
        return None
    mine = admin or (uid is not None and p["to"] == uid)
    view = {"id": p["id"], "kind": p["kind"], "question": p["question"], "mine": mine}
    if mine:
        view["choices"] = p.get("choices")
        view["plan"] = p.get("plan")
    else:
        name = (load_user(p["to"]) or {}).get("name") if p["to"] != "organizer" else "the organizer"
        view["waiting_for"] = name or "a traveler"
    return view


# ---------------------------------------------------------------- mock agents

def make_options(dest):
    d = dest or "your destination"
    return [
        {"id": "f1", "type": "flight", "title": f"Red-eye nonstop to {d}", "price": 120, "url": "#", "tags": ["red-eye", "nonstop"], "min_round": 1},
        {"id": "f2", "type": "flight", "title": f"Daytime nonstop to {d}", "price": 240, "url": "#", "tags": ["nonstop"], "min_round": 1},
        {"id": "s1", "type": "stay", "title": "Harbor Hostel", "price": 60, "url": "#", "tags": ["hostel"], "min_round": 1},
        {"id": "s2", "type": "stay", "title": "Beachfront Hotel", "price": 220, "url": "#", "tags": ["beach"], "min_round": 1},
        {"id": "s3", "type": "stay", "title": "Midtown Rental", "price": 130, "url": "#", "tags": [], "min_round": 2},
        {"id": "a1", "type": "activity", "title": "Snorkel trip to the reef", "price": 90, "url": "#", "tags": ["snorkel", "long-drive"], "min_round": 1},
        {"id": "a2", "type": "activity", "title": "Airboat tour", "price": 55, "url": "#", "tags": ["airboat"], "min_round": 1},
        {"id": "a3", "type": "activity", "title": "Sunset beach day", "price": 0, "url": "#", "tags": ["beach"], "min_round": 1},
    ]


def objections(u, o):
    bad = [tag for tag in (u["answers"].get("dealbreakers") or []) if tag in o["tags"]]
    b = u.get("budget_per_night")
    if o["type"] == "stay" and b is not None and o["price"] > b:
        bad.append("over budget")
    return bad


def score(u, o):
    a = u["answers"]
    s = sum(1 for tag in (a.get("activities") or []) if tag in o["tags"])
    if a.get("lodging") == "hostel" and "hostel" in o["tags"]:
        s += 1
    return s


def add_log(tid, rnd, frm, to, typ, content, refs=(), sleep=True):
    with LOCK:
        t = load_trip(tid)
        t["log"].append({"round": rnd, "from": frm, "to": to, "type": typ, "content": content, "refs": list(refs), "at": time.time()})
        save_trip(t)
    if sleep:
        time.sleep(STEP_DELAY)


def set_state(tid, **fields):
    with LOCK:
        t = load_trip(tid)
        t.update(fields)
        save_trip(t)


def ask_human(tid, rnd, to, kind, question, choices=None, plan=None):
    qid = secrets.token_hex(4)
    ev = threading.Event()
    ANSWERS[tid] = {"qid": qid, "event": ev, "answer": None}
    set_state(tid, status="awaiting", pending={"id": qid, "to": to, "kind": kind, "question": question, "choices": choices, "plan": plan})
    who = "human:organizer" if to == "organizer" else "human:" + ((load_user(to) or {}).get("name") or "traveler")
    add_log(tid, rnd, "coordinator", who, "human_request", question, sleep=False)
    answered = ev.wait(1800)
    set_state(tid, status="planning", pending=None)
    if not answered:
        raise TimeoutError()
    return ANSWERS[tid]["answer"]


def _choose_type(tid, users, typ, excluded, opts):
    names = lambda u: f"advocate:{u['name']}"
    for r in range(1, 4):
        cands = [o for o in opts if o["type"] == typ and o["min_round"] <= r and o["id"] not in excluded]
        if not cands:
            add_log(tid, r, "coordinator", "all", "decision", f"No {typ} options are left to choose from.")
            return None
        vetoed = {}
        for u in users:
            ok = []
            for o in cands:
                bad = objections(u, o)
                if bad:
                    vetoed.setdefault(o["id"], []).append(u)
                    add_log(tid, r, names(u), "coordinator", "objection", f"I object to {o['title']}: {', '.join(bad)}.", [o["id"]])
                else:
                    ok.append(o)
            if ok:
                best = max(ok, key=lambda o: (score(u, o), -o["price"]))
                add_log(tid, r, names(u), "coordinator", "proposal", f"I propose {best['title']} (${best['price']}).", [best["id"]])
        viable = [o for o in cands if o["id"] not in vetoed]
        if viable:
            pick = max(viable, key=lambda o: (sum(score(u, o) for u in users), -o["price"]))
            add_log(tid, r, "coordinator", "all", "decision", f"Chose {pick['title']} for {typ}. Nobody objected.", [pick["id"]])
            return pick
        if r < 3:
            add_log(tid, r, "coordinator", "researcher", "research_request", f"Every {typ} option was vetoed. Search again with tighter limits.")
            fresh = [o for o in opts if o["type"] == typ and o["min_round"] == r + 1 and o["id"] not in excluded]
            if fresh:
                add_log(tid, r + 1, "researcher", "coordinator", "research_result", f"Found a new {typ} option: {fresh[0]['title']} (${fresh[0]['price']}).", [fresh[0]["id"]])
            else:
                add_log(tid, r + 1, "researcher", "coordinator", "research_result", f"No new {typ} options. The search is already as narrow as it gets.")
    # deadlock after 3 rounds: ask the traveler with the most vetoes
    loudest = max(users, key=lambda u: sum(1 for lst in vetoed.values() if u in lst))
    choices = [{"value": o["id"], "label": f"{o['title']} (${o['price']})"} for o in cands]
    ans = ask_human(tid, 3, loudest["id"], "choice", f"The group is stuck on the {typ}. Which one can you live with?", choices)
    pick = next((o for o in cands if o["id"] == ans["choice"]), cands[0])
    if ans.get("by") == "admin":
        add_log(tid, 3, "human:organizer", "coordinator", "human_response", f"The organizer chose {pick['title']} on behalf of {loudest['name']}.", [pick["id"]])
    else:
        add_log(tid, 3, f"human:{loudest['name']}", "coordinator", "human_response", f"{loudest['name']} picked {pick['title']}.", [pick["id"]])
    add_log(tid, 3, "coordinator", "all", "decision", f"Chose {pick['title']} for {typ} after the deadlock.", [pick["id"]])
    return pick


def _plan(tid):
    t = load_trip(tid)
    users = [load_user(uid) for uid in t["members"]]
    opts = make_options(t["destination"])
    set_state(tid, status="planning", log=[], plan=None, pending=None)
    add_log(tid, 1, "coordinator", "all", "research_request", f"Planning a trip to {t['destination']} for {len(users)} travelers.")
    excluded, chosen = set(), {}
    for typ in ("flight", "stay", "activity"):
        chosen[typ] = _choose_type(tid, users, typ, excluded, opts)
    while True:
        plan = [o for o in chosen.values() if o]
        set_state(tid, plan=plan)
        ans = ask_human(tid, 3, "organizer", "approval", "Here is the plan. Approve it, or reject one item with a reason.", plan=plan)
        if ans["choice"] == "approve":
            add_log(tid, 3, "human:organizer", "coordinator", "human_response", "Approved the plan.")
            add_log(tid, 3, "coordinator", "all", "decision", "The plan is final.", sleep=False)
            set_state(tid, status="done")
            return
        item = next((o for o in plan if o["id"] == ans["item_id"]), None)
        add_log(tid, 3, "human:organizer", "coordinator", "human_response", f"Rejected {item['title'] if item else 'an item'}: {ans['reason'] or 'no reason given'}.")
        # Real version: remember() the feedback in trip_<id>, improve(), then re-plan that type.
        add_log(tid, 3, "coordinator", "all", "decision", "Saved the feedback to trip memory (mock of remember and improve) and re-planning that item.")
        if item:
            excluded.add(item["id"])
            chosen[item["type"]] = _choose_type(tid, users, item["type"], excluded, opts)


def _create_band_agent(u):
    """With TRANSPORT=band and BAND_USER_API_KEY set, give this traveler their own Band agent (in the background)."""
    root = str(WEB.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    import protocol  # noqa: F401  (loads .env)

    if os.environ.get("TRANSPORT") != "band" or not os.environ.get("BAND_USER_API_KEY"):
        return
    import band

    threading.Thread(target=band.ensure_agent, args=(u,), daemon=True).start()


def run_plan(tid):
    try:
        if os.environ.get("AGENTS", "real") == "mock":  # the original stand-in negotiation above
            _plan(tid)
        else:  # the real agents: coordinator.run_trip (see web/bridge.py)
            import bridge

            bridge.run(tid, sys.modules[__name__])
    except TimeoutError:
        add_log(tid, 3, "coordinator", "all", "decision", "Planning paused because nobody answered in time. Start it again when ready.", sleep=False)
        set_state(tid, status="gathering", pending=None)
    except Exception:
        traceback.print_exc()
        add_log(tid, 3, "coordinator", "all", "decision", "Planning stopped because of a server error.", sleep=False)
        set_state(tid, status="gathering", pending=None)


# ---------------------------------------------------------------- routes

def route(method, path, q, body):
    parts = [p for p in path.split("/") if p]
    if parts[:1] != ["api"]:
        raise ApiError(404, "Not found.")
    parts = parts[1:]
    one = lambda k: (q.get(k) or [""])[0]

    if method == "GET" and parts == ["questions"]:
        return QUESTIONS

    if method == "POST" and parts == ["trips"]:
        t = {
            "id": "".join(secrets.choice(ALPHABET) for _ in range(8)),
            "admin_token": secrets.token_urlsafe(18),
            "destination": "", "start": "", "end": "",
            "members": [], "status": "gathering", "pending": None, "plan": None, "log": [],
            "created_at": time.time(),
        }
        apply_details(t, body)
        with LOCK:
            save_trip(t)
        return {"trip_id": t["id"], "admin_token": t["admin_token"]}

    if len(parts) >= 2 and parts[0] == "trips":
        t = need_trip(parts[1])
        rest = parts[2:]

        if method == "GET" and not rest:
            return public_trip(t)

        if method == "GET" and rest == ["dashboard"]:
            check_admin(t, one("admin"))
            members = [load_user(uid) for uid in t["members"]]
            return {"trip": public_trip(t), "members": [m for m in members if m], "log": t["log"],
                    "pending": pending_view(t, admin=True), "plan": t["plan"]}

        if method == "GET" and rest == ["export"]:
            check_admin(t, one("admin"))
            return {"trip": {k: v for k, v in t.items() if k != "admin_token"},
                    "users": [u for u in (load_user(uid) for uid in t["members"]) if u]}

        if method == "GET" and rest == ["me"]:
            u = load_user(one("u"))
            if not u or u["trip_id"] != t["id"]:
                raise ApiError(403, "That personal link is not valid.")
            return {"trip": public_trip(t), "user": u, "log": t["log"],
                    "pending": pending_view(t, uid=u["id"]), "plan": t["plan"]}

        if method == "POST" and rest == ["details"]:
            check_admin(t, one("admin") or body.get("admin"))
            with LOCK:
                t = need_trip(t["id"])
                apply_details(t, body)
                save_trip(t)
            return public_trip(t)

        if method == "POST" and rest == ["users"]:
            name = clean(body.get("name"), 60)
            if not name:
                raise ApiError(400, "A name is required.")
            budget = body.get("budget_per_night")
            budget = float(budget) if isinstance(budget, (int, float)) and not isinstance(budget, bool) and 0 <= budget < 100000 else None
            organizer = False
            if body.get("admin"):
                check_admin(t, body.get("admin"))
                organizer = True
            with LOCK:
                t = need_trip(t["id"])
                if len(t["members"]) >= MAX_MEMBERS:
                    raise ApiError(400, "This trip is full.")
                if organizer and any(load_user(m).get("is_organizer") for m in t["members"] if load_user(m)):
                    raise ApiError(400, "The organizer has already answered.")
                u = {
                    "id": secrets.token_urlsafe(9), "trip_id": t["id"], "name": name,
                    "budget_per_night": budget, "home_city": clean(body.get("home_city"), 80),
                    "answers": clean_answers(body.get("answers")), "is_organizer": organizer,
                    "created_at": time.time(),
                }
                save_user(u)
                t["members"].append(u["id"])
                save_trip(t)
            _create_band_agent(u)
            return {"user_id": u["id"]}

        if method == "POST" and rest == ["plan"]:
            check_admin(t, one("admin") or body.get("admin"))
            with LOCK:
                t = need_trip(t["id"])
                if t["status"] in ("planning", "awaiting"):
                    raise ApiError(400, "Planning is already running.")
                if not t["destination"]:
                    raise ApiError(400, "Add a destination before planning.")
                if len(t["members"]) < 2:
                    raise ApiError(400, "Wait for at least two travelers to answer.")
                t["status"], t["log"], t["plan"], t["pending"] = "planning", [], None, None
                save_trip(t)
            threading.Thread(target=run_plan, args=(t["id"],), daemon=True).start()
            return {"ok": True}

        if method == "POST" and rest == ["answer"]:
            admin = False
            if body.get("admin"):
                check_admin(t, body.get("admin"))
                admin = True
            uid = body.get("u") if isinstance(body.get("u"), str) else None
            with LOCK:
                t = need_trip(t["id"])
                p, waiting = t.get("pending"), ANSWERS.get(t["id"])
                if not p or not waiting or waiting["qid"] != body.get("question_id"):
                    raise ApiError(409, "That question is no longer open.")
                if not (admin or (uid and uid == p["to"])):
                    raise ApiError(403, "This question is for someone else.")
                choice = body.get("choice")
                if p["kind"] == "choice":
                    if choice not in {c["value"] for c in p["choices"]}:
                        raise ApiError(400, "Pick one of the choices.")
                    answer = {"choice": choice}
                else:
                    if choice not in ("approve", "reject"):
                        raise ApiError(400, "Approve or reject.")
                    answer = {"choice": choice}
                    if choice == "reject":
                        item = body.get("item_id")
                        if item not in {o["id"] for o in p["plan"]}:
                            raise ApiError(400, "Pick the item to reject.")
                        answer.update(item_id=item, reason=clean(body.get("reason"), 300))
                answer["by"] = "admin" if admin else "user"
                waiting["answer"] = answer
                waiting["event"].set()
            return {"ok": True}

    raise ApiError(404, "Not found.")


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "Travelbee"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, urlparse(self.path).path))

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _handle(self, method):
        url = urlparse(self.path)
        try:
            if method == "GET" and url.path in ("/", "/index.html"):
                return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    raise ApiError(413, "That request is too large.")
                raw = self.rfile.read(n) if n else b"{}"
                try:
                    body = json.loads(raw)
                except ValueError:
                    raise ApiError(400, "That request was not valid JSON.")
                if not isinstance(body, dict):
                    raise ApiError(400, "Expected a JSON object.")
            self._json(200, route(method, url.path, parse_qs(url.query), body))
        except ApiError as e:
            self._json(e.code, {"error": e.message})
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": "Something went wrong on the server."})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def recover():
    """A restart kills any running negotiation, so put those trips back to a state the organizer can restart."""
    if not TRIPS.exists():
        return
    for f in TRIPS.glob("*.json"):
        t = _read(f)
        if t and t.get("status") in ("planning", "awaiting"):
            t["status"], t["pending"] = "gathering", None
            t["log"].append({"round": 0, "from": "coordinator", "to": "all", "type": "decision",
                             "content": "The server restarted while planning. Start it again when ready.", "refs": [], "at": time.time()})
            _write(f, t)


if __name__ == "__main__":
    recover()
    port = int(os.environ.get("PORT", "8123"))
    host = os.environ.get("HOST", "127.0.0.1")  # HOST=0.0.0.0 lets others on the same Wi-Fi open it
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Travelbee on http://{host}:{port}")
    srv.serve_forever()
