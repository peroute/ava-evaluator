# Operations

[← Overview](overview.md)

How to run, configure, test, and change this project, plus the Docker and PostgreSQL traps that have already
caught someone. Quick start is in the [README](../README.md).

## Contents

- [Configuration](#configuration)
- [Docker](#docker)
- [Database](#database)
- [Changing the schema](#changing-the-schema)
- [Tests](#tests)
- [Going live](#going-live)

---

## Configuration

Every setting is read once in [`ava_eval/config.py`](../ava_eval/config.py) from environment variables (and `.env`).
Copy [`.env.example`](../.env.example) to `.env` to start.

| Variable | Default | Used by | Notes |
|---|---|---|---|
| `GEMINI_API_KEY` | — | llm | Only needed when a stub flag is `0` |
| `AVA_STUB_LLM` | `1` | steps 3, 4, 6 | `1/true/yes/on` = offline |
| `AVA_STUB_EMBED` | `1` | steps 2, 5 | `1` = hashed bag-of-words embedder |
| `AVA_JUDGE_MODEL` | `gemini-3.8-flash` | steps 3, 4, 6 | |
| `AVA_EMBED_MODEL` | `gemini-embedding-2` | steps 2, 5 | Changing it requires `index-kb --force` |
| `AVA_EMBED_DIM` | `768` | steps 2, 5 | Changing it requires `index-kb --force` |
| `DATABASE_URL` | `postgresql://ava:ava@127.0.0.1:5433/ava_eval` | all | Compose overrides it inside containers |
| `AVA_TEST_DATABASE_URL` | `…/ava_eval_test` | pytest | Name **must** end in `_test` |
| `AVA_DISPLAY_TIMEZONE` | `America/Chicago` | db | IANA name, display only |
| `AVA_TOP_K` | `5` | step 5 | Chunks per claim |
| `AVA_RETRY_K_MULTIPLIER` | `3` | step 6 | Retry searches `TOP_K × this` |
| `AVA_MAX_WORKERS` | `4` | steps 5, 6 | Thread pool size for embed/judge calls |
| `AVA_KB_FILE` | `sample_data/kb/sample_online.docx` | step 2 | The knowledge base `.docx`. Set to `realdata/online.docx` for real runs |
| `AVA_CHUNK_TARGET_CHARS` | `1200` | step 2 | Soft ceiling on a chunk's body; reference sections are packed up to it |
| `AVA_CHUNK_OVERLAP_RATIO` | `0.15` | step 2 | Share of the target carried between the parts of one oversized section (never across sections) |
| `POSTGRES_USER` / `_PASSWORD` / `_DB` | `ava` / `ava` / `ava_eval` | compose | Used when the volume is first created |
| `AVA_DB_PORT` | `5433` | compose | Host port for the database |

A malformed number (e.g. `AVA_TOP_K=five`) silently falls back to the default.

---

## Docker

[`docker-compose.yml`](../docker-compose.yml) runs three services from two images: `api` from the Python
[`Dockerfile`](../Dockerfile), `frontend` from [`frontend/Dockerfile`](../frontend/Dockerfile) (nginx), and the stock
pgvector image for `db`:

```mermaid
flowchart LR
    subgraph host["Your machine (127.0.0.1 only)"]
        CLI["python -m ava_eval / pytest"]
        B["Browser"]
    end
    subgraph compose["docker compose network"]
        FE["frontend<br/>nginx :80"]
        API["api<br/>migrate && uvicorn :8000"]
        DB[("db<br/>pgvector/pgvector:pg17<br/>:5432")]
    end
    CLI -->|127.0.0.1:5433| DB
    B -->|127.0.0.1:8080| FE
    FE -->|"/api/* → api:8000"| API
    B -.->|127.0.0.1:8000 /docs| API
    API -->|db:5432| DB
```

| Command | Does |
|---|---|
| `docker compose up --build` | DB + migrations + API on http://127.0.0.1:8000 + [frontend](frontend.md) on http://127.0.0.1:8080 |
| `docker compose up -d db` | just the database (for local Python and pytest) |
| `docker compose run --rm api python -m ava_eval <cmd>` | run a pipeline command inside the image |
| `docker compose down` | stop. Data survives in the `pgdata` volume |
| `docker compose down -v` | stop **and delete all data** |

### Gotchas

- **Use `127.0.0.1`, not `localhost`.** All three ports (5433 database, 8000 API, 8080 frontend) are published on
  IPv4 loopback only. `localhost` tries IPv6 (`::1`)
  first, where nothing is listening, and every connection stalls until that attempt times out. Binding to 127.0.0.1
  also means nothing else on your network can reach the database with its default password.
- **Inside compose, the database host is `db`**, not `localhost` (inside a container, `localhost` means that same container).
  Compose sets `DATABASE_URL` for the containers accordingly.
- **Host port 5433, not 5432.** A PostgreSQL installed directly on your machine usually owns 5432, and connections would
  silently reach it instead. The symptom is `password authentication failed for user "ava"`. To change the port, set
  `AVA_DB_PORT` and update both database URLs.
- **`docker compose run` doesn't migrate.** The `api` service's command is `migrate && uvicorn`, and `run` replaces it. On a
  fresh volume, run `docker compose up` once first (or `docker compose run --rm api python -m ava_eval migrate`).
- **One API container only.** Several replicas would all migrate at startup and race each other. Move migration to its own
  deploy step before scaling.
- **`docker/postgres/init.sql` runs only when the volume is first created.** It enables pgvector and creates
  `ava_eval_test`. After editing it: `docker compose down -v` (this deletes data).
- The API binds `0.0.0.0` *inside* the container. That's required, because binding to the container's own localhost
  would make it unreachable from outside. The host-side `127.0.0.1:` mapping is what keeps it private.
- `sample_data/` is mounted into the container, so `make_sample_kb.py`, `ingest` and `index-kb` see your local files
  without a rebuild.

---

## Database

All SQL is in [`ava_eval/db/store.py`](../ava_eval/db/store.py). The schema is in Alembic migrations.

- **Native types.** Timestamps are `TIMESTAMPTZ`, document versions `DATE`, `contains_claims` `BOOLEAN`,
  `scope_derived_from` `JSONB`, embeddings pgvector `vector` (any dimension, checked against `embedding_dim`).
- **Constraints mirror `models.py`.** A mistyped verdict or tag is rejected on insert instead of showing up in a report.
- **Similarity search is exact.** Embeddings are stored unit length, search orders by `<#>`, and there's deliberately no
  HNSW/IVFFlat index. The reasoning is in [Step 5](steps/s5-retrieve.md#why-its-built-this-way).
- **One failed statement aborts the whole transaction.** After an error, PostgreSQL refuses every later statement until a
  rollback. [`store.atomic`](../ava_eval/db/store.py) wraps a block in a savepoint so only that block is undone.
  `evaluate` uses one savepoint per conversation.
- **`store.connect`** commits on success, rolls back on error, and always closes. Rows come back as plain dicts.
- **Parameters, never string formatting.** psycopg placeholders are `%s` / `%(name)s`, so a literal `%` in SQL (e.g. `LIKE`)
  must be written `%%`.

### Time zone

Timestamps are `TIMESTAMPTZ`: an absolute instant, written in UTC by `store.utcnow()`. They're **displayed** in Lubbock
time (`America/Chicago`: -05:00 in summer, -06:00 in winter).

- App connections set the zone from `AVA_DISPLAY_TIMEZONE` in `store.connect()`.
- Migration `0002` makes `America/Chicago` the database default, so pgAdmin and psql match. It only applies to **new**
  connections, so reconnect after migrating.
- An unknown zone name fails with a clear message rather than a driver error.

### Inspecting data

```powershell
docker compose exec db psql -U ava -d ava_eval
```
```sql
SELECT * FROM v_conversation_summary;
SELECT claim_id, verdict, tag, claim_text FROM v_claim_results WHERE verdict <> 'SUPPORTED';
```

---

## Changing the schema

**Never edit a migration that has already been applied.** Add a new one:

```powershell
alembic revision -m "add reviewer_note to verdicts"   # creates ava_eval/db/migrations/versions/...
# write upgrade() and downgrade() as op.execute("...") SQL, one statement per call
python -m ava_eval migrate                             # apply it
alembic downgrade -1                                   # undo it, if needed
```

- Migrations are hand-written SQL, and there are no SQLAlchemy models, so `--autogenerate` doesn't apply.
- One statement per `op.execute` call. Several statements in one call aren't reliable through psycopg.
- Change a view with `CREATE OR REPLACE VIEW` in a new migration.
- Changing verdict severity? Update `SEVERITY` in `models.py` **and** `v_turn_severity` together.
- Migrations can't read configuration (they must do the same thing every run), which is why `0002` hard-codes the zone.
- `python -m ava_eval migrate` is safe to run repeatedly. It goes through Alembic's Python API
  ([`db/migrate.py`](../ava_eval/db/migrate.py)), so it works from any directory and inside the container.

---

## Tests

```powershell
docker compose up -d db
python -m pytest
```

- Database tests run against a **real PostgreSQL database**, `ava_eval_test`, never the `ava_eval` database holding
  your results. The SQL is the thing under test (views, constraints, pgvector), so a fake database would only test the fake.
- The suite migrates the test database once per run and **empties every table before each test**. It refuses to start
  if the test database's name doesn't end in `_test`.
- **Always offline.** [`conftest.py`](../tests/conftest.py) forces both stub flags on and clears the API key, whatever
  your `.env` says, so tests never spend quota or fail for a missing key.
- Coverage focuses on places where a bug produces *plausible-looking* output rather than a crash:

| File | Covers |
|---|---|
| [`test_ingest.py`](../tests/test_ingest.py) | transcript validation, batch resilience, re-ingest, one in-memory payload (`ingest_payload`) |
| [`test_classify.py`](../tests/test_classify.py) | stub answers, the live batch call, per-turn fallback, ignoring unknown/duplicate indices, classifications stored as booleans |
| [`test_decompose.py`](../tests/test_decompose.py) | live prompt carries the rendered transcript, no call when nothing is flagged, `to_claims` id and index rules |
| [`test_kb.py`](../tests/test_kb.py) | bold/list/table extraction, topic markers, scaffold and TOC handling, label ownership and parents, catalog groups, chunk packing, ids, content survival, hashing |
| [`test_kb_realdata.py`](../tests/test_kb_realdata.py) | the same properties on `realdata/online.docx` (skipped when the file is absent): topic count, no cross-topic chunks, pasted program pages, nothing lost |
| [`test_kb_versioning.py`](../tests/test_kb_versioning.py) | chunk kind column, KB replacement, verdicts recording the KB version, the stale-verdict flag and report warning, migration 0003 round trip |
| [`test_retrieve_and_rollup.py`](../tests/test_retrieve_and_rollup.py) | pgvector search vs brute force, normalisation, severity rollup, cascades, constraints, `UNJUDGED` turns and counts (migration 0004) |
| [`test_judge_policy.py`](../tests/test_judge_policy.py) | the `NOT_FOUND`-only retry and tagging, the live judge (fake model): evidence-id check, a failed claim not losing the others, failed-retry fallback; prompt loading, dimension-mismatch errors |
| [`test_cli_evaluate.py`](../tests/test_cli_evaluate.py) | `evaluate` exits 1 when any claim is unjudged or a conversation errored |
| [`test_timezone.py`](../tests/test_timezone.py) | display zone for app and other clients, stored instants unchanged |
| [`test_migration_0004.py`](../tests/test_migration_0004.py) | migration 0004 downgrades and upgrades cleanly |
| [`test_api.py`](../tests/test_api.py) | `POST /evaluations` status codes, GET routes unchanged, unjudged fields the frontend reads |
| [`test_evaluation_service.py`](../tests/test_evaluation_service.py) | submit returns frontend-ready results; rejection, pipeline error and empty KB become the right errors |
| [`test_frontend.py`](../tests/test_frontend.py) | frontend files and Dockerfile contents, nginx `/api/` proxy, compose service, page controls, no unsafe DOM, no npm dependencies |

The frontend's display logic has its own tests, run with Node (18+, nothing to install). Details in
[Frontend](frontend.md#tests):

```powershell
cd frontend
node --test
```

---

## Going live

1. Put `GEMINI_API_KEY` in `.env`.
2. `AVA_KB_FILE=realdata/online.docx` and `AVA_STUB_EMBED=0`, then `python -m ava_eval index-kb --force` (the stored
   vectors must be rebuilt with the real model). Indexing makes one embedding call per chunk, about 320 for the real document.
3. `python -m ava_eval migrate` (applies 0004, which the report needs for `UNJUDGED` counts).
4. `AVA_STUB_LLM=0` turns steps 3, 4 and 6 live together. Try one conversation first:
   `python -m ava_eval evaluate --session <id>`, then `report --session <id>`.
5. If `evaluate` prints `x <claim_id>: ...` lines and exits 1, those claims could not be judged (quota, bad JSON, or the
   judge cited a chunk it was not shown). Every other verdict in the conversation was still saved; the claims show as
   `UNJUDGED` in the report. Re-run `evaluate` to retry them. Note a re-run redoes steps 3–6 for that conversation.
6. Check `GET /health` reports `stub_llm: false, stub_embed: false` before showing results to anyone.
7. Before exposing the API beyond your machine: restrict CORS `allow_origins` in [`api/main.py`](../ava_eval/api/main.py).
8. **`POST /evaluations` has no authentication, and live, every call spends Gemini quota.** Anyone who can reach
   `:8000` or the frontend on `:8080` can run evaluations. Keep both on `127.0.0.1` (as compose does) until there's
   auth in front of them. A slow evaluation through the frontend can hit nginx's 300s timeout (`504`); the pipeline
   still finishes and saves. See [Frontend](frontend.md#gotchas).
