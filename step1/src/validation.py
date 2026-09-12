"""Step 1 validation: decide whether a raw conversation is safe to normalize.

A conversation is either accepted as-is or quarantined whole. Nothing is
repaired here - if pair indexes are wrong, the conversation is rejected.
"""


def validate_conversation(conversation):
    """Check one raw conversation dict.

    Returns (True, None) if valid, otherwise (False, "reason").
    """
    if not isinstance(conversation, dict):
        return False, "conversation is not a JSON object"

    ok, reason = _validate_session_id(conversation)
    if not ok:
        return False, reason

    if "raw_pairs" not in conversation:
        return False, "missing raw_pairs"

    pairs = conversation["raw_pairs"]
    if not isinstance(pairs, list):
        return False, "raw_pairs is not a list"
    if len(pairs) == 0:
        return False, "raw_pairs is empty"

    indexes = []
    for position, pair in enumerate(pairs):
        ok, reason = _validate_pair(pair, position)
        if not ok:
            return False, reason
        indexes.append(pair["pair_index"])

    return _validate_indexes(indexes)


def _validate_session_id(conversation):
    if "provider_session_id" not in conversation:
        return False, "missing provider_session_id"

    session_id = conversation["provider_session_id"]
    if not isinstance(session_id, str):
        return False, "provider_session_id is not a string"
    if session_id.strip() == "":
        return False, "provider_session_id is empty"
    return True, None


def _validate_pair(pair, position):
    """Validate a single raw pair. `position` is only used for error messages."""
    if not isinstance(pair, dict):
        return False, "pair at position %d is not a JSON object" % position

    if "pair_index" not in pair:
        return False, "pair at position %d is missing pair_index" % position

    index = pair["pair_index"]
    # bool is a subclass of int in Python, so reject it explicitly.
    if not isinstance(index, int) or isinstance(index, bool):
        return False, "pair at position %d has a non-integer pair_index" % position

    for field in ("caller_asked", "agent_said"):
        if field not in pair:
            return False, "pair_index %d is missing %s" % (index, field)
        value = pair[field]
        if not isinstance(value, str):
            return False, "pair_index %d has a non-string %s" % (index, field)
        if value.strip() == "":
            return False, "pair_index %d has an empty %s" % (index, field)

    return True, None


def _validate_indexes(indexes):
    """Indexes must be unique and cover 0..n-1 with no gaps."""
    if len(set(indexes)) != len(indexes):
        return False, "duplicate pair_index values"
    if sorted(indexes) != list(range(len(indexes))):
        return False, "pair_index values are not sequential starting from 0"
    return True, None
