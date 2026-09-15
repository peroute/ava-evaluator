# Step 1 — Ingest transcripts

[← Overview](../overview.md) · [Walkthrough](../walkthrough.md) · [Step 2 →](s2-index-kb.md)

**Command:** `python -m ava_eval ingest [--input DIR]` · **Code:** [`ava_eval/steps/s1_ingest.py`](../../ava_eval/steps/s1_ingest.py)

## What it does

Reads the exported call transcripts (JSON files), checks each conversation is well-formed, and saves it
as a list of question/answer pairs (turns). A broken conversation is saved as *rejected*, along with the reason,
and the rest of the batch keeps going.

This is the only place in the project that knows what a transcript file looks like. Every later step works with
plain `Turn` records, so a new source only needs a new parser here, and nothing downstream changes. The live
ElevenLabs API is the likely next source.

## Inputs → outputs

| | |
|---|---|
| **Reads** | `*.json` files, each holding one conversation or a JSON array of them. Default: `sample_data/transcripts/` |
| **Produces** | [`Conversation`](../../ava_eval/models.py), [`Turn`](../../ava_eval/models.py), [`RejectedConversation`](../../ava_eval/models.py) |
| **Writes** | `conversations` (every conversation, `status` = `accepted` or `rejected`, `note` = the reason), `turns` (accepted only) |

Expected file shape:

```json
{
  "provider_session_id": "conv_demo_001",
  "raw_pairs": [
    { "pair_index": 0, "caller_asked": "...", "agent_said": "..." }
  ]
}
```

## How the code works

1. **[`ingest_directory`](../../ava_eval/steps/s1_ingest.py)** is the entry point for files. It lists the `.json`
   files, opens one database connection, and loops over them.
2. **[`_load_payloads`](../../ava_eval/steps/s1_ingest.py)** reads a file and always returns a list, so one
   conversation or many look the same. A file that isn't valid JSON goes into `unreadable_files` and gets skipped.
3. Each payload goes to **[`_ingest_one`](../../ava_eval/steps/s1_ingest.py)**, which validates it and writes the
   result either way.
4. **[`parse_export_v1`](../../ava_eval/steps/s1_ingest.py)** is the adapter for the current export format.
   It checks, in order:
   - the payload is an object with a non-empty `provider_session_id`
   - `raw_pairs` exists, is a list, and isn't empty
   - every pair has an integer `pair_index` (booleans are refused, since `True` is an `int` in Python), no duplicates,
     and non-empty `caller_asked` / `agent_said` (via [`_require_text`](../../ava_eval/steps/s1_ingest.py))
   - the indexes run 0, 1, 2 … with no gaps (the "not sequential from 0" check at the end of `parse_export_v1`)

   Any failure raises `TranscriptFormatError` with a specific message, e.g. `conv_demo_003[1]: 'agent_said' is empty`.
5. On failure, [`_fallback_session_id`](../../ava_eval/steps/s1_ingest.py) finds an id to save the rejection
   under. It uses the payload's own id if it has one, otherwise `filename#position`.
6. On success, [`store.upsert_conversation`](../../ava_eval/db/store.py) and
   [`store.replace_turns`](../../ava_eval/db/store.py) write the rows. "Replace" means delete-then-insert, so
   ingesting the same file twice leaves one copy, not two.

**[`ingest_payload`](../../ava_eval/steps/s1_ingest.py)** is the second entry point: one conversation already in
memory, run through the same `_ingest_one`. It's what `POST /evaluations` uses ([Frontend](../frontend.md#submitting-a-transcript)).
Submissions are saved with `source_file = "api-submission.json"`, and a rejected one is stored as a rejected row just
like a bad file, so it still shows up in reports.

The CLI wrapper [`cmd_ingest`](../../ava_eval/cli.py) prints the report. It exits with code 1 when *nothing*
was accepted.

## Why it's built this way

- **Rejected conversations are kept as rows, not dropped.** Otherwise a broken export would just vanish from
  the totals and nobody would know it existed. The report shows `REJECTED: <reason>`.
- **One bad conversation doesn't stop the batch.** Losing four good transcripts because the fifth is broken is
  a bad trade.
- **Gaps and duplicates in `pair_index` are rejected, not patched up.** Step 4 reads the conversation as a story
  and works out what each answer refers to from the turns before it. An out-of-order or gappy transcript would
  quietly corrupt that reasoning.
- **An adapter function per format.** A new source (the ElevenLabs API) means adding `parse_elevenlabs_api` next
  to `parse_export_v1`, not editing five steps.

## Gotchas

- Re-ingesting a conversation **replaces its turns**. It does *not* clear old classifications or claims for that
  session. Those get overwritten the next time `evaluate` runs.
- `ingest` doesn't evaluate anything. A newly ingested conversation shows as `UNCLASSIFIED` in reports until you run
  `evaluate`.

## Tests

[`tests/test_ingest.py`](../../tests/test_ingest.py): valid parsing, sorting out-of-order pairs, every rejection
rule (empty text, duplicates, gaps, non-integer and boolean indexes), a bad conversation not stopping the
batch, JSON arrays, re-ingest replacing turns, and for `ingest_payload`: one in-memory payload accepted, a rejection
saved with no turns, and re-submitting replacing turns.

## Connects to

- **Next:** `evaluate` reads accepted sessions via [`store.list_sessions`](../../ava_eval/db/store.py) and
  their turns via [`store.get_turns`](../../ava_eval/db/store.py), inside [`pipeline.evaluate`](../../ava_eval/pipeline.py).
- **Also called by:** [`evaluation_service`](../../ava_eval/evaluation_service.py) for `POST /evaluations`
  ([Frontend](../frontend.md)).
- **Independent of:** [Step 2](s2-index-kb.md), which runs whenever the knowledge base changes.
