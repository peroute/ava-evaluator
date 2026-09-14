# Walkthrough: one conversation, start to finish

[← Overview](overview.md) · [Step 1 →](steps/s1-ingest.md)

This follows one real sample transcript, [`conv_demo_001.json`](../sample_data/transcripts/conv_demo_001.json), through all
seven steps, and shows **the actual rows the pipeline wrote** at each stage. Read it once end to end to see how the pieces
connect, then go to a [step doc](overview.md#reading-order) for the code-level detail.

> **Everything below is stub-mode output** (`AVA_STUB_LLM=1`, `AVA_STUB_EMBED=1`): no API key, no network. The
> *plumbing* is real (validation, chunking, SQL search, retry policy, rollups). The *judgement* is a keyword heuristic.
> Where a live model would behave differently, a **Live:** note says so.

## Run it yourself

```powershell
docker compose up -d db
python -m ava_eval migrate
python scripts\make_sample_kb.py
python -m ava_eval ingest
python -m ava_eval index-kb
python -m ava_eval evaluate
python -m ava_eval report --session conv_demo_001
```

To look at the rows yourself:

```powershell
docker compose exec db psql -U ava -d ava_eval
```

---

## The conversation

| pair | Caller asked | Ava said |
|---|---|---|
| 0 | Hi, I'm interested in your online business degree. | Hi there, thanks for calling Texas Tech Online! Which one did you have in mind? |
| 1 | The general business one. | Great choice. The Online Bachelor of Business Administration in General Business is delivered fully asynchronously, with no scheduled live class meetings. |
| 2 | What GPA do I need to get in? | For the Online BBA in General Business, the minimum cumulative grade point average required is **2.5** from the most recently attended institution. |
| 3 | Do I need to send transcripts? | Yes. You must submit official transcripts from every institution previously attended, regardless of whether credit was earned. |
| 4 | Is there parking on campus if I ever need to come in? | Online students who visit campus can purchase a commuter parking permit for 75 dollars per semester. |

The sample KB says the Online BBA's minimum GPA is **2.0**, and says nothing about parking. So by the end we expect
one contradiction (pair 2) and one gap (pair 4).

---

## Step 1: Ingest → `conversations`, `turns`

```
Accepted 2 conversations (8 turns)
  + conv_demo_001            5 turns
  + conv_demo_002            3 turns

Rejected 1 conversations
  - conv_demo_003            conv_demo_003[1]: 'agent_said' is empty
```

`conv_demo_001` passes every check (ids 0–4 in sequence, no empty text) and becomes 5 rows in `turns`.
A different file, `conv_demo_003`, has an empty reply. It's saved as `rejected` with that exact reason, and the batch
carries on. → [Step 1 details](steps/s1-ingest.md)

## Step 2: Index the KB → `kb_documents`, `kb_chunks`

```
Indexed sample_online.docx: 11 chunks (6 question, 5 reference)
  6 topics, 3 placed under a catalog group

Listed in a catalog but no topic in the KB (1)
  ? Online Bachelor of Arts in History  (listed in: Bachelor's Degrees)
```

The sample KB is one document in the same format as TTU's real one: bold "If asked questions about X, answer with"
lines open 6 topics. Every **question** becomes its own chunk (`q`). A program page's small descriptive sections are
**packed** into one chunk (`r`). The three program pages are listed by the "Bachelor's Degrees" catalog, so they get
that group in their header. History is listed there too but has no page, which the output points out.

| chunk_id | kind | section | chars |
|---|---|---|---|
| `t01.r01` | reference | Undergraduate Admissions > Overview | 117 |
| `t01.q01` | qa | Undergraduate Admissions > What GPA do I need to be admitted? | 367 |
| `t01.q02` | qa | Undergraduate Admissions > Do programs set their own requirements? | 493 |
| `t01.q03` | qa | Undergraduate Admissions > Do I need to send official transcripts? | 304 |
| `t02.r01` | reference | Bachelor's Degrees > Overview | 278 |
| `t03.r01` | reference | Bachelor's Degrees > Online Bachelor of Business Administration in General Business > Overview; Prerequisites; Application Deadlines > Late Fall; Application Deadlines > Early Spring | 745 |
| … 5 more (`t04.r01`, `t05.r01`, `t06.q01`–`t06.q03`) | | | |

The GPA **table** was extracted, labelled, and kept inside the answer it belongs to. This is the full text of chunk
`t01.q02`:

```
Undergraduate Admissions

Do programs set their own requirements?
Yes. Where a program states a requirement different from the university minimum, the program requirement applies.
Program Requirements
Program: Online BBA in General Business | Minimum GPA: 2.0 | Transferable Credit Hours Required: 30
Program: Online BS in Computer Science | Minimum GPA: 2.75 | Transferable Credit Hours Required: 30
Program: Online BA in Psychology | Minimum GPA: 2.0 | Transferable Credit Hours Required: 24
```

A naive extractor would have returned none of those three rows. The bold "Program Requirements" line sits inside the
answer, so it stays with its question instead of starting a chunk of its own. → [Step 2 details](steps/s2-index-kb.md)

## Step 3: Classify → `turn_classifications`

| pair | contains_claims | reason |
|---|---|---|
| 0 | `false` | [stub] No fact-bearing vocabulary found. |
| 1 | `true` | [stub] Contains fact-bearing vocabulary. |
| 2 | `true` | [stub] Contains fact-bearing vocabulary. |
| 3 | `true` | [stub] Contains fact-bearing vocabulary. |
| 4 | `true` | [stub] Contains fact-bearing vocabulary. |

Pair 0 is a greeting plus a question back to the caller. It stops here and will show as `NOT_EVALUATED`.
→ [Step 3 details](steps/s3-classify.md)

## Step 4: Decompose → `claims`

| claim_id | text | subject_tag | scope_derived_from |
|---|---|---|---|
| `conv_demo_001__p1__c0` | The Online Bachelor of Business Administration in General Business is delivered fully asynchronously, with no scheduled live class meetings. | [stub] unresolved subject | `[]` |
| `conv_demo_001__p2__c0` | For the Online BBA in General Business, the minimum cumulative grade point average required is 2.5 from the most recently attended institution. | [stub] unresolved subject | `[]` |
| `conv_demo_001__p3__c0` | You must submit official transcripts from every institution previously attended, regardless of whether credit was earned. | [stub] unresolved subject | `[]` |
| `conv_demo_001__p4__c0` | Online students who visit campus can purchase a commuter parking permit for 75 dollars per semester. | [stub] unresolved subject | `[]` |

The stub keeps sentences of 4+ words, so "Great choice." and "Yes." were dropped.

**Live:** this is where the stub is most obviously not the real thing. Claim `p3__c0` says "You must submit official
transcripts" but doesn't say *for what*. A live model would rewrite it as "For the Online BBA in General Business,
official transcripts are required from…", set `subject_tag` to `Online BBA - General Business`, and set
`scope_derived_from` to `[1]`. Pair 2 happens to be self-contained already, because Ava named the program herself.
→ [Step 4 details](steps/s4-decompose.md)

## Step 5: Retrieve → `claim_retrievals`

Top 5 chunks per claim (attempt 1). Scores are cosine similarity.

| claim | rank 0 (best) | score |
|---|---|---|
| `p1__c0` asynchronous BBA | `t03.r01` (BBA program page) | 0.553 |
| `p2__c0` GPA 2.5 | `t01.q01` (What GPA do I need to be admitted?) | 0.343 |
| `p3__c0` transcripts | `t01.q03` (Do I need to send official transcripts?) | 0.496 |
| `p4__c0` parking | `t01.q02` (Do programs set their own requirements?) | **0.182** |

The first three found the right chunk. Parking's best match scores only 0.182, because nothing in the KB is about
parking. For `p2__c0`, the GPA table chunk `t01.q02` came second (0.318). → [Step 5 details](steps/s5-retrieve.md)

## Step 6: Judge → `verdicts`

| claim | verdict | tag | attempts | evidence | rationale |
|---|---|---|---|---|---|
| `p1__c0` | `SUPPORTED` | | 1 | `t03.r01` | [stub] Excerpt covers 100% of the claim's content words. |
| `p2__c0` | `CONTRADICTED` | | 1 | `t01.q01` | [stub] Claim states ['2.5'] but the excerpt states ['2.0', '4.0']. |
| `p3__c0` | `SUPPORTED` | | 1 | `t01.q03` | [stub] Excerpt covers 100% of the claim's content words. |
| `p4__c0` | `NOT_FOUND` | `kb_gap` | **2** | | [stub] Best excerpt overlapped only 25% of the claim. |

Each verdict also stores the KB's content hash at judging time (`kb_content_hash`), which is what lets step 7 warn if the
KB is re-indexed later.

What happened to `p4__c0`, following the [retry flow](steps/s6-judge.md#how-the-code-works):

1. First pass: `NOT_FOUND`.
2. Searched again with `k = 5 × 3 = 15`, saved as `attempt = 2`. The index only has 11 chunks, so all 11 came back,
   **6 of which were new**.
3. Judged again on all 11. Still `NOT_FOUND` → tagged `kb_gap`, `attempts = 2`.

If the second judgement had come back `SUPPORTED` or `CONTRADICTED`, that would have become the verdict, tagged
`retrieval_miss`: the evidence was in the KB all along, and our first search missed it.

`p2__c0` was contradicted once and is **final**. It doesn't retry, even though another chunk might "fit better".

**Live:** the stub's rule is "same topic words, no numbers in common". A live judge would read the excerpts, apply
the general-policy rule, and quote the decisive wording, e.g. *"Excerpt states 'Minimum GPA: 2.0' for the Online
BBA in General Business."* It would also see, from each excerpt's `source:` header, which program the excerpt
belongs to. → [Step 6 details](steps/s6-judge.md)

## Step 7: Roll up → views, report, API

`v_turn_results`: each turn gets its worst claim verdict:

| pair | contains_claims | claims | turn_verdict |
|---|---|---|---|
| 0 | false | 0 | `NOT_EVALUATED` |
| 1 | true | 1 | `SUPPORTED` |
| 2 | true | 1 | `CONTRADICTED` |
| 3 | true | 1 | `SUPPORTED` |
| 4 | true | 1 | `NOT_FOUND` |

`v_conversation_summary`: rejected conversations are listed too:

| session | status | turns | contradicted | not_found | supported | skipped |
|---|---|---|---|---|---|---|
| conv_demo_001 | accepted | 5 | 1 | 1 | 2 | 1 |
| conv_demo_002 | accepted | 3 | 0 | 0 | 2 | 1 |
| conv_demo_003 | rejected: `'agent_said' is empty` | 0 | 0 | 0 | 0 | 0 |

`python -m ava_eval report --session conv_demo_001`:

```
========================================================================
AVA FAITHFULNESS REPORT
========================================================================
2 conversations | 8 turns | 6 claims | 11 KB chunks indexed

Claim verdicts
------------------------------------------------------------------------
  CONTRADICTED  (Ava said something the KB refutes)       1
  NOT_FOUND     (KB cannot confirm it)                    1
  SUPPORTED     (grounded in the KB)                      4
  tags: kb_gap=1
  [#####~~~~~...................]

Conversations
------------------------------------------------------------------------
  conv_demo_001              5 turns | contradicted 1 | not_found 1 | supported 2 | skipped 1

Flagged claims (2)
------------------------------------------------------------------------
  CONTRADICTED  conv_demo_001 pair 2
    subject : [stub] unresolved subject
    claim   : For the Online BBA in General Business, the minimum cumulative grade point average required is 2.5 from the most recently attended institution.
    why     : [stub] Claim states ['2.5'] but the excerpt states ['2.0', '4.0'].
    evidence: Undergraduate Admissions > What GPA do I need to be admitted? (t01.q01)

  NOT_FOUND [kb_gap]  conv_demo_001 pair 4
    subject : [stub] unresolved subject
    claim   : Online students who visit campus can purchase a commuter parking permit for 75 dollars per semester.
    why     : [stub] Best excerpt overlapped only 25% of the claim.
```

The header counts cover **every** conversation (4 supported = 2 from each demo), while the sections below are filtered to
`conv_demo_001`. The same data is at `GET /conversations/conv_demo_001` and
`GET /claims/conv_demo_001__p4__c0` (the second shows attempts 1 and 2).

If the KB is re-indexed after a different document version, each of these flags gets an extra line,
`[!] the knowledge base changed since this verdict`, because chunk ids like `t01.q01` may now name a different passage.
Re-run `evaluate` to clear it. → [Step 7 details](steps/s7-aggregate.md)

---

## What this run showed

| Who acts | Finding |
|---|---|
| **Ava's team** | Pair 2: Ava said 2.5, the KB says 2.0 → `CONTRADICTED` |
| **TTU content team** | Pair 4: parking permits aren't in the KB → `NOT_FOUND` + `kb_gap` |
| **Evaluator tuning** | No `retrieval_miss` this run |
| **Nobody** | Pairs 1 and 3 are grounded in the KB |

This is why the verdicts are never merged into one "flagged" count: each row sends work to a different team.
