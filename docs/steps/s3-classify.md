# Step 3 — Classify turns

[← Step 2](s2-index-kb.md) · [Overview](../overview.md) · [Step 4 →](s4-decompose.md)

**Runs inside:** `python -m ava_eval evaluate` · **Code:** [`ava_eval/steps/s3_classify.py`](../../ava_eval/steps/s3_classify.py) · **Prompt:** [`classify_turn.md`](../../ava_eval/llm/prompts/classify_turn.md)

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
| **LLM returns** | [`TurnClassification`](../../ava_eval/models.py#L124): `contains_claims: bool`, `reason: str` |
| **Produces** | [`ClassifiedTurn`](../../ava_eval/models.py#L136) (same fields + `session_id`, `pair_index`) |
| **Writes** | `turn_classifications`, via [`store.save_classifications`](../../ava_eval/db/store.py#L406) (upsert, so re-running overwrites) |

## How the code works

1. [`pipeline.evaluate`](../../ava_eval/pipeline.py#L102) calls [`classify_turns(turns)`](../../ava_eval/steps/s3_classify.py#L115).
2. `classify_turns` loops over the turns **one at a time, in order**, calling [`classify_turn`](../../ava_eval/steps/s3_classify.py#L108).
3. `classify_turn` is a switch: `settings.stub_llm` → `_classify_stub`, otherwise `_classify_live`. Steps 4 and 6
   use the same pattern.
4. The pipeline saves the results and builds a `pair_index → ClassifiedTurn` dict (`by_index`) for step 4.

The live version (still to be written) is about four lines:

```python
prompt = load_prompt("classify_turn", caller_asked=turn.caller_asked, agent_said=turn.agent_said)
return generate_structured(prompt, TurnClassification)
```

[`load_prompt`](../../ava_eval/llm/__init__.py#L10) fills the `{{CALLER_ASKED}}` / `{{AGENT_SAID}}` placeholders and
raises if one is missing or left unfilled. [`generate_structured`](../../ava_eval/llm/client.py#L191) makes Gemini
answer in exactly the `TurnClassification` JSON shape (structured output), at temperature 0 with retries, and
validates the reply before returning it.

## Stub vs live

| `AVA_STUB_LLM=1` (default) | `AVA_STUB_LLM=0` |
|---|---|
| [`_classify_stub`](../../ava_eval/steps/s3_classify.py#L46): splits into sentences, drops ones that match filler patterns ("hi", "thanks", "let me…"), and says `true` if what's left contains fact-like words (GPA, credit, deadline, a digit…). | [`_classify_live`](../../ava_eval/steps/s3_classify.py#L77) — **not implemented yet**, raises `NotImplementedError`. |

The stub is deliberately the approach the design *rejected*. "Feel free to call back any time" and "You can defer
enrollment for one term" have the same shape, and only one is a fact. No keyword list reliably separates them.
The stub only needs to produce varied, repeatable output so the rest of the pipeline can run offline.

## Why it's built this way

- **Deliberately over-inclusive.** A reply that states a fact *and* asks a follow-up question is `true`. When unsure,
  `true`. Step 4 can drop a false positive (it returns an empty claim list), but it never even sees a turn this step rejected.
- **No conversation history is sent.** Whether a sentence is a greeting doesn't depend on earlier turns, so history
  would cost tokens and buy nothing. Step 4 is the step that needs history.
- **Use the cheapest model you trust here.** It runs once per turn, which makes it the highest-volume call in the pipeline.

## Gotchas

- A turn the classifier wrongly marks `false` can never be flagged, because nothing downstream looks at it again. In reports it
  shows as `NOT_EVALUATED`. That's the risk being over-inclusive protects against.
- In the stub, a sentence starting with a filler word is dropped even if it goes on to state a fact
  ("Sure, the GPA is 2.0" doesn't match, but "Let me check: the GPA is 2.0" does, and gets dropped).

## Tests

No dedicated test file yet. The rollup tests in [`tests/test_retrieve_and_rollup.py`](../../tests/test_retrieve_and_rollup.py)
check how classifications surface (`NOT_EVALUATED` / `NO_CLAIMS` turns are labelled, not dropped), and
[`test_every_prompt_template_is_loadable`](../../tests/test_judge_policy.py#L223) covers the prompt.

## Connects to

- **Previous:** [Step 1](s1-ingest.md) produced the turns.
- **Next:** [Step 4](s4-decompose.md) receives `by_index` and prints `contains_claims: true/false` for every turn in its prompt.
- **Step 7:** `v_turn_results` uses this table to tell `NOT_EVALUATED` (filtered out here) apart from `UNCLASSIFIED` (never evaluated).
