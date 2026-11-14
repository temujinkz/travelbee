# travelbee: Group Trip Crew

Group trip planning is a negotiation. Each traveler gets an AI Advocate agent with its own memory; the agents negotiate a fair plan and only ask humans on a deadlock or for final approval.

Built at "Hive Mind: Building Agent Teams" (cognee), Nov 14 2026.

**Team plan, data formats and function contracts:** [TEAM_SPLIT.md](TEAM_SPLIT.md)

## Superlinked models (SIE)

`sie.py` is a stdlib client for the Superlinked Inference Engine. Set `SUPERLINKED_API_KEY` (optional: `SIE_BASE_URL`, default `https://api.superlinked.com`). Without a key everything falls back to the local mocks and scorer.

| Where | Model | Endpoint |
|---|---|---|
| `ranking.py` `rank()` shortlist | Qwen3 Embedding 4B | `/v1/encode` |
| `ranking.py` `rank()` reorder | Qwen3 Reranker 4B | `/v1/score` |
| `researcher.py` agent (tool calling) | GLM-5.3-Flash | `/v1/chat/completions` |
| `itinerary.py` final JSON | Qwen3.8 27B | `/v1/chat/completions` |

Model IDs can be overridden with `SIE_EMBED_MODEL`, `SIE_RERANK_MODEL`, `SIE_AGENT_MODEL`, `SIE_WRITER_MODEL`.

## Web search (Keenable)

`keenable.py` calls Keenable's `/v1/search`. Set `KEENABLE_API_KEY`; the research agent in `researcher.py` then also gets a `web_search` tool to check current facts (opening times, seasonal closures). Search snippets are treated as untrusted text. Without the key the tool is simply not offered.

## Research agent (optional)

`researcher.py` is the real-data layer (Google Flights fares, Keenable stays and activities, cached in `demo/cached_options.json`). `RESEARCH_AGENT=1` puts GLM-5.3-Flash in front of it (`research_agent.py`): the model picks the searches and can call `web_search` to check opening hours or closures, but options still come only from `researcher.research()`. Off by default so the demo stays fast and replayable.
