# Step 3 — Classify turns

[← Step 2](s2-index-kb.md) · [Overview](../overview.md) · [Step 4 →](s4-decompose.md)

**Runs inside:** `python -m ava_eval evaluate` · **Code:** [`ava_eval/steps/s3_classify.py`](../../ava_eval/steps/s3_classify.py) · **Prompts:** [`classify_turns_batch.md`](../../ava_eval/llm/prompts/classify_turns_batch.md) (live), [`classify_turn.md`](../../ava_eval/llm/prompts/classify_turn.md) (per-turn fallback)

## What it does

A cheap yes/no filter for each turn: *does Ava's reply state anything that could be checked against TTU's documents?*
Greetings, "let me connect you", and questions back to the caller get `false`, and nothing further happens
to them.

It exists so the expensive steps (4–6) don't waste calls on filler, and so filler never shows up as a
meaningless `NOT_FOUND` that inflates the gap numbers.

## Inputs → outputs

| | |
|---|---|
| **Reads** | a conversation's `Turn`s (from step 1) |
| **LLM returns** | [`TurnClassification`](../../ava_eval/models.py): `contains_claims: bool`, `reason: str` |
| **Produces** | [`ClassifiedTurn`](../../ava_eval/models.py) (same fields + `session_id`, `pair_index`) |
| **Writes** | `turn_classifications`, via [`store.save_classifications`](../../ava_eval/db/store.py) (upsert, so re-running overwrites) |

## How the code works

1. [`pipeline.evaluate`](../../ava_eval/pipeline.py) calls [`classify_turns(turns)`](../../ava_eval/steps/s3_classify.py).
2. **Stub** (`settings.stub_llm`): loops over the turns in order, calling `classify_turn`, which returns
   `_classify_stub` for each.
3. **Live**: [`_classify_batch_live`](../../ava_eval/steps/s3_classify.py) sends **every turn in one call**.
   `_render_turns` prints each as a `[pair N]` block with its `caller:` and `agent:` lines:

   ```python
   prompt = load_prompt("classify_turns_batch", turns=_render_turns(turns))
   response = generate_structured(prompt, BatchClassification)
   ```

   The reply is a list of `{pair_index, contains_claims, reason}`. Answers for a `pair_index` that wasn't sent are
   dropped, and if an index appears twice, the first answer wins.
4. **Fallback:** any turn the batch reply left out is classified on its own with `_classify_live` (the
   `classify_turn` prompt, one call for that turn only).
5. Results come back in turn order, whatever order the model answered in. The pipeline saves them and builds a
   `pair_index → ClassifiedTurn` dict (`by_index`) for step 4.

[`load_prompt`](../../ava_eval/llm/__init__.py) fills the `{{TURNS}}` (or `{{CALLER_ASKED}}` / `{{AGENT_SAID}}`)
placeholders and raises if one is missing or left unfilled. [`generate_structured`](../../ava_eval/llm/client.py) makes
Gemini answer in exactly the schema's JSON shape (structured output), at temperature 0 with retries on failed calls,
and validates the reply before returning it. A reply that fails validation is not retried: it raises `LlmError`, and
the pipeline marks that conversation as errored.

## Stub vs live

| `AVA_STUB_LLM=1` (default) | `AVA_STUB_LLM=0` |
|---|---|
| `_classify_stub`: splits into sentences, drops ones that match filler patterns ("hi", "thanks", "let me…"), and says `true` if what's left contains fact-like words (GPA, credit, deadline, a digit…). | `classify_turns` sends every turn in one `classify_turns_batch` call; any turn the reply misses falls back to `_classify_live`, one call for that turn. |

The stub is deliberately the approach the design *rejected*. "Feel free to call back any time" and "You can defer
enrollment for one term" have the same shape, and only one is a fact. No keyword list reliably separates them.
The stub only needs to produce varied, repeatable output so the rest of the pipeline can run offline.

## Why it's built this way

- **Deliberately over-inclusive.** A reply that states a fact *and* asks a follow-up question is `true`. When unsure,
  `true`. Step 4 can drop a false positive (it returns an empty claim list), but it never even sees a turn this step rejected.
- **Presence, not correctness.** A wrong fact ("the minimum GPA is 9.5") or a hedged one ("I believe the deadline is
  March 1") is still `true`. Whether it is right is step 6's question; this step never looks at the KB.
- **One call per conversation.** The prompt template is sent once instead of once per turn. The model sees every turn
  but judges each reply on its own, using earlier turns only to read short replies like "yes, it does".
- **Runs on `judge_model` for now.** It's the highest-volume call in the pipeline, so it should get a cheaper model,
  but `generate_structured` has no per-step model override yet.

## Gotchas

- A turn the classifier wrongly marks `false` can never be flagged, because nothing downstream looks at it again. In reports it
  shows as `NOT_EVALUATED`. That's the risk being over-inclusive protects against.
- In the stub, a sentence starting with a filler word is dropped even if it goes on to state a fact
  ("Sure, the GPA is 2.0" doesn't match, but "Let me check: the GPA is 2.0" does, and gets dropped).

## Tests

[`tests/test_classify.py`](../../tests/test_classify.py) covers: the stub's answers (greeting / closing / clarifying
question → `false`; fact, fact + filler, wrong fact, hedged fact → `true`); the live path with a fake
`generate_structured` (all turns go in **one** batch call and results keep turn order even when answered out of order;
a turn the batch left out gets one fallback call carrying only that turn; unknown and duplicate indices are ignored);
the schema declaring `contains_claims` a boolean; step 4 extracting nothing from a `false` turn; and, through `pipeline.evaluate` on the Postgres test
database, that classifications are stored as booleans, `false` turns stay stored, only `true` turns get claims, and
re-classifying overwrites. How the real model answers those cases is not tested offline. The rollup tests in
[`tests/test_retrieve_and_rollup.py`](../../tests/test_retrieve_and_rollup.py) check how classifications surface
(`NOT_EVALUATED` / `NO_CLAIMS` turns are labelled, not dropped), and
[`test_every_prompt_template_is_loadable`](../../tests/test_judge_policy.py) covers both prompts.

## Connects to

- **Previous:** [Step 1](s1-ingest.md) produced the turns.
- **Next:** [Step 4](s4-decompose.md) receives `by_index` and prints `contains_claims: true/false` for every turn in its prompt.
- **Step 7:** `v_turn_results` uses this table to tell `NOT_EVALUATED` (filtered out here) apart from `UNCLASSIFIED` (never evaluated).
