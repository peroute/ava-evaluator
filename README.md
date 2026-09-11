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

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env         # works offline as-is; add a key when going live
python scripts\make_sample_kb.py

python -m ava_eval init-db
python -m ava_eval ingest
python -m ava_eval index-kb
python -m ava_eval evaluate
python -m ava_eval report
```

That runs the whole pipeline with **no API key and no network calls**, on the
sample data in `sample_data/`. See "Offline mode" below for what is real and
what is faked.

Then, for the frontend:

```powershell
python -m ava_eval serve       # http://127.0.0.1:8000  (interactive docs at /docs)
```

## Commands

| Command | Step | When you run it |
|---|---|---|
| `init-db` | — | Once, or after changing the schema |
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

## Storage

Everything lives in one SQLite file (`data/ava_eval.db` by default), including
the vector index — embeddings are stored as `float32` blobs and searched with a
numpy dot product. At knowledge-base scale that is sub-millisecond and costs
zero extra infrastructure.

The schema is written to stay portable to PostgreSQL: explicit text ids,
Python-generated timestamps, `ON CONFLICT` upserts, JSON as text, no SQLite-only
functions. All SQL lives in `ava_eval/db/`, so the migration is one module plus
swapping the similarity search for a `pgvector` `ORDER BY`.

## Layout

```
ava_eval/
  config.py        env-driven settings, the only place os.getenv is called
  models.py        the contracts between steps (and the Gemini output schemas)
  pipeline.py      orchestrates steps 3-6
  cli.py           command line entry points
  db/              schema.sql + store.py -- the ONLY module that speaks SQL
  llm/             client.py + prompts/ -- the ONLY module that imports google.genai
  kb/              .docx extraction and semantic chunking
  steps/           s1_ingest ... s7_aggregate
  api/main.py      read-only FastAPI for the frontend
```

## Tests

```powershell
python -m pytest
```

Runs fully offline — the test suite forces stub mode regardless of your `.env`,
so it can never spend quota or fail for a missing key.

Coverage focuses on the places where a bug produces plausible-looking output
rather than a crash: transcript validation, table extraction from `.docx`,
chunk overlap and section boundaries, retrieval ordering, the severity rollup,
and the `NOT_FOUND`-only retry policy.

## Not built yet (Phase 2)

A golden dataset for checking correctness against reality rather than against
the KB. The intended path is to use this phase's `kb_gap` and `CONTRADICTED`
claims, grouped by `subject_tag`, to decide which questions get human-written
answers first — highest-frequency, highest-error topics rather than the whole
KB blind.
