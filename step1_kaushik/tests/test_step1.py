"""Tests for Step 1 (ingestion and normalization).

Run with:  python -m unittest discover tests
       or: python -m pytest tests
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import run_step1  # noqa: E402
from src import database  # noqa: E402
from src.ingest import IngestError, iter_incoming_files, load_conversations  # noqa: E402
from src.normalize import normalize_conversation  # noqa: E402
from src.validation import validate_conversation  # noqa: E402


def pair(index=0, caller="Hi there", agent="Hello!"):
    return {"pair_index": index, "caller_asked": caller, "agent_said": agent}


def conversation(session_id="conv_demo_001", pairs=None):
    if pairs is None:
        pairs = [pair(0)]
    return {"provider_session_id": session_id, "raw_pairs": pairs}


class ValidationTests(unittest.TestCase):
    def test_valid_single_conversation(self):
        self.assertEqual(validate_conversation(conversation()), (True, None))

    def test_valid_multi_pair_conversation(self):
        conv = conversation(pairs=[pair(0), pair(1), pair(2)])
        self.assertEqual(validate_conversation(conv), (True, None))

    def test_missing_provider_session_id(self):
        conv = conversation()
        del conv["provider_session_id"]
        self._assert_invalid(conv, "missing provider_session_id")

    def test_empty_provider_session_id(self):
        self._assert_invalid(conversation(session_id="   "), "empty")

    def test_non_string_provider_session_id(self):
        self._assert_invalid(conversation(session_id=123), "not a string")

    def test_missing_raw_pairs(self):
        self._assert_invalid({"provider_session_id": "c1"}, "missing raw_pairs")

    def test_empty_raw_pairs(self):
        self._assert_invalid(conversation(pairs=[]), "raw_pairs is empty")

    def test_pair_not_an_object(self):
        self._assert_invalid(conversation(pairs=["nope"]), "not a JSON object")

    def test_missing_pair_index(self):
        self._assert_invalid(
            conversation(pairs=[{"caller_asked": "Hi", "agent_said": "Hello"}]),
            "missing pair_index",
        )

    def test_missing_caller_asked(self):
        self._assert_invalid(
            conversation(pairs=[{"pair_index": 0, "agent_said": "Hello"}]),
            "missing caller_asked",
        )

    def test_empty_caller_asked(self):
        self._assert_invalid(conversation(pairs=[pair(0, caller="   ")]), "empty caller_asked")

    def test_missing_agent_said(self):
        self._assert_invalid(
            conversation(pairs=[{"pair_index": 0, "caller_asked": "Hi"}]),
            "missing agent_said",
        )

    def test_empty_agent_said(self):
        self._assert_invalid(conversation(pairs=[pair(0, agent="\n\t ")]), "empty agent_said")

    def test_duplicate_pair_index(self):
        self._assert_invalid(conversation(pairs=[pair(0), pair(0)]), "duplicate pair_index")

    def test_gap_in_pair_index(self):
        self._assert_invalid(conversation(pairs=[pair(0), pair(2)]), "not sequential")

    def test_pair_index_not_starting_at_zero(self):
        self._assert_invalid(conversation(pairs=[pair(1), pair(2)]), "not sequential")

    def test_non_integer_pair_index(self):
        self._assert_invalid(conversation(pairs=[pair("0")]), "non-integer pair_index")
        self._assert_invalid(conversation(pairs=[pair(1.0)]), "non-integer pair_index")
        self._assert_invalid(conversation(pairs=[pair(True)]), "non-integer pair_index")

    def _assert_invalid(self, conv, expected_fragment):
        is_valid, reason = validate_conversation(conv)
        self.assertFalse(is_valid)
        self.assertIn(expected_fragment, reason)


class NormalizeTests(unittest.TestCase):
    def test_renames_and_flattens(self):
        conv = conversation(pairs=[pair(0), pair(1, caller="Bye", agent="Goodbye")])
        self.assertEqual(
            normalize_conversation(conv),
            [
                {
                    "session_id": "conv_demo_001",
                    "pair_index": 0,
                    "caller_asked": "Hi there",
                    "agent_said": "Hello!",
                },
                {
                    "session_id": "conv_demo_001",
                    "pair_index": 1,
                    "caller_asked": "Bye",
                    "agent_said": "Goodbye",
                },
            ],
        )

    def test_strips_whitespace_but_keeps_wording(self):
        conv = conversation(pairs=[pair(0, caller="  What are your hours?  ", agent=" 9 to 5 ")])
        turn = normalize_conversation(conv)[0]
        self.assertEqual(turn["caller_asked"], "What are your hours?")
        self.assertEqual(turn["agent_said"], "9 to 5")

    def test_preserves_pair_order(self):
        conv = conversation(pairs=[pair(0), pair(1), pair(2)])
        indexes = [turn["pair_index"] for turn in normalize_conversation(conv)]
        self.assertEqual(indexes, [0, 1, 2])


class PipelineTestCase(unittest.TestCase):
    """Base class giving each test a throwaway incoming dir and database."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.incoming = os.path.join(self.temp.name, "incoming")
        os.makedirs(self.incoming)
        self.db_path = os.path.join(self.temp.name, "test_eval_store.db")

    def tearDown(self):
        self.temp.cleanup()

    def write_file(self, name, payload, raw_text=None):
        path = os.path.join(self.incoming, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(raw_text if raw_text is not None else json.dumps(payload))
        return path

    def run_pipeline(self):
        return run_step1(db_path=self.db_path, incoming_dir=self.incoming)

    def fetch_turns(self):
        connection = database.connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT session_id, pair_index, caller_asked, agent_said"
                " FROM step1_turns ORDER BY session_id, pair_index"
            ).fetchall()
            return [tuple(row) for row in rows]
        finally:
            connection.close()

    def fetch_quarantine(self):
        connection = database.connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT session_id, reason FROM step1_quarantine ORDER BY id"
            ).fetchall()
            return [tuple(row) for row in rows]
        finally:
            connection.close()


class IngestTests(PipelineTestCase):
    def test_loads_single_conversation_object(self):
        path = self.write_file("one.json", conversation())
        self.assertEqual(len(load_conversations(path)), 1)

    def test_loads_array_of_conversations(self):
        path = self.write_file("many.json", [conversation("c1"), conversation("c2")])
        self.assertEqual(len(load_conversations(path)), 2)

    def test_malformed_json_raises_ingest_error(self):
        path = self.write_file("bad.json", None, raw_text="{not json")
        with self.assertRaises(IngestError):
            load_conversations(path)

    def test_iter_incoming_files_only_returns_json(self):
        self.write_file("a.json", conversation("c1"))
        self.write_file("notes.txt", None, raw_text="ignore me")
        files = list(iter_incoming_files(self.incoming))
        self.assertEqual([os.path.basename(p) for p in files], ["a.json"])


class EndToEndTests(PipelineTestCase):
    def test_valid_single_conversation_is_stored(self):
        self.write_file("one.json", conversation(pairs=[pair(0), pair(1)]))
        summary = self.run_pipeline()
        self.assertEqual(summary["conversations_accepted"], 1)
        self.assertEqual(summary["conversations_quarantined"], 0)
        self.assertEqual(summary["turns_stored"], 2)
        self.assertEqual(len(self.fetch_turns()), 2)

    def test_file_with_multiple_conversations(self):
        self.write_file("many.json", [conversation("c1"), conversation("c2", [pair(0), pair(1)])])
        summary = self.run_pipeline()
        self.assertEqual(summary["files_processed"], 1)
        self.assertEqual(summary["conversations_accepted"], 2)
        self.assertEqual(summary["turns_stored"], 3)

    def test_malformed_json_file_is_quarantined_and_run_continues(self):
        self.write_file("a_bad.json", None, raw_text="{ oops")
        self.write_file("b_good.json", conversation("c_good"))
        summary = self.run_pipeline()
        self.assertEqual(summary["files_processed"], 2)
        self.assertEqual(summary["files_failed"], 1)
        self.assertEqual(summary["conversations_accepted"], 1)
        self.assertEqual(summary["conversations_quarantined"], 1)
        self.assertIn("malformed JSON", self.fetch_quarantine()[0][1])

    def test_invalid_conversation_does_not_block_valid_one(self):
        payload = [
            conversation("c_bad", [pair(0), pair(2)]),  # gap in index
            conversation("c_good", [pair(0), pair(1)]),
            {"raw_pairs": [pair(0)]},  # no session id
        ]
        self.write_file("mixed.json", payload)
        summary = self.run_pipeline()
        self.assertEqual(summary["conversations_accepted"], 1)
        self.assertEqual(summary["conversations_quarantined"], 2)
        self.assertEqual(
            self.fetch_turns(),
            [
                ("c_good", 0, "Hi there", "Hello!"),
                ("c_good", 1, "Hi there", "Hello!"),
            ],
        )
        self.assertEqual(
            self.fetch_quarantine(),
            [
                ("c_bad", "pair_index values are not sequential starting from 0"),
                (None, "missing provider_session_id"),
            ],
        )

    def test_invalid_conversations_are_quarantined_with_reasons(self):
        cases = {
            "missing_session.json": {"raw_pairs": [pair(0)]},
            "empty_session.json": conversation(session_id=""),
            "missing_pairs.json": {"provider_session_id": "c1"},
            "empty_pairs.json": conversation(pairs=[]),
            "missing_caller.json": conversation(pairs=[{"pair_index": 0, "agent_said": "Hi"}]),
            "empty_caller.json": conversation(pairs=[pair(0, caller=" ")]),
            "missing_agent.json": conversation(pairs=[{"pair_index": 0, "caller_asked": "Hi"}]),
            "empty_agent.json": conversation(pairs=[pair(0, agent=" ")]),
            "duplicate_index.json": conversation(pairs=[pair(0), pair(0)]),
            "gap_index.json": conversation(pairs=[pair(0), pair(2)]),
            "string_index.json": conversation(pairs=[pair("0")]),
        }
        for name, payload in cases.items():
            self.write_file(name, payload)

        summary = self.run_pipeline()
        self.assertEqual(summary["files_processed"], len(cases))
        self.assertEqual(summary["conversations_accepted"], 0)
        self.assertEqual(summary["conversations_quarantined"], len(cases))
        self.assertEqual(self.fetch_turns(), [])

    def test_rerun_does_not_duplicate_rows(self):
        self.write_file("one.json", conversation(pairs=[pair(0), pair(1)]))
        first = self.run_pipeline()
        second = self.run_pipeline()

        self.assertEqual(first["turns_stored"], 2)
        self.assertEqual(second["turns_stored"], 2)  # written again, but in place
        self.assertEqual(len(self.fetch_turns()), 2)

    def test_rerun_keeps_original_row_ids(self):
        self.write_file("one.json", conversation(pairs=[pair(0), pair(1)]))
        self.run_pipeline()
        before = self._row_ids()
        self.run_pipeline()
        self.assertEqual(self._row_ids(), before)

    def test_rerun_with_edited_text_updates_in_place(self):
        self.write_file("one.json", conversation(pairs=[pair(0)]))
        self.run_pipeline()
        self.write_file("one.json", conversation(pairs=[pair(0, agent="Hello again!")]))
        self.run_pipeline()
        self.assertEqual(self.fetch_turns(), [("conv_demo_001", 0, "Hi there", "Hello again!")])

    def test_missing_incoming_dir_is_not_an_error(self):
        summary = run_step1(
            db_path=self.db_path, incoming_dir=os.path.join(self.temp.name, "nope")
        )
        self.assertEqual(summary["files_processed"], 0)

    def _row_ids(self):
        connection = database.connect(self.db_path)
        try:
            rows = connection.execute("SELECT id FROM step1_turns ORDER BY id").fetchall()
            return [row[0] for row in rows]
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
