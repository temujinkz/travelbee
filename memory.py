"""Agent memory. Owner: P2.

    remember(dataset, text)        save a fact in "user_<id>" or "trip_<id>"
    recall(dataset, query) -> list[str]
    improve(dataset)               consolidate feedback into the permanent graph
    forget(dataset)

Two backends behind the same calls:
  - dict:   always on. Keyword recall, instant, no dependencies. It is also the safety net for cognee.
  - cognee: used when `import cognee` works (cognee needs Python >= 3.10) and MEMORY_BACKEND != "dict".
            Session memory works with no API key; improve() builds the permanent graph and needs LLM_API_KEY.
Every cognee call is wrapped: if it fails or is slow, we log once and answer from the dict, so the demo never dies.

cognee is async; we run it on one private event loop in a background thread, so it works from the
Coordinator's thread and from Streamlit alike.
"""
from __future__ import annotations

import asyncio
import os
import re
import threading

TIMEOUT_S = float(os.getenv("MEMORY_TIMEOUT_S") or 8)

_DICT: dict[str, list[str]] = {}
_lock = threading.Lock()

LAST_SOURCE: dict[str, str] = {}  # dataset -> "cognee" | "dict", for the UI / log ("recalled from cognee")


# ---------------------------------------------------------------- cognee plumbing
def _load_cognee():
    if os.getenv("MEMORY_BACKEND", "auto") == "dict":
        return None
    try:
        import cognee  # noqa: F401
        return cognee
    except Exception as exc:  # not installed, or Python too old
        print(f"[memory] cognee unavailable ({type(exc).__name__}: {exc}); using dict memory", flush=True)
        return None


_cognee = _load_cognee()
_loop: asyncio.AbstractEventLoop | None = None
_warned: set[str] = set()


def _run(coro_fn, *args, **kwargs):
    """Run an async cognee call on our private loop and wait for it. Returns None on any failure."""
    global _loop
    if _cognee is None:
        return None
    if _loop is None:
        _loop = asyncio.new_event_loop()
        threading.Thread(target=_loop.run_forever, daemon=True, name="cognee-loop").start()
    name = getattr(coro_fn, "__name__", "call")
    try:
        fut = asyncio.run_coroutine_threadsafe(coro_fn(*args, **kwargs), _loop)
        return fut.result(timeout=TIMEOUT_S)
    except Exception as exc:
        if name not in _warned:
            _warned.add(name)
            print(f"[memory] cognee.{name} failed ({type(exc).__name__}: {exc}); falling back to dict", flush=True)
        return None


def _as_texts(result) -> list[str]:
    """cognee returns strings, dicts or objects depending on the search type; flatten to strings."""
    if result is None:
        return []
    if isinstance(result, (str, bytes)):
        return [result if isinstance(result, str) else result.decode()]
    if isinstance(result, dict):
        for key in ("text", "content", "answer", "search_result", "results"):
            if key in result:
                return _as_texts(result[key])
        return [str(result)]
    if isinstance(result, (list, tuple)):
        out: list[str] = []
        for r in result:
            out.extend(_as_texts(r))
        return out
    for attr in ("text", "content", "answer", "search_result"):
        if hasattr(result, attr):
            return _as_texts(getattr(result, attr))
    return [str(result)]


# ---------------------------------------------------------------- dict recall
_STOP = {"the", "and", "for", "with", "that", "this", "was", "are", "what", "does", "about", "from"}


def _words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9\-]+", s.lower()) if len(w) > 2 and w not in _STOP]


def _dict_recall(dataset: str, query: str, top_k: int) -> list[str]:
    with _lock:
        facts = list(_DICT.get(dataset, []))
    words = _words(query)
    if not words:
        return facts[-top_k:]
    scored = [(sum(w in f.lower() for w in words), i, f) for i, f in enumerate(facts)]
    hits = [x for x in scored if x[0] > 0]
    hits.sort(key=lambda x: (-x[0], -x[1]))  # best match first, newest first on ties
    return [f for _, _, f in hits[:top_k]]


# ---------------------------------------------------------------- public API (P3's contract)
def remember(dataset: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    with _lock:
        if text in _DICT.setdefault(dataset, []):
            return
        _DICT[dataset].append(text)
    if _cognee is not None:
        _run(_cognee.remember, text, dataset_name=dataset, session_id=dataset)


def recall(dataset: str, query: str, top_k: int = 5) -> list[str]:
    """Facts relevant to `query`. The dict hits always come back (they are exact); cognee adds whatever
    its graph/session search finds on top."""
    local = _dict_recall(dataset, query, top_k)
    extra: list[str] = []
    if _cognee is not None:
        extra = _as_texts(_run(_cognee.recall, query, session_id=dataset, top_k=top_k))
    LAST_SOURCE[dataset] = "cognee" if extra else "dict"
    out, seen = [], set()
    for f in local + extra:
        key = f.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(f.strip())
    return out[:top_k]


def improve(dataset: str) -> None:
    """Fold this dataset's session memory into cognee's permanent graph. No-op on the dict backend,
    or when there is no LLM_API_KEY (graph building needs an LLM)."""
    if _cognee is None or not os.getenv("LLM_API_KEY"):
        return
    _run(_cognee.improve, dataset=dataset, session_ids=[dataset])


def forget(dataset: str) -> None:
    with _lock:
        _DICT.pop(dataset, None)
    LAST_SOURCE.pop(dataset, None)
    if _cognee is not None:
        _run(_cognee.forget, dataset=dataset)


def dump(dataset: str) -> list[str]:
    """Everything stored for a dataset (dict view). Handy for P1's memory panel and for debugging."""
    with _lock:
        return list(_DICT.get(dataset, []))


def backend() -> str:
    return "cognee" if _cognee is not None else "dict"


if __name__ == "__main__":
    forget("user_demo")
    remember("user_demo", "Ben rejected 1-stop via ATL, 6:15am because: no layovers")
    remember("user_demo", "Ben's dealbreakers: long-drive")
    print("backend:", backend())
    print("recall 'rejected because':", recall("user_demo", "rejected because"))
    print("recall 'dealbreakers':", recall("user_demo", "dealbreakers"))
