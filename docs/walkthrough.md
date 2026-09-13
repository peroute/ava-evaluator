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
Indexed 3 documents (9 chunks)
  + admissions_requirements.docx                    3 chunks
  + online_programs.docx                            3 chunks
  + tuition_and_aid.docx                            3 chunks
```

One chunk per section here, because the sample sections are short (well under 1200 characters). The ids spell out
the document and section:

| chunk_id | section | chars |
|---|---|---|
| `admissions_requirements__undergraduate_admissions_2_1_minimum_gra__000` | Undergraduate Admissions > 2.1 Minimum Grade Point Average | 306 |
| `admissions_requirements__undergraduate_admissions_2_2_program_spe__001` | Undergraduate Admissions > 2.2 Program Specific Requirements | 404 |
| `admissions_requirements__undergraduate_admissions_2_3_official_tr__002` | Undergraduate Admissions > 2.3 Official Transcripts | 238 |
| `online_programs__online_program_formats_bachelor_of_busin__000` | Online Program Formats > Bachelor of Business Administration in General Business | 350 |
| … 5 more | | |

The GPA **table** was extracted and labelled. This is the full text of chunk `…2_2_program_spe__001`:

```
Where a program states a requirement different from the university minimum, the program requirement applies.

Program: Online BBA in General Business | Minimum GPA: 2.0 | Transferable Credit Hours Required: 30

Program: Online BS in Computer Science | Minimum GPA: 2.75 | Transferable Credit Hours Required: 30

Program: Online BA in Psychology | Minimum GPA: 2.0 | Transferable Credit Hours Required: 24
```

A naive extractor would have returned none of those three rows. → [Step 2 details](steps/s2-index-kb.md)

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
| `p1__c0` asynchronous BBA | `online_programs…bachelor_of_busin__000` | 0.684 |
| `p2__c0` GPA 2.5 | `admissions…2_1_minimum_gra__000` | 0.476 |
| `p3__c0` transcripts | `admissions…2_3_official_tr__002` | 0.602 |
| `p4__c0` parking | `admissions…2_2_program_spe__001` | **0.166** |

The first three found the right section. Parking's best match scores only 0.166, because nothing in the KB is about parking.
For `p2__c0`, the GPA table chunk came second (0.349). → [Step 5 details](steps/s5-retrieve.md)

## Step 6: Judge → `verdicts`

| claim | verdict | tag | attempts | evidence | rationale |
|---|---|---|---|---|---|
| `p1__c0` | `SUPPORTED` | | 1 | `online_programs…bachelor_of_busin__000` | [stub] Excerpt covers 100% of the claim's content words. |
| `p2__c0` | `CONTRADICTED` | | 1 | `admissions…2_1_minimum_gra__000` | [stub] Claim states ['2.5'] but the excerpt states ['2.0', '4.0']. |
| `p3__c0` | `SUPPORTED` | | 1 | `admissions…2_3_official_tr__002` | [stub] Excerpt covers 100% of the claim's content words. |
| `p4__c0` | `NOT_FOUND` | `kb_gap` | **2** | | [stub] Best excerpt overlapped only 25% of the claim. |

What happened to `p4__c0`, following the [retry flow](steps/s6-judge.md#how-the-code-works):

1. First pass: `NOT_FOUND`.
2. Searched again with `k = 5 × 3 = 15`, saved as `attempt = 2`. The index only has 9 chunks, so all 9 came back,
   **4 of which were new**.
3. New evidence existed, so it was judged again. Still `NOT_FOUND` → tagged `kb_gap`, `attempts = 2`.

If the second judgement had come back `SUPPORTED` or `CONTRADICTED`, that would have become the verdict, tagged
`retrieval_miss`: the evidence was in the KB all along, and our first search missed it.

`p2__c0` was contradicted once and is **final**. It doesn't retry, even though another chunk might "fit better".

**Live:** the stub's rule is "same topic words, no numbers in common". A live judge would read the excerpts, apply
the general-policy rule, and quote the decisive wording, e.g. *"Excerpt states 'Minimum GPA: 2.0' for the Online
BBA in General Business."* → [Step 6 details](steps/s6-judge.md)

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
2 conversations | 8 turns | 6 claims | 9 KB chunks indexed

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
    evidence: admissions_requirements.docx > Undergraduate Admissions > 2.1 Minimum Grade Point Average (admissions_requirements__undergraduate_admissions_2_1_minimum_gra__000)

  NOT_FOUND [kb_gap]  conv_demo_001 pair 4
    subject : [stub] unresolved subject
    claim   : Online students who visit campus can purchase a commuter parking permit for 75 dollars per semester.
    why     : [stub] Best excerpt overlapped only 25% of the claim.
```

The header counts cover **every** conversation (4 supported = 2 from each demo), while the sections below are filtered to
`conv_demo_001`. The same data is at `GET /conversations/conv_demo_001` and
`GET /claims/conv_demo_001__p4__c0` (the second shows attempts 1 and 2). → [Step 7 details](steps/s7-aggregate.md)

---

## What this run showed

| Who acts | Finding |
|---|---|
| **Ava's team** | Pair 2: Ava said 2.5, the KB says 2.0 → `CONTRADICTED` |
| **TTU content team** | Pair 4: parking permits aren't in the KB → `NOT_FOUND` + `kb_gap` |
| **Evaluator tuning** | No `retrieval_miss` this run |
| **Nobody** | Pairs 1 and 3 are grounded in the KB |

This is why the verdicts are never merged into one "flagged" count: each row sends work to a different team.
