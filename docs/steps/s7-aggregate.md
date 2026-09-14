# Step 7 — Roll up and report

[← Step 6](s6-judge.md) · [Overview](../overview.md) · [Walkthrough →](../walkthrough.md)

**Command:** `python -m ava_eval report [--session ID]` and `python -m ava_eval serve` · **Code:** [`ava_eval/steps/s7_aggregate.py`](../../ava_eval/steps/s7_aggregate.py), views in [`0001_initial_schema.py`](../../ava_eval/db/migrations/versions/0001_initial_schema.py#L148), [`ava_eval/api/main.py`](../../ava_eval/api/main.py)

## What it does

Turns claim verdicts into answers at three levels: *which claims were flagged*, *which turns had a problem*, *how did
each conversation do*. Then prints them in the terminal or serves them over HTTP for a frontend.

There's almost no Python logic here, and that's on purpose. The rollups are **SQL views**: saved queries that
recompute from the underlying rows every time you read them. This module only reads and formats them.

## Inputs → outputs

| | |
|---|---|
| **Reads** | `v_claim_results`, `v_turn_results`, `v_conversation_summary`, plus counts from `verdicts` |
| **Produces** | a report dict (`stats`, `conversations`, `turns`, `flagged_claims`) → plain text, or JSON from the API |
| **Writes** | nothing |

## How the code works

### The views (defined in migration 0001)

| View | Level | What it adds |
|---|---|---|
| [`v_claim_results`](../../ava_eval/db/migrations/versions/0001_initial_schema.py#L157) | claim | claim + verdict + **evidence chunk text, section, source doc, version date** (LEFT JOINs, so unjudged claims still appear) |
| [`v_turn_severity`](../../ava_eval/db/migrations/versions/0001_initial_schema.py#L182) | helper | per turn: counts of each verdict and `MAX(severity)` where CONTRADICTED=3, NOT_FOUND=2, SUPPORTED=1 |
| [`v_turn_results`](../../ava_eval/db/migrations/versions/0001_initial_schema.py#L203) | turn | **the worst verdict wins**. Turns without verdicts are labelled, never dropped (see below) |
| [`v_conversation_summary`](../../ava_eval/db/migrations/versions/0001_initial_schema.py#L230) | conversation | counts of contradicted / not_found / supported / skipped turns, and includes rejected conversations |

`turn_verdict` labels for turns that have no verdicts:

| Label | Meaning |
|---|---|
| `NOT_EVALUATED` | step 3 said it's filler |
| `NO_CLAIMS` | step 3 flagged it, but step 4 found nothing to check |
| `UNCLASSIFIED` | `evaluate` hasn't run on it yet |

### The Python

- [`conversation_report`](../../ava_eval/steps/s7_aggregate.py#L36) collects [`verdict_stats`](../../ava_eval/db/store.py#L646),
  [`conversation_summary`](../../ava_eval/db/store.py#L592), [`turn_results`](../../ava_eval/db/store.py#L602), and
  flagged claims via [`claim_results`](../../ava_eval/db/store.py#L612) (CONTRADICTED first, then NOT_FOUND, up to 500 each).
- [`format_report`](../../ava_eval/steps/s7_aggregate.py#L61) prints the text report. [`_bar`](../../ava_eval/steps/s7_aggregate.py#L50)
  draws the verdict mix as `#` (contradicted) `~` (not found) `.` (supported).

### The API (read-only)

| Endpoint | Returns |
|---|---|
| `GET /health` | status, chunk count, **stub flags**, model name |
| `GET /stats` | totals, counts by verdict and by tag |
| `GET /conversations` | `v_conversation_summary` |
| `GET /conversations/{id}` | summary + turns + claims for one conversation |
| `GET /claims?verdict=&tag=&subject=&session=&limit=&offset=` | filterable `v_claim_results`, which is what a frontend mostly uses |
| `GET /claims/{id}` | one claim + its attempt 1 and attempt 2 retrievals |

Interactive docs at `http://127.0.0.1:8000/docs`.

### The dashboard (read-only)

`http://127.0.0.1:8000/ui/` (and `/`, which redirects there) serves a small dashboard from the same app: plain
HTML/CSS/JS in [`ava_eval/api/static/`](../../ava_eval/api/static/), no build step, no extra container. It only calls
`GET /health`, `GET /conversations` and `GET /conversations/{id}`:

- **Conversations:** status (with the rejection note), source file, ingest time, turn and verdict counts, and a banner
  when `/health` reports stub mode.
- **Conversation detail:** summary, every turn with its Step 3 state, and the conversation's claims with verdict, tag
  and evidence. Step 3 state comes from `turn_verdict` first, because `contains_claims` is also `false` for turns
  that were never classified: `UNCLASSIFIED` → *Not classified*, `NOT_EVALUATED` → *Filler / no checkable claim*.

## Why it's built this way

- **Views, not summary tables.** A stored summary goes stale when a claim is re-judged. A view recomputes on every
  read, so claim, turn and conversation results can never disagree.
- **`CONTRADICTED` and `NOT_FOUND` are never merged into one "flagged" count.** They go to different people: a
  contradiction is an Ava behaviour fix, a gap is usually a TTU content fix. One combined number is something neither team can act on.
- **Worst-first ordering.** That's the order a reviewer wants to read problems in.
- **The API can't change anything.** Verdicts are evidence, not an editable document. Evaluation happens through the CLI.
- **Stub flags are exposed on `/health`,** so a frontend can warn that it's showing keyword-heuristic output, not model verdicts.

## Gotchas

- **Severity is defined in two places:** [`SEVERITY` in models.py](../../ava_eval/models.py#L59) and the `CASE` in
  `v_turn_severity`. Change both together, and change the view through a **new** migration (`CREATE OR REPLACE VIEW`),
  never by editing 0001.
- `report --session X` filters conversations, turns and flagged claims, but the **header totals (`stats`) are always
  across every conversation**.
- CORS is `allow_origins=["*"]` for local development. Restrict it before the API is reachable from anywhere real.

## Tests

[`tests/test_retrieve_and_rollup.py`](../../tests/test_retrieve_and_rollup.py): turn verdict is the worst of its claims,
turns with no claims are labelled not dropped, conversation summary counts each severity separately, **re-judging a
claim updates the rollup**, tags are counted, the database rejects an unknown verdict.

## Connects to

- **Previous:** [Step 6](s6-judge.md) wrote `verdicts`. Everything here derives from it plus the step 1–4 tables.
- **Where it's called:** [`cmd_report`](../../ava_eval/cli.py#L112), [`cmd_serve`](../../ava_eval/cli.py#L120). In
  Docker, the `api` container serves it after migrating ([Operations](../operations.md)).
- **See it end to end:** [Walkthrough](../walkthrough.md).
