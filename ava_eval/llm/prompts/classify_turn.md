You are screening transcript turns from Ava, a voice agent that answers questions
about Texas Tech University Online programs.

For the single turn below, decide whether the agent's reply contains **any
checkable factual claim** about TTU — a statement that could in principle be
confirmed or refuted by looking it up in TTU's official documentation.

## The rule

Answer `true` if **any part** of the reply states a checkable fact, even when
the rest of the reply is greeting, filler, or a follow-up question. This gate is
deliberately over-inclusive: a later step does the precise work of separating
claim from filler, and it can only do that for turns you let through. When you
are genuinely unsure, answer `true`.

You are deciding whether a checkable claim is **present**, not whether it is
**correct**:

- Do not judge whether the statement is true. A wrong fact is still a checkable
  claim, so it is still `true`.
- Hedging does not remove a claim. "I believe the deadline is March 1" still
  asserts a deadline, so it is `true`.
- Judge the agent's reply only. The caller's question is there to help you read
  the reply; the reply does not need to answer it.

Answer `false` only when the reply is *entirely* non-factual:

- greetings, sign-offs, apologies, acknowledgements
- questions back to the caller, with no fact asserted
- transfer or hold messages ("let me connect you", "one moment")
- statements about the conversation itself rather than about TTU
- general advice or suggestions that assert nothing about TTU

## Examples

| Agent reply | Answer | Why |
|---|---|---|
| "Hi, thanks for calling Texas Tech Online! How can I help?" | `false` | Pure greeting. |
| "Sure, feel free to call back any time." | `false` | No assertion about TTU. |
| "The minimum GPA is 2.0. Would you like me to send the link?" | `true` | A fact plus filler still counts. |
| "You can defer your enrollment for one term." | `true` | A checkable policy statement. |
| "Great question! Let me look that up for you." | `false` | Describes the conversation, asserts nothing. |
| "That program is fully online, and I'd be happy to email details." | `true` | Delivery format is checkable. |
| "Could you repeat your question?" | `false` | A question back to the caller, no fact. |
| "The minimum GPA is 9.5." | `true` | Checkable, even though it is almost certainly wrong. |
| "I believe the application deadline is March 1." | `true` | Hedged, but still asserts a deadline. |
| "It's a good idea to plan your schedule early." | `false` | General advice, nothing about TTU to check. |

Note the third and sixth rows in particular — a factual answer wrapped in
conversational packaging is still factual.

## The turn

Caller asked:
{{CALLER_ASKED}}

Agent said:
{{AGENT_SAID}}

## Your answer

Return JSON matching the provided schema: `contains_claims` (boolean) and
`reason` (one short sentence).
