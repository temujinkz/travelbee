"""Thin client for the Superlinked Inference Engine (SIE) HTTP API. Stdlib only.

    SUPERLINKED_API_KEY   required for live calls (without it available() is False and callers fall back)
    SIE_BASE_URL          default https://api.superlinked.com
    SIE_AUTH_HEADER       default "Authorization"; value is "Bearer <key>" (set to e.g. "X-API-Key" for a raw key)
    SIE_TIMEOUT_S         default 30

Model IDs (override per env var if the catalog names differ):
    SIE_EMBED_MODEL   Qwen/Qwen3-Embedding-4B      /v1/encode/:model
    SIE_RERANK_MODEL  Qwen/Qwen3-Reranker-4B       /v1/score/:model
    SIE_AGENT_MODEL   zai-org/GLM-5.3-Flash:relay       /v1/chat/completions  (research agent, tool calling)
    SIE_WRITER_MODEL  Qwen/Qwen3.8-27B-FP8         /v1/chat/completions  (itinerary, structured JSON)

Wire format: JSON in, JSON out (Accept: application/json); SIE's default is msgpack.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

EMBED_MODEL = os.getenv("SIE_EMBED_MODEL", "Qwen/Qwen3-Embedding-4B")
RERANK_MODEL = os.getenv("SIE_RERANK_MODEL", "Qwen/Qwen3-Reranker-4B")
AGENT_MODEL = os.getenv("SIE_AGENT_MODEL", "zai-org/GLM-5.3-Flash:relay")
WRITER_MODEL = os.getenv("SIE_WRITER_MODEL", "Qwen/Qwen3.8-27B-FP8")


class SIEError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status, self.code = status, code


def available() -> bool:
    return bool(os.getenv("SUPERLINKED_API_KEY")) and os.getenv("SIE_DISABLE") != "1"


def _post(path: str, body: dict) -> dict:
    key = os.getenv("SUPERLINKED_API_KEY")
    if not key:
        raise SIEError("SUPERLINKED_API_KEY is not set")
    header = os.getenv("SIE_AUTH_HEADER", "Authorization")
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               header: f"Bearer {key}" if header.lower() == "authorization" else key}
    url = os.getenv("SIE_BASE_URL", "https://api.superlinked.com").rstrip("/") + path
    req = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=float(os.getenv("SIE_TIMEOUT_S") or 30)) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail", {})
            raise SIEError(f"{path}: {detail.get('message', exc.reason)}", exc.code, detail.get("code")) from None
        except (ValueError, AttributeError):
            raise SIEError(f"{path}: HTTP {exc.code} {exc.reason}", exc.code) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise SIEError(f"{path}: {exc}") from None


def encode(texts: list[str], *, is_query: bool = False, instruction: str | None = None,
           model: str | None = None) -> list[list[float]]:
    """Dense embeddings, one vector per text."""
    params: dict = {"output_types": ["dense"]}
    if instruction and is_query:
        params["instruction"] = instruction
    out = _post(f"/v1/encode/{model or EMBED_MODEL}",
                {"items": [{"id": str(i), "text": t} for i, t in enumerate(texts)], "params": params})
    return [it["dense"]["values"] for it in out["items"]]


def score(query: str, texts: list[str], *, instruction: str | None = None,
          model: str | None = None) -> list[float]:
    """Reranker relevance of each text against the query, returned in the INPUT order."""
    body: dict = {"query": {"id": "q", "text": query},
                  "items": [{"id": str(i), "text": t} for i, t in enumerate(texts)]}
    if instruction:
        body["instruction"] = instruction
    out = _post(f"/v1/score/{model or RERANK_MODEL}", body)
    scores = [0.0] * len(texts)
    for entry in out["scores"]:
        scores[int(entry["item_id"])] = float(entry["score"])
    return scores


def chat(messages: list[dict], *, model: str | None = None, tools: list[dict] | None = None,
         tool_choice: str | None = None, response_format: dict | None = None,
         max_tokens: int = 1500, temperature: float = 0.2) -> dict:
    """OpenAI-compatible chat completion; returns choices[0].message."""
    body: dict = {"model": model or AGENT_MODEL, "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice or "auto"
    if response_format:
        body["response_format"] = response_format
    return _post("/v1/chat/completions", body)["choices"][0]["message"]
