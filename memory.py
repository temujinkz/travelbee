"""Agent memory. Owner: P2.

    remember(dataset, text)        save a fact in "user_<id>" or "trip_<id>"
    recall(dataset, query) -> list[str]
    improve(dataset)               consolidate feedback into the permanent graph
    forget(dataset)

Datasets (memory layout):
  user_<id>     one per traveler: that traveler's own Advocate writes it; other Advocates may read it
  trip_<id>     the group's shared memory (also the Band room's memory): decisions, group rejections,
                notes any Advocate shares. Every Advocate reads it.

Backends behind the same calls:
  - dict:         always on. Keyword recall, instant, no dependencies. Safety net for cognee.
  - cognee cloud: when COGNEE_SERVICE_URL + COGNEE_API_KEY are set (in the environment or in .env next to
                  this file). The whole team then shares one memory on the tenant, and improve() runs server-side.
  - cognee local: when cognee is importable (Python >= 3.10) but no cloud URL. Session memory needs no key;
                  improve() needs LLM_API_KEY.
MEMORY_BACKEND=dict forces the dict. Every cognee call is wrapped: if it fails or is slow, we log once and
answer from the dict, so the demo never dies.

cognee is async; we run it on one private event loop in a background thread, so it works from the
Coordinator's thread and from Streamlit alike.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path: str) -> None:
    """Minimal .env reader (KEY=value lines). Real environment variables win."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            val = re.split(r"\s+#", val, maxsplit=1)[0]  # drop inline "   # comment" (as protocol.py does)
            os.environ.setdefault(key, val.strip().strip('"').strip("'"))


_load_dotenv(os.path.join(_HERE, ".env"))
if os.getenv("COGNEE_BASE_URL") and not os.getenv("COGNEE_SERVICE_URL"):  # the Claude plugin's name for it
    os.environ["COGNEE_SERVICE_URL"] = os.environ["COGNEE_BASE_URL"]

CLOUD = bool(os.getenv("COGNEE_SERVICE_URL"))
TIMEOUT_S = float(os.getenv("MEMORY_TIMEOUT_S") or (20 if CLOUD else 15))

# The dict store is saved to a JSON file so separate processes share one memory: the Coordinator run,
# P1's app, and the Band Advocate agents (which call p2cli.py). MEMORY_FILE="" keeps it in-process only.
MEMORY_FILE = os.getenv("MEMORY_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "memory.json"))

_DICT: dict[str, list[str]] = {}
_lock = threading.Lock()
_mtime = 0.0


def _load() -> None:
    """Pick up writes from other processes (cheap: only re-reads when the file changed)."""
    global _DICT, _mtime
    if not MEMORY_FILE or not os.path.exists(MEMORY_FILE):
        return
    m = os.path.getmtime(MEMORY_FILE)
    if m == _mtime:
        return
    try:
        with open(MEMORY_FILE, encoding="utf-8") as f:
            _DICT = {k: list(v) for k, v in json.load(f).items()}
        _mtime = m
    except (OSError, ValueError):
        pass  # half-written by another process: keep what we have, retry next call


def _save() -> None:
    global _mtime
    if not MEMORY_FILE:
        return
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    tmp = f"{MEMORY_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_DICT, f, ensure_ascii=False, indent=1)
    os.replace(tmp, MEMORY_FILE)  # atomic, so readers never see half a file
    _mtime = os.path.getmtime(MEMORY_FILE)

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


def _connect() -> bool:
    """Route every cognee call to the Cognee Cloud tenant. On failure, fall back to the dict for this process."""
    global _cognee
    url = os.environ["COGNEE_SERVICE_URL"].rstrip("/")
    try:
        fut = asyncio.run_coroutine_threadsafe(_cognee.serve(url=url, api_key=os.getenv("COGNEE_API_KEY", "")), _loop)
        fut.result(timeout=TIMEOUT_S)
        return True
    except Exception as exc:
        print(f"[memory] could not connect to Cognee Cloud at {url} ({type(exc).__name__}: {exc}); "
              "using dict memory", flush=True)
        _cognee = None
        return False


def _improving() -> bool:
    """improve() needs an LLM: server-side on Cognee Cloud, LLM_API_KEY locally."""
    return CLOUD or bool(os.getenv("LLM_API_KEY"))


def _run(coro_fn, *args, **kwargs):
    """Run an async cognee call on our private loop and wait for it. Returns None on any failure."""
    global _loop
    if _cognee is None:
        return None
    if _loop is None:
        _loop = asyncio.new_event_loop()
        threading.Thread(target=_loop.run_forever, daemon=True, name="cognee-loop").start()
        if CLOUD and not _connect():
            return None
    if _cognee is None:  # cloud connection failed: dict only from now on
        return None
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
        _load()
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
        _load()
        if text in _DICT.setdefault(dataset, []):
            return
        _DICT[dataset].append(text)
        _save()
    if _cognee is not None:
        _run(_cognee.remember, text, dataset_name=dataset, session_id=dataset, self_improvement=_improving())


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
    or locally without LLM_API_KEY (graph building needs an LLM; Cognee Cloud has its own)."""
    if _cognee is None or not _improving():
        return
    _run(_cognee.improve, dataset=dataset, session_ids=[dataset])


def forget(dataset: str) -> None:
    with _lock:
        _load()
        _DICT.pop(dataset, None)
        _save()
    LAST_SOURCE.pop(dataset, None)
    if _cognee is not None:
        _run(_cognee.forget, dataset=dataset)


def dump(dataset: str) -> list[str]:
    """Everything stored for a dataset (dict view). Handy for P1's memory panel and for debugging."""
    with _lock:
        _load()
        return list(_DICT.get(dataset, []))


def backend() -> str:
    if _cognee is None:
        return "dict"
    return "cognee-cloud" if CLOUD else "cognee-local"


if __name__ == "__main__":
    forget("user_demo")
    remember("user_demo", "Ben rejected 1-stop via ATL, 6:15am because: no layovers")
    remember("user_demo", "Ben's dealbreakers: long-drive")
    print("backend:", backend())
    print("recall 'rejected because':", recall("user_demo", "rejected because"))
    print("recall 'dealbreakers':", recall("user_demo", "dealbreakers"))
