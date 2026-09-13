"""The contracts passed between pipeline steps.

Two families of model live here and it is worth knowing which is which:

* **Internal models** (``Turn``, ``Claim``, ``ClaimVerdict`` ...) are what the
  pipeline passes around and what the database stores.
* **LLM response models** (``TurnClassification``, ``ConversationClaims``,
  ``JudgeResponse``) are handed to Gemini as a JSON Schema so the model is
  *constrained* to return that exact shape instead of prose we have to parse.

The LLM models are deliberately plain -- flat fields, no unions, no nullable
types.  A nullable field in a generated JSON Schema is a common source of
provider-side rejections, so where "no value" is possible we use an empty
string and say so in the field description.  The model reads those descriptions,
so they are part of the prompt, not just documentation.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Verdict(str, Enum):
    """The judge's three-way answer for a single claim."""

    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    NOT_FOUND = "NOT_FOUND"


class VerdictTag(str, Enum):
    """Why a NOT_FOUND happened -- which points at who needs to act.

    ``KB_GAP`` means the knowledge base genuinely lacks the content (TTU's
    content team).  ``RETRIEVAL_MISS`` means the content existed but our own
    search failed to surface it on the first attempt (our tuning problem).
    """

    KB_GAP = "kb_gap"
    RETRIEVAL_MISS = "retrieval_miss"


class ConversationStatus(str, Enum):
    """Whether an ingested conversation survived validation."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


# Severity ordering used for the turn-level rollup.  Mirrored in SQL in the
# v_turn_severity view (db/migrations) -- if you change one, change the other,
# and change the view through a new migration.
SEVERITY: dict[Verdict, int] = {
    Verdict.CONTRADICTED: 3,
    Verdict.NOT_FOUND: 2,
    Verdict.SUPPORTED: 1,
}


# ---------------------------------------------------------------------------
# Step 1 -- ingest
# ---------------------------------------------------------------------------


class Turn(BaseModel):
    """One caller question and the agent's answer to it."""

    session_id: str
    pair_index: int
    caller_asked: str
    agent_said: str


class Conversation(BaseModel):
    """A validated conversation and its ordered turns."""

    session_id: str
    source_file: str
    turns: list[Turn]


class RejectedConversation(BaseModel):
    """A conversation that failed validation, kept so counts stay honest."""

    session_id: str
    source_file: str
    reason: str


# ---------------------------------------------------------------------------
# Step 2 -- knowledge base
# ---------------------------------------------------------------------------


class KbBlock(BaseModel):
    """One paragraph (or flattened table row) lifted out of a .docx."""

    text: str
    heading_path: list[str]
    ordinal: int


class KbChunk(BaseModel):
    """A retrievable unit of the knowledge base, before embedding."""

    chunk_id: str
    doc_id: str
    section: str
    ordinal: int
    text: str


# ---------------------------------------------------------------------------
# Step 3 -- turn classification  (LLM response schema)
# ---------------------------------------------------------------------------


class TurnClassification(BaseModel):
    """Gemini's answer to 'does this turn contain anything checkable?'."""

    # strict: only a JSON true/false is accepted. Without it pydantic would also
    # turn strings such as "yes" or "false" into a bool, hiding a reply that did
    # not follow the schema.
    contains_claims: bool = Field(
        strict=True,
        description=(
            "True if ANY part of the agent's reply states a checkable fact about "
            "TTU, even if the rest of the reply is filler or a follow-up question."
        ),
    )
    reason: str = Field(description="One short sentence justifying the decision.")


class ClassifiedTurn(BaseModel):
    """A turn plus its classification, as persisted."""

    session_id: str
    pair_index: int
    contains_claims: bool
    reason: str


# ---------------------------------------------------------------------------
# Step 4 -- claim decomposition  (LLM response schema)
# ---------------------------------------------------------------------------


class ExtractedClaim(BaseModel):
    """A single self-contained factual claim pulled out of one agent reply."""

    text: str = Field(
        description=(
            "The claim, rewritten to stand on its own: it must name the program, "
            "policy or topic it applies to, so it is checkable without the "
            "surrounding conversation."
        )
    )
    scope_derived_from_pair_index: list[int] = Field(
        default_factory=list,
        description=(
            "Which earlier turns supplied the scope/qualifiers you added to this "
            "claim. Empty if the claim was already self-contained."
        ),
    )


class TurnClaims(BaseModel):
    """All claims found in one turn, plus the subject that turn is about."""

    pair_index: int = Field(description="The turn these claims came from.")
    subject_tag: str = Field(
        description=(
            "The specific subject this turn is about, e.g. 'Online BBA - General "
            "Business'. Re-derive it for every turn; never assume it carried over."
        )
    )
    claims: list[ExtractedClaim] = Field(
        default_factory=list,
        description="Empty list if this turn turned out to contain nothing checkable.",
    )


class ConversationClaims(BaseModel):
    """Gemini's per-conversation decomposition response."""

    turns: list[TurnClaims]


class Claim(BaseModel):
    """A persisted claim, with the identity the rest of the pipeline keys on."""

    claim_id: str
    session_id: str
    pair_index: int
    subject_tag: str
    text: str
    scope_derived_from: list[int] = []


# ---------------------------------------------------------------------------
# Step 5 -- retrieval
# ---------------------------------------------------------------------------


class RetrievedChunk(BaseModel):
    """A KB chunk returned by similarity search, with its score and provenance."""

    chunk_id: str
    text: str
    score: float
    source_doc: str
    section: str
    rank: int


# ---------------------------------------------------------------------------
# Step 6 -- judging  (LLM response schema)
# ---------------------------------------------------------------------------


class JudgeResponse(BaseModel):
    """Gemini's verdict on one claim against one set of retrieved chunks."""

    verdict: Verdict = Field(
        description=(
            "SUPPORTED if the excerpts confirm the claim. CONTRADICTED if they "
            "state something incompatible with it. NOT_FOUND if they simply do "
            "not address it."
        )
    )
    rationale: str = Field(
        description="Two sentences at most, quoting the decisive wording from the excerpt."
    )
    evidence_chunk_id: str = Field(
        default="",
        description=(
            "The chunk_id that decided the verdict. Empty string when the verdict "
            "is NOT_FOUND and no excerpt was relevant."
        ),
    )


class ClaimVerdict(BaseModel):
    """A persisted verdict for one claim."""

    claim_id: str
    verdict: Verdict
    tag: VerdictTag | None = None
    rationale: str = ""
    evidence_chunk_id: str | None = None
    attempts: int = 1
