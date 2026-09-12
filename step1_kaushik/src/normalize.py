"""Step 1 normalization: flatten a validated conversation into turn records.

Purely mechanical: rename, flatten, trim whitespace. No classification,
summarization, correction or enrichment of any kind.
"""


def normalize_conversation(conversation):
    """Return a list of normalized turn dicts for one validated conversation.

    Assumes validate_conversation() already returned (True, None).
    """
    session_id = conversation["provider_session_id"].strip()

    turns = []
    for pair in conversation["raw_pairs"]:
        turns.append(normalize_pair(session_id, pair))
    return turns


def normalize_pair(session_id, pair):
    """Turn one raw pair into one flat record, keeping the original wording."""
    return {
        "session_id": session_id,
        "pair_index": pair["pair_index"],
        "caller_asked": pair["caller_asked"].strip(),
        "agent_said": pair["agent_said"].strip(),
    }
