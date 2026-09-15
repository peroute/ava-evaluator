# Step 4 — Decompose into self-contained claims

[← Step 3](s3-classify.md) · [Overview](../overview.md) · [Step 5 →](s5-retrieve.md)

**Runs inside:** `python -m ava_eval evaluate` · **Code:** [`ava_eval/steps/s4_decompose.py`](../../ava_eval/steps/s4_decompose.py) · **Prompt:** [`decompose_claims.md`](../../ava_eval/llm/prompts/decompose_claims.md)

## What it does

Breaks each flagged reply into single facts (claims), and **rewrites each one so it makes sense on its own**.

"You need a 2.5 GPA" can't be checked (2.5 for *which program*?). This step reads the whole conversation, works out
that the caller was asking about the Online BBA two turns earlier, and turns it into "For the Online BBA in General
Business, the minimum GPA is 2.5". It's the hardest step, and everything after it depends on it.

## Inputs → outputs

| | |
|---|---|
| **Reads** | all `Turn`s for one conversation + the step 3 `by_index` dict |
| **LLM returns** | [`ConversationClaims`](../../ava_eval/models.py), a list of [`TurnClaims`](../../ava_eval/models.py) (`pair_index`, `subject_tag`, `claims`), each claim an [`ExtractedClaim`](../../ava_eval/models.py) (`text`, `scope_derived_from_pair_index`) |
| **Produces** | flat [`Claim`](../../ava_eval/models.py) list with ids |
| **Writes** | `claims`, via [`store.replace_claims_for_session`](../../ava_eval/db/store.py) |

## How the code works

1. [`pipeline.evaluate`](../../ava_eval/pipeline.py) calls [`decompose_conversation`](../../ava_eval/steps/s4_decompose.py), which switches between stub and live.
2. **Live** ([`_decompose_live`](../../ava_eval/steps/s4_decompose.py)): if no turn was flagged, return no claims without
   calling the model. Otherwise render the transcript, fill the prompt, call `generate_structured(prompt, ConversationClaims)`.
   [`render_transcript`](../../ava_eval/steps/s4_decompose.py) prints **every** turn, including unflagged ones, in the
   exact format the prompt's worked example uses:
   ```
   [pair 1] caller: The general business one.
              agent:  Great choice. The Online Bachelor of Business Administration ...
              contains_claims: true
   ```
3. [`to_claims`](../../ava_eval/steps/s4_decompose.py) flattens the response into `Claim` rows:
   - drops any turn whose `pair_index` isn't in this conversation (the model can invent one)
   - drops empty claim text
   - filters `scope_derived_from` to indexes that actually exist
   - builds a **positional id**: `{session}__p{pair}__c{n}`, e.g. `conv_demo_001__p2__c0`. The `n` counter is kept per
     `pair_index`, so if the model returns the same turn twice the ids keep counting instead of colliding
4. `replace_claims_for_session` deletes the session's old claims and inserts the new ones. The database's
   `ON DELETE CASCADE` also deletes their old retrievals and verdicts.

## Stub vs live

| `AVA_STUB_LLM=1` (default) | `AVA_STUB_LLM=0` |
|---|---|
| [`_decompose_stub`](../../ava_eval/steps/s4_decompose.py): one claim per sentence of 4+ words that isn't a question. **No scope resolution:** the text is copied as-is, and `subject_tag` is always `[stub] unresolved subject`. | [`_decompose_live`](../../ava_eval/steps/s4_decompose.py): one Gemini call per conversation with the whole rendered transcript. The model resolves each claim's scope and returns a `subject_tag` per turn and `scope_derived_from_pair_index` per claim. Skips the call when nothing was flagged. |

What the stub skips (resolving scope) is the whole reason this step needs a language model.

## Why it's built this way

- **Self-contained claims.** Step 5 searches using *only* the claim text. This deliberately differs from the usual
  RAGAS-style default ("atomic, context-stripped claims"), which fails on facts that only hold for one program or policy.
- **One call per conversation, not per turn.** Sending a growing history with each turn re-sends turn 1 in every
  later call, so cost grows roughly with the square of the conversation's length. One call also gives one consistent
  reading of the conversation, instead of several separate scoping guesses that can disagree.
- **Four protections against the topic carrying over by mistake** are built into the prompt: work out the subject again
  for every turn, read `caller_asked` (callers change topic), output a `subject_tag` per turn, and record
  `scope_derived_from_pair_index` so a scoping mistake can be traced afterwards.
- **Positional, not random, ids.** Re-running on an unchanged conversation gives the same ids, so results can be
  compared between runs.

## Gotchas

- **Re-running step 4 wipes that session's verdicts** (by cascade). That's intended: if a claim's wording changed,
  it's a different claim, and keeping the old verdict attached would be wrong.
- Keep `render_transcript` and the worked example in the prompt **in sync**. The example is how the model learns to
  read the format.
- A flagged turn that comes back with `claims: []` is normal (step 3 over-includes on purpose). It shows as
  `NO_CLAIMS` in step 7.
- Conversations are processed **one after another** (the loop in [`pipeline.evaluate`](../../ava_eval/pipeline.py)), so an error
  always points to one specific conversation.

## Tests

[`tests/test_decompose.py`](../../tests/test_decompose.py): the live path sends the rendered transcript (fake model),
no model call when nothing was flagged, `to_claims` drops a `pair_index` that isn't in the conversation, and ids stay
unique when the model repeats a turn. [`test_replacing_claims_cascades_to_verdicts`](../../tests/test_retrieve_and_rollup.py)
checks the cascade, and [`test_classify.py`](../../tests/test_classify.py) checks a `false` turn yields no claims.
The stub's sentence splitting has no dedicated test yet.

## Connects to

- **Previous:** [Step 3](s3-classify.md) decides which turns get claims.
- **Next:** [Step 5](s5-retrieve.md) embeds each `Claim.text` as a search query. If there are no claims, the
  pipeline skips 5–6 for that conversation (the `if not claims` check in [`pipeline.evaluate`](../../ava_eval/pipeline.py)).
- **Step 7 / API:** `subject_tag` is a filter on `GET /claims?subject=...`, and the planned Phase 2 groups results by it.
