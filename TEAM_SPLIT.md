# Team split: Group Trip Crew

Hackathon: "Hive Mind: Building Agent Teams" (cognee), Sat Nov 14 2026. Submissions close **4:30 PM PT**, demos at 5:00 PM.

Each traveler gets an **Advocate** agent with their own cognee memory (`user_<id>`). The **Researcher** finds real options with H Company and ranks them with Superlinked. The **Coordinator** runs up to 3 negotiation rounds, keeps trip memory in `trip_<id>`, and only asks humans on a deadlock or for final approval. The demo moment: a human rejects an item, feedback goes through `remember()` + `improve()`, and the agents re-plan.

## Checkpoints

| Time | Goal |
|---|---|
| 12:30 | Formats frozen, everyone builds against mocks |
| 1:30 | Each part works alone |
| 2:30 | First end-to-end run |
| 3:30 | Feature freeze, rehearse, record backup video |
| before 4:30 | Submit |

## Shared data formats (frozen at 12:30)

```
User:    {id, name, budget_per_night, home_city,
          answers: {pace, budget, food, activities, lodging, dealbreakers}}
Trip:    {id, destination, start, end, members: [user_ids]}
Option:  {id, type: flight|stay|activity, title, price, url, tags}
Message: {round, from, to, type, content, refs}
         type = proposal|objection|concession|research_request|research_result|decision|human_request|human_response
```

Frozen files, one owner each:

- `protocol.py` (P3): `Message` dataclass plus `LOG: list[Message]`. `from` is a Python keyword, so name the field `from_` and write it out as `from`.
- `demo/users.json` (P1): 4 travelers whose answers conflict.
- `demo/mock_options.json` (P4): about 12 options.
- **One shared tag list**, used by the questionnaire answers, `Option.tags` and the Advocates' rules (e.g. `beach, hostel, red-eye, nonstop, long-drive, airboat, snorkel, fine-dining`). If these drift apart, vetoes silently never fire.

## Function contracts

```python
research(trip, request) -> list[Option]                  # P4; request={type, query, max_price, avoid_tags}
rank(user, options) -> list[tuple[Option, float]]        # P4
evaluate(user, options, round) -> list[Message]          # P2; proposals + objections
concede(user, lost, chosen, round) -> Message | None     # P2
run_trip(trip, users, ask_human) -> plan                 # P3; appends to LOG
replan(trip, users, plan, rejection, ask_human) -> plan  # P3; rejection={option_id, reason, by}
ask_human(question, choices) -> str                      # P1; waits for an answer in the UI
```

## P1: App & onboarding

Files: `app.py`, `data/*.json`, `demo/users.json`

1. Pick-a-user dropdown and questionnaire form, saved as User JSON.
2. Create-trip page (destination, dates, member multiselect), saved as Trip JSON.
3. "Plan our trip" runs `run_trip` in a background thread. The log panel re-reads `LOG` every second (`st.fragment(run_every=1)`). `ask_human` waits on a queue that buttons fill: the deadlock choice, approve, or reject an item with a reason.

Mock: a fake `run_trip` that appends scripted messages every 0.5s and asks one human question.
Later: demo script, backup video, pitch, submission form.

## P2: Memory & Advocates

Files: `memory.py`, `advocate.py`

1. `remember / recall / improve / forget(dataset, …)`, with a plain-dict fallback so others can call it from 12:30.
2. `evaluate`: hard rules from the answers. Over budget or a dealbreaker tag gives an `objection` with refs; otherwise send a `proposal` for the top-ranked option. Call `concede` when someone else vetoed your top pick.
3. Real cognee: questionnaire into `user_<id>`, `recall()` inside `evaluate` (show what it recalled in the message), feedback into `remember` + `improve`.

Checked on cognee 1.6.8:

- `remember(text, dataset_name=, session_id=)`, `recall(query, session_id=, top_k=)`, `improve(dataset=, session_ids=[…])`, `forget(dataset=)`.
- Session memory works with no API key, and recall took 0.4s.
- Building the permanent graph needs `LLM_API_KEY` (OpenAI by default).
- cognee reads the `LLM_*` environment variables, so give our agents' own LLM key a different prefix.

Mock: dict memory; `rank` just sorts by price.

**Status (P2, branch `p2-memory-advocates`): done, without cognee.** Team decision: memory runs on the dict backend
(`MEMORY_BACKEND=dict`), saved to `data/memory.json` so separate processes on one computer share it. The cognee
code path (local or Cognee Cloud via `COGNEE_SERVICE_URL` / `COGNEE_API_KEY`) is still there behind the same calls,
off by default.

- Datasets: `user_<id>` per traveler (only their Advocate writes it), `trip_<id>` shared by the group and the Band room.
  A rejection in `user_<id>` steers only that Advocate; a "The group rejected … because: …" fact in `trip_<id>` steers all.
- Rejection reasons are mapped to the shared tag list (`advocate.TAG_WORDS`), e.g. "no layovers" → `layover`.
- `advocate.py` uses `ranking.rank` (P4) as soon as `ranking.py` exists, else a local score.
- The four Advocates also live in Band as Claude Code agents (instructions in `band/advocate_*.md`, tool `p2cli.py`).
  They run on P2's computer, so memory is per computer: they save rejections the Coordinator announces in the room.

## P3: Coordinator & negotiation

Files: `protocol.py`, `coordinator.py`

1. Commit `protocol.py` by 12:30.
2. `run_trip`: research, then Advocates evaluate. For each type, pick the option with zero objections and the best total score. If every option is vetoed, send a `research_request` with tighter constraints (lowest budget, avoid dealbreaker tags) and start the next round.
3. Still stuck after 3 rounds: `human_request` for the deadlock. Then the final plan goes for approval. A rejection triggers `replan`: feedback into `trip_<id>`, `improve`, drop that item, redo that type.

Mock: Advocates that only check budget, the mock options, and a human that auto-answers. Get `python coordinator.py` printing a full log before touching the UI.

## P4: Researcher & ranking

Files: `researcher.py`, `ranking.py`, `demo/mock_options.json`

1. By 12:30, mock options with conflicts built in, plus `research()` serving them. Everyone else is blocked on this.
   - The cheapest flight is a red-eye.
   - The cheap stay has no beach; the beach stay is too pricey.
   - The Keys snorkel trip is a long drive.
   - A cheaper, closer option only shows up on a second, constrained search.
2. `rank` Python fallback: price against budget, plus how many tags match the answers.
3. H Company on one site (stays): turn its results into Options and save real ones to `demo/cached_options.json` as the demo fallback. Then add Superlinked behind the same `rank` signature; if that takes more than 30 minutes, keep the fallback.

Mock: cached JSON instead of H Company; Python scoring instead of Superlinked.
