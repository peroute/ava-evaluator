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
| **Produces** | a report dict (`stats`, `conversations`, `turns`, `flagged_claims`, `unjudged_claims`) → plain text, or JSON from the API |
| **Writes** | nothing |

## How the code works

### The views (defined in migration 0001)

| View | Level | What it adds |
|---|---|---|
| [`v_claim_results`](../../ava_eval/db/migrations/versions/0003_kb_structure.py) | claim | claim + verdict + **evidence chunk text, section, source doc, version date, chunk kind**, and `kb_changed_since_judged` (LEFT JOINs, so unjudged claims still appear). Redefined in migration 0003 |
| [`v_turn_severity`](../../ava_eval/db/migrations/versions/0004_unjudged_claims.py) | helper | per turn: counts of each verdict, `unjudged_count` (claims with no verdict), and `MAX(severity)` where CONTRADICTED=3, NOT_FOUND=2, SUPPORTED=1. `LEFT JOIN`s verdicts since migration 0004, so `claim_count` counts every claim |
| [`v_turn_results`](../../ava_eval/db/migrations/versions/0004_unjudged_claims.py) | turn | **the worst verdict wins**, plus `unjudged_count`. Turns without verdicts are labelled, never dropped (see below) |
| [`v_conversation_summary`](../../ava_eval/db/migrations/versions/0004_unjudged_claims.py) | conversation | counts of contradicted / not_found / supported / skipped / **unjudged** turns, and includes rejected conversations |

The three rollup views were created in 0001 and redefined in migration 0004.

`turn_verdict` labels for turns that have no verdicts, in the order they're checked:

| Label | Meaning |
|---|---|
| `UNJUDGED` | the turn has claims, but step 6 failed on all of them. Re-run `evaluate`. (A turn with *some* verdicts shows its worst verdict and `unjudged_count > 0`) |
| `NOT_EVALUATED` | step 3 said it's filler |
| `NO_CLAIMS` | step 3 flagged it, but step 4 found nothing to check |
| `UNCLASSIFIED` | `evaluate` hasn't run on it yet |

### The Python

- [`conversation_report`](../../ava_eval/steps/s7_aggregate.py#L37) collects [`verdict_stats`](../../ava_eval/db/store.py#L669)
  (its `totals` include `unjudged`), [`conversation_summary`](../../ava_eval/db/store.py#L609), [`turn_results`](../../ava_eval/db/store.py#L619),
  flagged claims via [`claim_results`](../../ava_eval/db/store.py#L629) (CONTRADICTED first, then NOT_FOUND, up to 500 each),
  and unjudged claims via `claim_results(unjudged=True)`.
- [`format_report`](../../ava_eval/steps/s7_aggregate.py#L67) prints the text report: a `[!] N claims are UNJUDGED` header
  line when there are any, an `unjudged N` column per conversation, and an "Unjudged claims" section before the flagged
  ones. [`_bar`](../../ava_eval/steps/s7_aggregate.py#L56) draws the verdict mix as `#` (contradicted) `~` (not found) `.` (supported).

### The API (read-only)

| Endpoint | Returns |
|---|---|
| `GET /health` | status, chunk count, **stub flags**, model name |
| `GET /stats` | totals, counts by verdict and by tag |
| `GET /conversations` | `v_conversation_summary` |
| `GET /conversations/{id}` | summary + turns + claims for one conversation |
| `GET /claims?verdict=&tag=&subject=&session=&unjudged=&limit=&offset=` | filterable `v_claim_results`, which is what a frontend mostly uses. `unjudged=true` returns only claims with no verdict |
| `GET /claims/{id}` | one claim + its attempt 1 and attempt 2 retrievals |

Interactive docs at `http://127.0.0.1:8000/docs`.

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
- **Evidence can go stale after a re-index.** Chunk ids are positional (`t11.r02`), so when TTU edits the document the
  same id can name a different passage. `v_claim_results.kb_changed_since_judged` compares the verdict's stored KB hash
  with the current one, and the report prints `[!] the knowledge base changed since this verdict` on those rows. It
  warns rather than deletes, by design: re-run `evaluate` to refresh them. Verdicts saved before migration 0003 have no
  hash and are always flagged.
- CORS is `allow_origins=["*"]` for local development. Restrict it before the API is reachable from anywhere real.

## Tests

[`tests/test_retrieve_and_rollup.py`](../../tests/test_retrieve_and_rollup.py): turn verdict is the worst of its claims,
turns with no claims are labelled not dropped, conversation summary counts each severity separately, **re-judging a
claim updates the rollup**, tags are counted, the database rejects an unknown verdict, and (migration 0004) a turn whose
claims all failed reads `UNJUDGED` not `NO_CLAIMS`, a partly judged turn keeps its worst verdict and reports the gap,
and unjudged claims are counted in the summary, the stats and the `unjudged` filter.

## Connects to

- **Previous:** [Step 6](s6-judge.md) wrote `verdicts`. Everything here derives from it plus the step 1–4 tables.
- **Where it's called:** [`cmd_report`](../../ava_eval/cli.py#L112), [`cmd_serve`](../../ava_eval/cli.py#L120). In
  Docker, the `api` container serves it after migrating ([Operations](../operations.md)).
- **See it end to end:** [Walkthrough](../walkthrough.md).
