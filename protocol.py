"""Shared message format + live log. Owner: P3. FROZEN at 12:30: change it only with the whole team.

    Message: {round, from, to, type, content, refs}

`from` is a Python keyword, so the field is `from_`; to_dict() / from_dict() use "from".

post() is the ONE place messages go out:
  - appends to LOG (P1's Streamlit page re-reads LOG every second)
  - PRINT_LOG=1 also prints it; PACE_S=0.5 slows the run so a live demo is readable
  - TRANSPORT=band will also send it to the trip's Band room (wired 1:30-2:30; LOG stays the source of truth)
"""
from __future__ import annotations

import os
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


LOG: list[Message] = []
_lock = threading.Lock()


def post(msg: Message) -> Message:
    with _lock:
        LOG.append(msg)
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


_band_warned = False


def _send_to_band(msg: Message) -> None:
    """TODO(P3, 1:30-2:30): send msg to the trip's Band room, @mentioning msg.to, with round/type/refs in a
    readable prefix like "[objection · R2 · refs: stay_inn] ...". Until then messages stay local."""
    global _band_warned
    if not _band_warned:
        print("[protocol] TRANSPORT=band is not wired yet; messages stay in LOG only", flush=True)
        _band_warned = True
