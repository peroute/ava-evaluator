# Ava Evaluator — Phase 1

Automated checking of whether **Ava** (TTU Online's ElevenLabs voice agent) says
things that are actually backed by TTU's knowledge base.

It reads exported call transcripts, breaks each of Ava's answers into individual
checkable statements, looks each one up in the knowledge base, and records
whether the KB **supports** it, **contradicts** it, or **says nothing about it**.

| Verdict | Means | Who fixes it |
|---|---|---|
| `CONTRADICTED` | The KB says something incompatible with what Ava said | Ava's prompting/behaviour |
| `NOT_FOUND` + `kb_gap` | The KB has nothing on this, even after a wider search | TTU's content team |
| `SUPPORTED` | The KB backs it up | Nobody |
| `SUPPORTED`/`CONTRADICTED` + `retrieval_miss` | The evidence existed, but only the wider retry search found it | This evaluator's tuning (the verdict itself still applies) |

Tags only appear on claims whose first verdict was `NOT_FOUND`. A claim is never `NOT_FOUND` + `retrieval_miss`.

## Documentation

| Doc | For |
|---|---|
| [Overview](docs/overview.md) | The big picture: problem, pipeline, architecture, data model, glossary. **Start here.** |
| [Walkthrough](docs/walkthrough.md) | One real conversation traced through all 7 steps, with the actual database rows |
| Steps: [1](docs/steps/s1-ingest.md) · [2](docs/steps/s2-index-kb.md) · [3](docs/steps/s3-classify.md) · [4](docs/steps/s4-decompose.md) · [5](docs/steps/s5-retrieve.md) · [6](docs/steps/s6-judge.md) · [7](docs/steps/s7-aggregate.md) | How each step works in code, why it's built that way, gotchas, tests |
| [Operations](docs/operations.md) | Configuration, Docker, database, migrations, tests, going live |

## Pipeline

```
transcripts ──▶ 1. ingest ──▶ 3. classify ──(nothing checkable)──▶ stored, not evaluated
                                  │
                             (has claims)
                                  ▼
                          4. decompose into self-contained claims
                                  ▼
                          5. retrieve KB chunks per claim
                                  ▼
                          6. judge: SUPPORTED / CONTRADICTED / NOT_FOUND
                                  ▼
                          7. roll up to turn + conversation level

KB documents ──▶ 2. index  (separate cadence: runs when the KB changes)
```

## Quick start

Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/) running.

### Option A — everything in Docker

```powershell
copy .env.example .env         # works offline as-is; add a key when going live
docker compose up --build      # Postgres + migrations + API on http://127.0.0.1:8000
```

In a second terminal, run the pipeline inside the same image:

```powershell
docker compose run --rm api python scripts/make_sample_kb.py
docker compose run --rm api python -m ava_eval ingest
docker compose run --rm api python -m ava_eval index-kb
docker compose run --rm api python -m ava_eval evaluate
docker compose run --rm api python -m ava_eval report
```

### Option B — Postgres in Docker, Python on your machine

Faster edit-run loop while developing.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

copy .env.example .env
docker compose up -d db        # just the database, on 127.0.0.1:5433
python scripts\make_sample_kb.py

python -m ava_eval migrate
python -m ava_eval ingest
python -m ava_eval index-kb
python -m ava_eval evaluate
python -m ava_eval report
python -m ava_eval serve       # http://127.0.0.1:8000  (dashboard at /ui, API docs at /docs)
```

Both run the whole pipeline with **no API key and no network calls** (`AVA_STUB_LLM=1`,
`AVA_STUB_EMBED=1`), on the sample data in `sample_data/`. **Stubbed output is not a real
verdict.** Every command prints its mode. See [stub vs live](docs/overview.md#stub-mode-vs-live-mode).

## Commands

| Command | Step | When you run it |
|---|---|---|
| `migrate` | — | After cloning, and whenever someone adds a migration |
| `ingest --input DIR` | 1 | When new transcripts arrive |
| `index-kb --kb-dir DIR [--force]` | 2 | When TTU edits a knowledge base document |
| `evaluate [--session ID]` | 3–6 | To produce verdicts |
| `report [--session ID]` | 7 | To read the results |
| `serve [--port N]` | — | To browse results in the dashboard (`/ui`) or query the API |

They are separate commands because every stage's output is saved to the
database. You can re-run step 6 after changing the judge prompt without
re-ingesting, re-chunking, or re-embedding anything.

## Tests

```powershell
docker compose up -d db
python -m pytest
```

Runs offline against the separate `ava_eval_test` database. Details in [Operations](docs/operations.md#tests).
