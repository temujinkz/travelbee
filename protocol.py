"""Shared message format + live log. Owner: P3. FROZEN at 12:30: change it only with the whole team.

    Message: {round, from, to, type, content, refs}

`from` is a Python keyword, so the field is `from_`; to_dict() / from_dict() use "from".

post() is the ONE place messages go out:
  - appends to LOG (P1's Streamlit page re-reads LOG every second)
  - PRINT_LOG=1 also prints it; PACE_S=0.5 slows the run so a live demo is readable
  - TRANSPORT=band also sends it to the trip's Band room (band.py; LOG stays the source of truth)
Settings can live in a .env file next to this one (see .env.example); real environment variables win.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field

TYPES = (
    "proposal", "objection", "concession",
    "research_request", "research_result",
    "decision", "human_request", "human_response",
)


@dataclass
class Message:
    round: int
    from_: str
    to: str
    type: str
    content: str
    refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.type not in TYPES:
            raise ValueError(f"unknown message type {self.type!r}; expected one of {TYPES}")

    def to_dict(self) -> dict:
        return {"round": self.round, "from": self.from_, "to": self.to,
                "type": self.type, "content": self.content, "refs": list(self.refs)}

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        return cls(d["round"], d["from"], d["to"], d["type"], d["content"], list(d.get("refs") or []))


def _load_dotenv(path: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")) -> None:
    """Read KEY=value lines from .env into the environment (real env vars win). No dependency."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                value = re.split(r"\s+#", value, maxsplit=1)[0]  # drop inline "   # comment"
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

LOG: list[Message] = []
_lock = threading.Lock()


LISTENERS: list = []  # callables fn(msg), e.g. web/bridge.py copies every message into the trip's web log


def post(msg: Message) -> Message:
    with _lock:
        LOG.append(msg)
    for fn in list(LISTENERS):
        try:
            fn(msg)
        except Exception as exc:  # a broken listener must never stop the negotiation
            print(f"[protocol] listener error: {exc!r}", flush=True)
    if os.getenv("PRINT_LOG") == "1":
        print(f"[R{msg.round}] {msg.from_} → {msg.to}  {msg.type.upper()}: {msg.content}", flush=True)
    if os.getenv("TRANSPORT", "local") == "band":
        _send_to_band(msg)
    pace = float(os.getenv("PACE_S") or 0)
    if pace > 0:
        time.sleep(pace)
    return msg


def clear_log() -> None:
    with _lock:
        LOG.clear()


def _send_to_band(msg: Message) -> None:
    """Mirror msg into the trip's Band room (see band.py). Non-blocking; failures never affect LOG."""
    import band

    band.send(msg)
