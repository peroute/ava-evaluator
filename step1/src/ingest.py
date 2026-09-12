"""Step 1 ingestion: get raw conversation dicts from a source.

A "source" is anything that yields dicts shaped like:

    {"provider_session_id": "...", "raw_pairs": [{"pair_index": 0, ...}, ...]}

Today the only source is JSON files in data/incoming/. A future ElevenLabs
adapter only has to produce the same shape (see elevenlabs_adapter_example at
the bottom of this file); validation, normalization and persistence stay
unchanged.
"""

import json
import os

DEFAULT_INCOMING_DIR = os.path.join("data", "incoming")


class IngestError(Exception):
    """Raised when a file cannot be read or is not valid JSON."""


def iter_incoming_files(incoming_dir=DEFAULT_INCOMING_DIR):
    """Yield the path of every .json file in incoming_dir, sorted by name."""
    if not os.path.isdir(incoming_dir):
        return
    for name in sorted(os.listdir(incoming_dir)):
        if name.lower().endswith(".json"):
            path = os.path.join(incoming_dir, name)
            if os.path.isfile(path):
                yield path


def load_json_file(path):
    """Read one JSON file. Raises IngestError on bad JSON or read problems."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as error:
        raise IngestError("malformed JSON: %s" % error) from error
    except OSError as error:
        raise IngestError("could not read file: %s" % error) from error


def load_conversations(path):
    """Load a file and return a list of raw conversation objects.

    The file may hold a single conversation object or an array of them.
    """
    payload = load_json_file(path)
    return as_conversation_list(payload)


def as_conversation_list(payload):
    """Normalize the container shape only: one object -> [object]."""
    if isinstance(payload, list):
        return list(payload)
    return [payload]


def elevenlabs_adapter_example(api_conversation):
    """Sketch of how a future adapter would map API data into our shape.

    Not used by Step 1 and not wired into main.py - it exists to show that a
    new source only needs to build the same dict.
    """
    return {
        "provider_session_id": api_conversation["conversation_id"],
        "raw_pairs": [
            {
                "pair_index": index,
                "caller_asked": turn["user"],
                "agent_said": turn["agent"],
            }
            for index, turn in enumerate(api_conversation["turns"])
        ],
    }
