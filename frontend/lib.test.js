import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  claimState,
  detailRoute,
  errorDetail,
  filterTurns,
  groupClaimsByTurn,
  isIncomplete,
  isStale,
  needsReview,
  orderConversations,
  orderTurns,
  parseRoute,
  stateInfo,
  tagLabel,
  toCount,
} from "./lib.js";

const turn = (overrides) => ({ pair_index: 0, turn_verdict: "SUPPORTED", unjudged_count: 0, ...overrides });

describe("claimState", () => {
  test("a claim with no verdict row is UNJUDGED", () => {
    assert.equal(claimState({ verdict: null }), "UNJUDGED");
    assert.equal(claimState({}), "UNJUDGED");
  });

  test("a judged claim keeps its verdict", () => {
    assert.equal(claimState({ verdict: "NOT_FOUND" }), "NOT_FOUND");
  });
});

describe("stateInfo", () => {
  test("UNJUDGED has its own label and tone", () => {
    assert.equal(stateInfo("UNJUDGED").label, "Not judged");
    assert.equal(stateInfo("UNJUDGED").tone, "unjudged");
  });

  test("an unknown label falls back to neutral and keeps the raw text", () => {
    const info = stateInfo("MAYBE");
    assert.deepEqual({ label: info.label, tone: info.tone, rank: info.rank }, { label: "MAYBE", tone: "neutral", rank: 0 });
    assert.equal(stateInfo(null).label, "Unknown state");
  });

  test("inherited object keys are not states", () => {
    assert.equal(stateInfo("toString").rank, 0);
  });
});

describe("turn review state", () => {
  test("a supported turn with a failed claim is incomplete and needs review", () => {
    const partial = turn({ unjudged_count: 1 });
    assert.equal(isIncomplete(partial), true);
    assert.equal(needsReview(partial), true);
  });

  test("a fully supported or skipped turn needs no review", () => {
    assert.equal(needsReview(turn({})), false);
    assert.equal(needsReview(turn({ turn_verdict: "NOT_EVALUATED" })), false);
  });

  test("contradicted, not found and unjudged turns need review", () => {
    for (const verdict of ["CONTRADICTED", "NOT_FOUND", "UNJUDGED"]) {
      assert.equal(needsReview(turn({ turn_verdict: verdict })), true, verdict);
    }
  });

  test("null counts read as zero", () => {
    assert.equal(toCount(null), 0);
    assert.equal(isIncomplete(turn({ unjudged_count: null })), false);
  });
});

describe("orderTurns", () => {
  const turns = [
    turn({ pair_index: 0, turn_verdict: "SUPPORTED" }),
    turn({ pair_index: 1, turn_verdict: "NOT_EVALUATED" }),
    turn({ pair_index: 2, turn_verdict: "CONTRADICTED" }),
    turn({ pair_index: 3, turn_verdict: "UNJUDGED", unjudged_count: 1 }),
    turn({ pair_index: 4, turn_verdict: "SUPPORTED", unjudged_count: 2 }),
    turn({ pair_index: 5, turn_verdict: "NOT_FOUND" }),
  ];

  test("worst first ranks incomplete turns with unjudged ones, ties by pair_index", () => {
    assert.deepEqual(orderTurns(turns, "worst").map((t) => t.pair_index), [2, 5, 3, 4, 0, 1]);
  });

  test("transcript order sorts by pair_index without mutating the input", () => {
    const shuffled = [turns[3], turns[0], turns[5]];
    assert.deepEqual(orderTurns(shuffled, "transcript").map((t) => t.pair_index), [0, 3, 5]);
    assert.deepEqual(shuffled.map((t) => t.pair_index), [3, 0, 5]);
  });

  test("the review filter keeps only turns that need review", () => {
    assert.deepEqual(filterTurns(turns, "review").map((t) => t.pair_index), [2, 3, 4, 5]);
    assert.equal(filterTurns(turns, "all").length, turns.length);
  });
});

describe("claims and tags", () => {
  test("claims group by pair_index in their original order", () => {
    const grouped = groupClaimsByTurn([
      { claim_id: "a", pair_index: 1 },
      { claim_id: "b", pair_index: 0 },
      { claim_id: "c", pair_index: 1 },
    ]);
    assert.deepEqual(grouped.get("1").map((c) => c.claim_id), ["a", "c"]);
    assert.deepEqual(grouped.get("0").map((c) => c.claim_id), ["b"]);
  });

  test("only an explicit true marks evidence stale", () => {
    assert.equal(isStale({ kb_changed_since_judged: true }), true);
    assert.equal(isStale({ kb_changed_since_judged: false }), false);
    assert.equal(isStale({ kb_changed_since_judged: null }), false);
  });

  test("tags read in plain words, unknown tags pass through", () => {
    assert.equal(tagLabel("kb_gap"), "Not in the KB");
    assert.equal(tagLabel("retrieval_miss"), "Search missed it");
    assert.equal(tagLabel("other"), "other");
  });
});

describe("routes", () => {
  test("empty and unknown hashes show the submit view", () => {
    for (const hash of ["", "#", "#/", "#/nope"]) {
      assert.deepEqual(parseRoute(hash), { view: "submit" }, hash);
    }
  });

  test("the conversation list and its trailing slash show the list", () => {
    assert.deepEqual(parseRoute("#/conversations"), { view: "list" });
    assert.deepEqual(parseRoute("#/conversations/"), { view: "list" });
  });

  test("a session id containing # survives a round trip", () => {
    const id = "api-submission.json#0";
    assert.deepEqual(parseRoute(detailRoute(id)), { view: "detail", id });
  });

  test("a malformed escape falls back to the list", () => {
    assert.deepEqual(parseRoute("#/conversations/%E0%A4%A"), { view: "list" });
  });
});

describe("orderConversations", () => {
  test("worst first by contradicted, not found, then unjudged; rejected last", () => {
    const rows = [
      { session_id: "rejected", status: "rejected", contradicted_turns: null },
      { session_id: "clean", status: "accepted", contradicted_turns: 0, not_found_turns: 0, unjudged_turns: 0 },
      { session_id: "gap", status: "accepted", contradicted_turns: 0, not_found_turns: 2, unjudged_turns: 0 },
      { session_id: "partial", status: "accepted", contradicted_turns: 0, not_found_turns: 2, unjudged_turns: 1 },
      { session_id: "wrong", status: "accepted", contradicted_turns: 1, not_found_turns: 0, unjudged_turns: 0 },
    ];
    assert.deepEqual(orderConversations(rows).map((r) => r.session_id), ["wrong", "partial", "gap", "clean", "rejected"]);
  });
});

describe("errorDetail", () => {
  test("reads string, validation list, and object details", () => {
    assert.equal(errorDetail({ detail: "index is empty" }, 503), "index is empty");
    assert.equal(errorDetail({ detail: [{ msg: "a" }, { msg: "b" }] }, 422), "a b");
    assert.equal(
      errorDetail({ detail: { session_id: "s1", error: "raw_pairs is empty" } }, 422),
      "Session s1: raw_pairs is empty",
    );
  });

  test("falls back to the status when there is no detail", () => {
    assert.equal(errorDetail(null, 502, "Bad Gateway"), "The evaluator returned 502 Bad Gateway.");
    assert.equal(errorDetail({}, 500), "The evaluator returned 500.");
  });
});
