# Ava Evaluator — Phase 1

Automated checking of whether **Ava** (TTU Online's ElevenLabs voice agent) says
things that are actually backed by TTU's knowledge base.

It reads exported call transcripts, breaks each of Ava's answers into individual
checkable statements, looks each one up in the knowledge base, and records
whether the KB **supports** it, **contradicts** it, or **says nothing about it**.

The ground truth in this phase is the knowledge base itself — there is no
hand-written answer key. That is a deliberate scope choice: it answers "is Ava
faithful to TTU's documentation?" without anyone having to author a golden
dataset first. Checking whether the documentation is itself correct is Phase 2.

## The three verdicts, and why they stay separate

| Verdict | Means | Who fixes it |
|---|---|---|
| `CONTRADICTED` | The KB says something incompatible with what Ava said | Ava's prompting/behaviour |
| `NOT_FOUND` + `kb_gap` | The KB has nothing on this | TTU's content team |
| `NOT_FOUND` + `retrieval_miss` | The KB *did* cover it; our search missed it first time | This evaluator's tuning |
| `SUPPORTED` | The KB backs it up | Nobody |

These are never collapsed into one "flagged" count, because they route work to
three different people.

## Pipeline

```
transcripts ──▶ 1. ingest ──▶ 3. classify ──(nothing checkable)──▶ stored, not evaluated
                                  │
                             (has claims)
                                  ▼
                          4. decompose into self-contained claims
                                  │
                                  ▼
                          5. retrieve KB chunks per claim
                                  │
                                  ▼
                          6. judge: SUPPORTED / CONTRADICTED / NOT_FOUND
                                  │
                                  ▼
                          7. roll up to turn + conversation level

KB documents ──▶ 2. index  (separate cadence: runs when the KB changes)
```

## Quick start

Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/) running.

### Option A — everything in Docker

```powershell
copy .env.example .env         # works offline as-is; add a key when going live
docker compose up --build      # Postgres + migrations + API on http://localhost:8000
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
docker compose up -d db        # just the database, on localhost:5433
python scripts\make_sample_kb.py

python -m ava_eval migrate
python -m ava_eval ingest
python -m ava_eval index-kb
python -m ava_eval evaluate
python -m ava_eval report
python -m ava_eval serve       # http://127.0.0.1:8000  (interactive docs at /docs)
```

Both run the whole pipeline with **no API key and no network calls**, on the
sample data in `sample_data/`. See "Offline mode" below for what is real and
what is faked.

## Commands

| Command | Step | When you run it |
|---|---|---|
| `migrate` | — | After cloning, and whenever someone adds a migration |
| `ingest --input DIR` | 1 | When new transcripts arrive |
| `index-kb --kb-dir DIR [--force]` | 2 | When TTU edits a knowledge base document |
| `evaluate [--session ID]` | 3–6 | To produce verdicts |
| `report [--session ID]` | 7 | To read the results |
| `serve [--port N]` | — | To let the frontend query results |

They are separate commands because every stage's output is saved to the
database. You can re-run step 6 after changing the judge prompt without
re-ingesting, re-chunking, or re-embedding anything.

## Offline mode

Two switches in `.env` control what actually calls Gemini:

| Setting | Effect |
|---|---|
| `AVA_STUB_LLM=1` | Steps 3, 4 and 6 return canned data instead of calling the model |
| `AVA_STUB_EMBED=1` | Embeddings come from a deterministic local hash instead of the API |

Both default to on, so a fresh clone runs end to end immediately. The stub
embedder is a hashed bag of words rather than random noise, so offline retrieval
returns plausible chunks and the demo shows something meaningful.

**Stubbed output is not a real verdict.** Every command prints its mode, and
`GET /health` reports the flags, so a frontend can say so too.

## Implementing the live steps

Three functions are left deliberately unimplemented. Each has its prompt
written, its output schema defined, and implementation notes in its docstring:

| Step | File | Function |
|---|---|---|
| 3 — classify | `ava_eval/steps/s3_classify.py` | `_classify_live` |
| 4 — decompose | `ava_eval/steps/s4_decompose.py` | `_decompose_live` |
| 6 — judge | `ava_eval/steps/s6_judge.py` | `_judge_live` |

Each is roughly: load the prompt, call `generate_structured(prompt, Schema)`,
return. `generate_structured` already handles retries, temperature 0, and
schema validation.

Set `GEMINI_API_KEY` in `.env`, flip `AVA_STUB_LLM=0`, and implement them one at
a time — re-running `evaluate` after each. Because every stage is persisted, a
failed attempt only costs you that step.

The prompts in `ava_eval/llm/prompts/` are the design work, not boilerplate.
They encode the scope-drift rules, the `subject_tag` requirement, and the judge
rubric. Read them before changing them.

## Design decisions worth knowing

**Chunking is our own, not Ava's.** We deliberately do not try to reproduce
Ava's internal chunk boundaries. Phase 1 asks whether a claim is true according
to the KB's *content*, not whether Ava's retrieval performed well. Copying Ava's
chunking would inherit its blind spots without telling us anything about its
retrieval, which would need matched retrieval logs we do not have.

**Claims must be self-contained.** "The minimum GPA is 2.0" is uncheckable —
2.0 for what? Step 4 resolves each claim against the conversation so it carries
its own scope. This is a deliberate departure from the standard RAGAS-style
"atomic, context-stripped claims" default, which breaks on qualified facts.

**Turn and conversation results are SQL views, not tables.** A stored summary
can go stale when a claim is re-judged. A view cannot, so the three granularities
are incapable of disagreeing.

**`CONTRADICTED` never retries.** Retrying it would let the judge search for a
friendlier chunk elsewhere in the KB, which is exactly how a real hallucination
gets buried.

**Vector search is exact, on purpose.** See Storage.

## Storage

PostgreSQL 17 with the [pgvector](https://github.com/pgvector/pgvector)
extension, run locally by `docker compose`. All SQL lives in `ava_eval/db/store.py`;
the schema lives in Alembic migrations.

**Similarity search is exact.** Embeddings are stored unit-normalised in a
`vector` column, and `store.search_chunks` orders by pgvector's inner-product
operator (`<#>`). There is deliberately **no HNSW/IVFFlat index** on the
column, so PostgreSQL compares every chunk. An approximate index is faster at
very large scale but can skip a true nearest neighbour — and here a skipped
chunk that supports a claim becomes a `NOT_FOUND` reported against Ava, our
retrieval error disguised as hers. At knowledge-base scale an exact scan takes
milliseconds; the Gemini calls around it take seconds. Revisit only past
~100k chunks, and measure recall against exact search before trusting an index.

**Native types and constraints.** Timestamps are `TIMESTAMPTZ`, document
versions `DATE`, `contains_claims` `BOOLEAN`, `scope_derived_from` `JSONB`.
`CHECK` constraints mirror the enums in `models.py`, so a typo'd verdict is
rejected by the database instead of showing up in a report.

**Time zone.** Timestamps are `TIMESTAMPTZ`: an absolute instant, written in UTC
by `store.utcnow()`. They are *displayed* in Lubbock time (`America/Chicago`,
so -05:00 in summer and -06:00 in winter). App connections take the zone from
`AVA_DISPLAY_TIMEZONE`; migration `0002` makes it the database default, so
pgAdmin and psql match (reconnect after migrating).

**Transactions.** A failed statement in PostgreSQL aborts the rest of its
transaction. `evaluate` wraps each conversation in a savepoint
(`store.atomic`), so one bad conversation does not take down the batch.

### Changing the schema

Never edit a migration that has already been applied — add a new one:

```powershell
alembic revision -m "add reviewer_note to verdicts"   # creates ava_eval/db/migrations/versions/...
# write upgrade() and downgrade() as op.execute("...") SQL, one statement per call
python -m ava_eval migrate                             # apply it
alembic downgrade -1                                   # undo it, if needed
```

Migrations are hand-written SQL (there are no SQLAlchemy models), so
`--autogenerate` does not apply. To change a view, use `CREATE OR REPLACE VIEW`
in the new migration.

### Docker gotchas

- Inside compose, the database host is **`db`**, not `localhost` —
  `docker-compose.yml` sets `DATABASE_URL` for the containers accordingly.
- The `api` container runs `migrate` before starting uvicorn, so
  `docker compose up` always brings the schema up to date. `docker compose run
  --rm api <command>` replaces that startup command and does **not** migrate: on
  a fresh volume, run `docker compose up` once first (or
  `docker compose run --rm api python -m ava_eval migrate`). This setup assumes a
  single API container — with several, move migration to its own deploy step.
- `docker/postgres/init.sql` (creates `ava_eval_test`, enables pgvector) runs
  **only when the data volume is first created**. After editing it:
  `docker compose down -v` (this deletes all data).
- The database is published on host port **5433**, not the usual 5432. A
  PostgreSQL installed directly on your machine typically owns 5432, and
  connections would silently reach it instead of the container — the symptom is
  `password authentication failed for user "ava"`. To use another port, set
  `AVA_DB_PORT` in `.env` and update `DATABASE_URL` / `AVA_TEST_DATABASE_URL` to
  match. Inside compose, containers always use `db:5432`.

## Layout

```
ava_eval/
  config.py        env-driven settings, the only place os.getenv is called
  models.py        the contracts between steps (and the Gemini output schemas)
  pipeline.py      orchestrates steps 3-6
  cli.py           command line entry points
  db/              store.py -- the ONLY module that speaks SQL
    migrate.py     runs Alembic from Python (used by `migrate` and the tests)
    migrations/    Alembic env + versioned schema migrations
  llm/             client.py + prompts/ -- the ONLY module that imports google.genai
  kb/              .docx extraction and semantic chunking
  steps/           s1_ingest ... s7_aggregate
  api/main.py      read-only FastAPI for the frontend
docker/postgres/   first-boot database init script
Dockerfile         one image: API, migrations and CLI
docker-compose.yml db + api (api migrates on startup)
```

## Tests

```powershell
docker compose up -d db
python -m pytest
```

Database tests run against a real PostgreSQL database, `ava_eval_test`, inside
the compose container — never the `ava_eval` database holding your pipeline
results. The suite migrates it once per run and empties every table before each
test, and refuses to start if the configured test database's name does not end
in `_test`.

Runs fully offline — the test suite forces stub mode regardless of your `.env`,
so it can never spend quota or fail for a missing key.

Coverage focuses on the places where a bug produces plausible-looking output
rather than a crash: transcript validation, table extraction from `.docx`,
chunk overlap and section boundaries, retrieval ordering (including a check that
pgvector search matches a full brute-force comparison), the severity rollup,
database constraints, and the `NOT_FOUND`-only retry policy.

## Not built yet (Phase 2)

A golden dataset for checking correctness against reality rather than against
the KB. The intended path is to use this phase's `kb_gap` and `CONTRADICTED`
claims, grouped by `subject_tag`, to decide which questions get human-written
answers first — highest-frequency, highest-error topics rather than the whole
KB blind.
