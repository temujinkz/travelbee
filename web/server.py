#!/usr/bin/env python3
"""Travelbee server: serves web/index.html and a small JSON API. Standard library only.

    python3 web/server.py            # http://127.0.0.1:8123

Storage follows TEAM_SPLIT.md: one JSON file per trip and per user under data/.
Access is by link, not by login:
  join link       ?trip=<id>                  anyone, opens the questionnaire
  organizer link  ?trip=<id>&admin=<secret>   the creator, sees everyone and starts planning
  personal link   ?trip=<id>&u=<user id>      one traveler, answers questions and sees the plan

Planning runs coordinator.run_trip(): Google Flights + Keenable find the options, Superlinked decides, the
organizer approves or rejects in the UI. It needs SUPERLINKED_API_KEY and KEENABLE_API_KEY (see .env.example).
There is no mock data: if a service fails, the error is written into the trip's log.
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
DATA = Path(os.environ.get("TRAVELBEE_DATA") or WEB.parent / "data")  # tests use their own folder
TRIPS = DATA / "trips"
USERS = DATA / "users"
LOCK = threading.RLock()
ANSWERS = {}  # trip id -> {"qid", "event", "answer"} for the question the agents are waiting on

ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alike characters
MAX_BODY = 64 * 1024
MAX_MEMBERS = 12
STEP_DELAY = 0.0  # no artificial delay: messages appear as the live pipeline produces them

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


def fix_place(text):
    """Correct typos in a city name with places.py ("Pariss" -> "Paris"); keeps the text if that fails."""
    if not text:
        return text
    root = str(WEB.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    import protocol  # noqa: F401  (loads .env)
    import places

    return places.normalize(text)


def apply_details(t, body, require_dates=False):
    dest = fix_place(clean(body.get("destination"), 80))
    start, end = clean_date(body.get("start")), clean_date(body.get("end"))
    if require_dates and not (start and end):
        raise ApiError(400, "Add both travel dates.")
    if start and start < datetime.date.today().isoformat():
        raise ApiError(400, "The departure date is in the past.")
    if start and end and end <= start:
        raise ApiError(400, "The return date must be after the departure date.")
    t["destination"], t["start"], t["end"] = dest, start, end


def trip_activities(t):
    """Activities proposed for this trip's destination (activities.py), generated once and kept in the trip file."""
    dest = t.get("destination") or ""
    if t.get("activities") and t.get("activities_for") == dest:
        return t["activities"]
    root = str(WEB.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    import protocol  # noqa: F401  (loads .env)
    import activities

    acts = activities.for_destination(dest)
    with LOCK:
        fresh = need_trip(t["id"])
        fresh["activities"], fresh["activities_for"] = acts, dest
        save_trip(fresh)
    return acts


def questions_for(t):
    q = dict(QUESTIONS)
    if t is not None:
        q["activities"] = trip_activities(t)
    return q


def clean_answers(raw, t=None):
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key, options in questions_for(t).items():
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


def _voter_id(t, uid=None, admin=False):
    """Who is voting: the traveler, or (organizer link) the organizer's own traveler record."""
    if uid:
        return uid
    if admin:
        org = next((m for m in t["members"] if (load_user(m) or {}).get("is_organizer")), None)
        return org or "organizer"
    return None


def pending_view(t, uid=None, admin=False):
    p = t.get("pending")
    if not p:
        return None
    if p["to"] == "group":  # a group vote: every traveler sees it and votes
        names = {m: (load_user(m) or {}).get("name", "a traveler") for m in p.get("voters", [])}
        votes = p.get("votes", {})
        me = _voter_id(t, uid, admin)
        return {"id": p["id"], "kind": p["kind"], "question": p["question"], "mine": True, "group": True,
                "choices": p.get("choices"), "plan": p.get("plan"), "summary": p.get("summary"),
                "voted": [names.get(v, "the organizer") for v in votes], "waiting": [n for m, n in names.items() if m not in votes],
                "my_vote": votes.get(me), "can_close": bool(admin and votes)}
    mine = admin or (uid is not None and p["to"] == uid)
    view = {"id": p["id"], "kind": p["kind"], "question": p["question"], "mine": mine}
    if mine:
        view["choices"] = p.get("choices")
        view["plan"] = p.get("plan")
    else:
        name = (load_user(p["to"]) or {}).get("name") if p["to"] != "organizer" else "the organizer"
        view["waiting_for"] = name or "a traveler"
    return view


def tally(p):
    """Majority decision from the group's votes (ties: the earliest vote wins)."""
    votes = list(p.get("votes", {}).items())
    if p["kind"] == "choice":
        counts = {}
        for _, v in votes:
            counts[v["choice"]] = counts.get(v["choice"], 0) + 1
        best = max(counts.values())
        return {"choice": next(v["choice"] for _, v in votes if counts[v["choice"]] == best), "by": "group"}
    approve = sum(1 for _, v in votes if v["choice"] == "approve")
    rejects = [(m, v) for m, v in votes if v["choice"] == "reject"]
    if approve >= len(rejects):
        return {"choice": "approve", "by": "group"}
    per_item = {}
    for _, v in rejects:
        per_item[v["item_id"]] = per_item.get(v["item_id"], 0) + 1
    item = max(per_item, key=per_item.get)
    m, first = next((m, v) for m, v in rejects if v["item_id"] == item)
    name = (load_user(m) or {}).get("name")
    reason = first.get("reason") or "no reason given"
    return {"choice": "reject", "item_id": item, "reason": f"{name}: {reason}" if name else reason, "by": "group"}


# ---------------------------------------------------------------- planning

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


def run_plan(tid):
    try:
        import bridge  # web/bridge.py runs coordinator.run_trip for this trip

        bridge.run(tid, sys.modules[__name__])
    except TimeoutError:
        add_log(tid, 3, "coordinator", "all", "decision", "Planning paused because nobody answered in time. Start it again when ready.", sleep=False)
        set_state(tid, status="gathering", pending=None)
    except Exception:
        if (load_trip(tid) or {}).get("status", "cancelled") == "cancelled":
            return  # the organizer cancelled or deleted the trip while the agents were working
        traceback.print_exc()
        add_log(tid, 3, "coordinator", "all", "decision", f"Planning stopped: {sys.exc_info()[1]}", sleep=False)
        set_state(tid, status="gathering", pending=None)


# ---------------------------------------------------------------- routes

def route(method, path, q, body):
    parts = [p for p in path.split("/") if p]
    if parts[:1] != ["api"]:
        raise ApiError(404, "Not found.")
    parts = parts[1:]
    one = lambda k: (q.get(k) or [""])[0]

    if method == "GET" and parts == ["questions"]:
        tid = one("trip")
        return questions_for(need_trip(tid) if tid else None)

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
        threading.Thread(target=trip_activities, args=(t,), daemon=True).start()
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
                    "pending": pending_view(t, admin=True), "plan": t["plan"], "summary": t.get("summary")}

        if method == "GET" and rest == ["export"]:
            check_admin(t, one("admin"))
            return {"trip": {k: v for k, v in t.items() if k != "admin_token"},
                    "users": [u for u in (load_user(uid) for uid in t["members"]) if u]}

        if method == "GET" and rest == ["me"]:
            u = load_user(one("u"))
            if not u or u["trip_id"] != t["id"]:
                raise ApiError(403, "That personal link is not valid.")
            return {"trip": public_trip(t), "user": u, "log": t["log"],
                    "pending": pending_view(t, uid=u["id"]), "plan": t["plan"], "summary": t.get("summary")}

        if method == "POST" and rest == ["details"]:
            check_admin(t, one("admin") or body.get("admin"))
            with LOCK:
                t = need_trip(t["id"])
                apply_details(t, body, require_dates=True)
                save_trip(t)
            threading.Thread(target=trip_activities, args=(t,), daemon=True).start()
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
                if t["status"] == "cancelled":
                    raise ApiError(400, "This trip was cancelled.")
                if len(t["members"]) >= MAX_MEMBERS:
                    raise ApiError(400, "This trip is full.")
                if organizer and any(load_user(m).get("is_organizer") for m in t["members"] if load_user(m)):
                    raise ApiError(400, "The organizer has already answered.")
                u = {
                    "id": secrets.token_urlsafe(9), "trip_id": t["id"], "name": name,
                    "budget_per_night": budget, "home_city": fix_place(clean(body.get("home_city"), 80)),
                    "answers": clean_answers(body.get("answers"), t), "is_organizer": organizer,
                    "created_at": time.time(),
                }
                save_user(u)
                t["members"].append(u["id"])
                save_trip(t)
            return {"user_id": u["id"]}

        if method == "POST" and rest == ["delete"]:  # organizer only: remove the trip (kept in data/_deleted/)
            check_admin(t, one("admin") or body.get("admin"))
            with LOCK:
                t = need_trip(t["id"])
                waiting = ANSWERS.pop(t["id"], None)
                if waiting:
                    waiting["answer"] = {"cancel": True}
                    waiting["event"].set()
                bin_ = DATA / "_deleted"
                (bin_ / "trips").mkdir(parents=True, exist_ok=True)
                (bin_ / "users").mkdir(parents=True, exist_ok=True)
                for uid in t["members"]:
                    f = USERS / f"{uid}.json"
                    if f.exists():
                        f.replace(bin_ / "users" / f.name)
                (TRIPS / f"{t['id']}.json").replace(bin_ / "trips" / f"{t['id']}.json")
            return {"ok": True}

        if method == "POST" and rest == ["cancel"]:  # organizer only: cancel the trip, stop any vote
            check_admin(t, one("admin") or body.get("admin"))
            with LOCK:
                t = need_trip(t["id"])
                t["status"], t["pending"] = "cancelled", None
                t["log"].append({"round": 0, "from": "coordinator", "to": "all", "type": "decision",
                                 "content": "The organizer cancelled this trip.", "refs": [], "at": time.time()})
                save_trip(t)
                waiting = ANSWERS.get(t["id"])
                if waiting:
                    waiting["answer"] = {"cancel": True}
                    waiting["event"].set()
            return public_trip(t)

        if method == "POST" and rest == ["plan"]:
            check_admin(t, one("admin") or body.get("admin"))
            with LOCK:
                t = need_trip(t["id"])
                if t["status"] == "cancelled":
                    raise ApiError(400, "This trip was cancelled.")
                if t["status"] in ("planning", "awaiting"):
                    raise ApiError(400, "Planning is already running.")
                if not t["destination"]:
                    raise ApiError(400, "Add a destination before planning.")
                if not (t.get("start") and t.get("end")):
                    raise ApiError(400, "Add the travel dates before planning.")
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
                if p["to"] == "group":
                    if body.get("close"):  # the organizer closes the vote with the votes so far
                        if not admin or not p.get("votes"):
                            raise ApiError(400, "Only the organizer can close a vote, once someone has voted.")
                    else:
                        voter = _voter_id(t, uid, admin)
                        if voter not in p.get("voters", []) and voter != "organizer":
                            raise ApiError(403, "You are not part of this trip.")
                        choice = body.get("choice")
                        if p["kind"] == "choice":
                            if choice not in {c["value"] for c in p["choices"]}:
                                raise ApiError(400, "Pick one of the choices.")
                            vote = {"choice": choice}
                        else:
                            if choice not in ("approve", "reject"):
                                raise ApiError(400, "Approve or reject.")
                            vote = {"choice": choice}
                            if choice == "reject":
                                if body.get("item_id") not in {o["id"] for o in p["plan"]}:
                                    raise ApiError(400, "Pick the item to reject.")
                                vote.update(item_id=body["item_id"], reason=clean(body.get("reason"), 300))
                        p.setdefault("votes", {})[voter] = vote
                        t["pending"] = p
                        save_trip(t)
                    if body.get("close") or all(v in p.get("votes", {}) for v in p.get("voters", [])):
                        waiting["answer"] = tally(p)
                        waiting["event"].set()
                    return {"ok": True}
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
        if self.command == "GET" and os.environ.get("LOG_GETS") != "1":
            return  # pages poll every second; only log actions (POST) and errors. LOG_GETS=1 shows everything
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
