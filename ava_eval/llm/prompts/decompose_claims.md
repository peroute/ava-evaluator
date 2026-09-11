You are extracting checkable factual claims from one complete conversation
between a caller and Ava, a voice agent for Texas Tech University Online.

You are given the **whole conversation in order**, and your job is to turn each
flagged agent reply into a list of claims that can each be looked up in TTU's
documentation **on their own**, with no access to the rest of the conversation.

## Why self-contained matters

A downstream step searches the knowledge base using nothing but the claim text.
So a claim like "the minimum GPA is 2.0" is useless — 2.0 for *what program*?
It cannot be confirmed or refuted as written. The same sentence, rewritten as
"For the Online BBA in General Business, the minimum GPA is 2.0", is checkable.

Your job is to do that rewriting: pull the scope (program, policy, term,
audience) out of the conversation and put it **inside** each claim.

## The trap: scope does not automatically carry over

Callers change topic mid-conversation. Someone can ask about a business program
in turn 2 and a computer science program in turn 4, and the agent answers both.
If you assume the program from turn 2 still applies at turn 4, you will attach
the wrong program to a claim and the whole verdict will be wrong — worse than
useless, because it looks confident.

So, for **every** turn:

1. **Re-derive the subject from scratch.** Never inherit it by default. Ask
   yourself what *this* turn is about, then check whether the conversation
   actually supports that reading.
2. **Read `caller_asked`, not just `agent_said`.** The caller's own words are
   the clearest signal that the topic just changed.
3. **Record `subject_tag`** for the turn — the specific program, policy, or
   topic it concerns (e.g. `Online BBA - General Business`, `Financial Aid -
   FAFSA deadlines`). If the turn is genuinely general and not about a specific
   program, say so (e.g. `University-wide admissions`).
4. **Record `scope_derived_from_pair_index`** on each claim — the list of
   earlier `pair_index` values you drew scope or qualifiers from. Use an empty
   list when the turn was already self-contained. This exists so a scoping
   mistake can be found afterwards rather than merely being less likely.

## Rules for the claims themselves

- One fact per claim. If a reply states a GPA requirement *and* a transcript
  requirement, that is two claims.
- Use the agent's own meaning. Do not soften, strengthen, correct, or complete
  it from your own knowledge — you are recording what was said, not what is
  true. A later step decides truth.
- Keep numbers, dates, deadlines, and names exactly as the agent stated them.
- Skip pure filler, pleasantries, and questions back to the caller.
- Only produce claims for turns marked `contains_claims: true` below.
- If a flagged turn turns out to hold nothing checkable after all, return it
  with an empty `claims` list. That is an expected outcome, not an error — the
  flagging step is deliberately over-inclusive.

## Worked example

Conversation:

```
[pair 0] caller: Hi, I want to know about your online business degree.
         agent:  Happy to help! Which one are you interested in?
         contains_claims: false
[pair 1] caller: The general business one.
         agent:  The Online BBA in General Business is fully asynchronous.
         contains_claims: true
[pair 2] caller: What do I need to get in?
         agent:  You need a 2.0 GPA, and official transcripts from every school
                 you have attended.
         contains_claims: true
```

Correct output:

```json
{
  "turns": [
    {
      "pair_index": 1,
      "subject_tag": "Online BBA - General Business",
      "claims": [
        {
          "text": "The Online Bachelor of Business Administration in General Business is fully asynchronous.",
          "scope_derived_from_pair_index": []
        }
      ]
    },
    {
      "pair_index": 2,
      "subject_tag": "Online BBA - General Business",
      "claims": [
        {
          "text": "For the Online BBA in General Business, the minimum GPA is 2.0 from the applicant's previous institution.",
          "scope_derived_from_pair_index": [1]
        },
        {
          "text": "For the Online BBA in General Business, official transcripts are required from all previously attended institutions.",
          "scope_derived_from_pair_index": [1]
        }
      ]
    }
  ]
}
```

Note that pair 2 produced two claims, both of which had to borrow their program
scope from pair 1 — and both say so.

## The conversation

Session: {{SESSION_ID}}

{{TRANSCRIPT}}

## Your answer

Return JSON matching the provided schema. Include an entry for every turn marked
`contains_claims: true`, and no entries for any other turn.
