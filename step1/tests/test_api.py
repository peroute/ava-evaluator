"""Tests for the read-only dashboard API.

Each test builds a throwaway database by running the real Step 1 pipeline, then
points a fresh app at it. Run with:  python -m pytest tests
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from main import run_step1  # noqa: E402
from web.api import create_app  # noqa: E402

VALID = {
    "provider_session_id": "conv_a",
    "raw_pairs": [
        {"pair_index": 0, "caller_asked": "Hi there", "agent_said": "Hello!"},
        {"pair_index": 1, "caller_asked": "Opening hours?", "agent_said": "9 to 5."},
    ],
}

SECOND_VALID = {
    "provider_session_id": "conv_b",
    "raw_pairs": [{"pair_index": 0, "caller_asked": "Bye", "agent_said": "Goodbye"}],
}

# Rejected: pair_index has a gap.
INVALID = {
    "provider_session_id": "conv_bad",
    "raw_pairs": [
        {"pair_index": 0, "caller_asked": "Hi", "agent_said": "Hello"},
        {"pair_index": 2, "caller_asked": "Hi", "agent_said": "Hello"},
    ],
}


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.incoming = os.path.join(self.temp.name, "incoming")
        os.makedirs(self.incoming)
        self.db_path = os.path.join(self.temp.name, "eval_store.db")

    def tearDown(self):
        self.temp.cleanup()

    def write_file(self, name, payload):
        with open(os.path.join(self.incoming, name), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def seed(self, *payloads):
        """Write payloads to the temp incoming dir and run Step 1 over them."""
        for number, payload in enumerate(payloads):
            self.write_file("file_%d.json" % number, payload)
        return run_step1(db_path=self.db_path, incoming_dir=self.incoming)

    def client(self):
        return TestClient(create_app(db_path=self.db_path))


class SummaryTests(ApiTestCase):
    def test_summary_counts(self):
        self.seed(VALID, SECOND_VALID, INVALID)
        body = self.client().get("/api/summary").json()
        self.assertEqual(
            body,
            {"conversations_accepted": 2, "turns_stored": 3, "quarantined_records": 1},
        )

    def test_summary_is_zero_on_empty_database(self):
        self.seed()  # creates the tables, stores nothing
        body = self.client().get("/api/summary").json()
        self.assertEqual(
            body,
            {"conversations_accepted": 0, "turns_stored": 0, "quarantined_records": 0},
        )

    def test_missing_database_returns_503(self):
        response = self.client().get("/api/summary")
        self.assertEqual(response.status_code, 503)
        self.assertIn("database not found", response.json()["detail"])


class ConversationTests(ApiTestCase):
    def test_lists_unique_sessions_with_counts(self):
        self.seed(VALID, SECOND_VALID)
        body = self.client().get("/api/conversations").json()
        self.assertEqual(
            [(c["session_id"], c["turn_count"]) for c in body["conversations"]],
            [("conv_a", 2), ("conv_b", 1)],
        )

    def test_turns_returned_in_pair_index_order(self):
        self.seed(VALID)
        body = self.client().get("/api/conversations/conv_a").json()
        self.assertEqual(body["session_id"], "conv_a")
        self.assertEqual(body["turn_count"], 2)
        self.assertEqual([turn["pair_index"] for turn in body["turns"]], [0, 1])
        self.assertEqual(body["turns"][0]["caller_asked"], "Hi there")
        self.assertEqual(body["turns"][0]["agent_said"], "Hello!")

    def test_unknown_session_returns_404(self):
        self.seed(VALID)
        response = self.client().get("/api/conversations/nope")
        self.assertEqual(response.status_code, 404)

    def test_quarantined_session_is_not_listed(self):
        self.seed(INVALID)
        body = self.client().get("/api/conversations").json()
        self.assertEqual(body["conversations"], [])

    def test_session_id_with_odd_characters_is_handled(self):
        # Proves the lookup is parameterized rather than string-concatenated.
        odd = {
            "provider_session_id": "conv'; DROP TABLE step1_turns; --",
            "raw_pairs": [{"pair_index": 0, "caller_asked": "Hi", "agent_said": "Hello"}],
        }
        self.seed(odd)
        client = self.client()
        response = client.get("/api/conversations/%s" % odd["provider_session_id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["turn_count"], 1)
        # The table still exists afterwards.
        self.assertEqual(client.get("/api/summary").json()["turns_stored"], 1)


class QuarantineTests(ApiTestCase):
    def test_returns_reason_and_payload(self):
        self.seed(INVALID)
        records = self.client().get("/api/quarantine").json()["records"]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["session_id"], "conv_bad")
        self.assertIn("not sequential", record["reason"])
        self.assertIn("created_at", record)
        self.assertEqual(json.loads(record["raw_payload"])["provider_session_id"], "conv_bad")

    def test_empty_when_nothing_quarantined(self):
        self.seed(VALID)
        self.assertEqual(self.client().get("/api/quarantine").json()["records"], [])


class ReadOnlyTests(ApiTestCase):
    def test_connection_cannot_write(self):
        self.seed(VALID)
        from web import queries

        connection = queries.connect_read_only(self.db_path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM step1_turns")
        finally:
            connection.close()

    def test_api_exposes_no_write_routes(self):
        methods = set()
        for route in create_app(db_path=self.db_path).routes:
            methods.update(getattr(route, "methods", set()) or set())
        self.assertEqual(methods - {"GET", "HEAD"}, set())

    def test_pipeline_data_is_untouched_after_reads(self):
        self.seed(VALID)
        client = self.client()
        client.get("/api/summary")
        client.get("/api/conversations")
        client.get("/api/conversations/conv_a")
        client.get("/api/quarantine")

        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM step1_turns").fetchone()[0], 2
            )
        finally:
            connection.close()


class StaticPageTests(ApiTestCase):
    def test_index_is_served(self):
        self.seed(VALID)
        response = self.client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Step 1 Inspector", response.text)

    def test_static_assets_are_served(self):
        self.seed(VALID)
        client = self.client()
        self.assertEqual(client.get("/static/app.js").status_code, 200)
        self.assertEqual(client.get("/static/styles.css").status_code, 200)


if __name__ == "__main__":
    unittest.main()
