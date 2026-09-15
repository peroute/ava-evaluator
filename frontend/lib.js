// Pure logic for the AVA evaluation page: state labels, ordering, filtering and
// routing. Nothing here touches the DOM, so `node --test` can check it without a
// browser. app.js imports it and does all the rendering.

const NEUTRAL = "neutral";

// One entry per label the result views produce (claim verdicts and turn labels
// share names). `rank` orders turns worst-first. UNJUDGED sits between the real
// flags and SUPPORTED: an incomplete turn needs a look, but it is a gap in this
// run, not a finding about Ava.
export const STATES = Object.freeze({
  CONTRADICTED: {
    label: "Contradicted",
    tone: "contradicted",
    rank: 5,
    description: "Ava said something the knowledge base contradicts.",
  },
  NOT_FOUND: {
    label: "Not found in KB",
    tone: "not_found",
    rank: 4,
    description: "The knowledge base couldn't confirm this, which is likely a content gap.",
  },
  UNJUDGED: {
    label: "Not judged",
    tone: "unjudged",
    rank: 3,
    description: "The judge could not evaluate this. Re-run the evaluation.",
  },
  SUPPORTED: {
    label: "Supported",
    tone: "supported",
    rank: 2,
    description: "Every judged claim is grounded in the knowledge base.",
  },
  NO_CLAIMS: {
    label: "No claims extracted",
    tone: NEUTRAL,
    rank: 1,
    description: "The turn was flagged, but no checkable claim was extracted.",
  },
  NOT_EVALUATED: {
    label: "Not evaluated",
    tone: NEUTRAL,
    rank: 1,
    description: "No checkable factual claim was identified in this turn.",
  },
  UNCLASSIFIED: {
    label: "Unclassified",
    tone: NEUTRAL,
    rank: 1,
    description: "This turn has not been classified yet.",
  },
});

const FLAGGED_TURNS = new Set(["CONTRADICTED", "NOT_FOUND", "UNJUDGED"]);
const TAG_LABELS = Object.freeze({ kb_gap: "Not in the KB", retrieval_miss: "Search missed it" });
const LIST_PATH = "conversations";

/** A count from the API as a number; null (SUM over no rows) reads as 0. */
export function toCount(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

/** Display metadata for a label, with a neutral fallback for anything unknown. */
export function stateInfo(value) {
  if (typeof value === "string" && Object.hasOwn(STATES, value)) return STATES[value];
  return {
    label: value ? String(value) : "Unknown state",
    tone: NEUTRAL,
    rank: 0,
    description: "The evaluator returned a state this page does not recognise.",
  };
}

/**
 * A claim's state. A claim step 6 failed on has a claims row but no verdicts row,
 * so the view returns `verdict: null` -- that means "never judged", not "judged
 * as nothing".
 */
export function claimState(claim) {
  return claim.verdict ?? "UNJUDGED";
}

/** True when at least one of the turn's claims was never judged. */
export function isIncomplete(turn) {
  return toCount(turn.unjudged_count) > 0;
}

/** Contradicted, not found, or incomplete: the turns a reviewer must open. */
export function needsReview(turn) {
  return FLAGGED_TURNS.has(turn.turn_verdict) || isIncomplete(turn);
}

/**
 * Sort key for "worst first". A SUPPORTED turn with a failed claim ranks with
 * UNJUDGED, so it is not buried among fully grounded turns.
 */
export function turnRank(turn) {
  const rank = stateInfo(turn.turn_verdict).rank;
  return isIncomplete(turn) ? Math.max(rank, STATES.UNJUDGED.rank) : rank;
}

/** Claims keyed by String(pair_index), keeping their original order. */
export function groupClaimsByTurn(claims) {
  const byTurn = new Map();
  for (const claim of claims) {
    const key = String(claim.pair_index);
    if (!byTurn.has(key)) byTurn.set(key, []);
    byTurn.get(key).push(claim);
  }
  return byTurn;
}

/** A sorted copy: "worst" (rank, then transcript order) or "transcript". */
export function orderTurns(turns, order = "worst") {
  const byIndex = (a, b) => toCount(a.pair_index) - toCount(b.pair_index);
  const copy = [...turns];
  if (order === "transcript") return copy.sort(byIndex);
  return copy.sort((a, b) => turnRank(b) - turnRank(a) || byIndex(a, b));
}

/** A filtered copy: "review" keeps only turns that need review. */
export function filterTurns(turns, show = "all") {
  return show === "review" ? turns.filter(needsReview) : [...turns];
}

/** The KB was re-indexed after this verdict, so its evidence may have moved. */
export function isStale(claim) {
  return claim.kb_changed_since_judged === true;
}

/** A verdict tag in plain words. */
export function tagLabel(tag) {
  return Object.hasOwn(TAG_LABELS, tag) ? TAG_LABELS[tag] : String(tag);
}

/** Map a location hash to a view: submit, list, or detail with a session id. */
export function parseRoute(hash) {
  const path = String(hash ?? "").replace(/^#\/?/, "");
  if (path === LIST_PATH) return { view: "list" };
  if (path.startsWith(`${LIST_PATH}/`)) {
    try {
      const id = decodeURIComponent(path.slice(LIST_PATH.length + 1));
      if (id) return { view: "detail", id };
    } catch {
      // A malformed escape cannot name a session; fall back to the list.
    }
    return { view: "list" };
  }
  return { view: "submit" };
}

/** The hash for one conversation. Ids can contain "#", so they are encoded. */
export function detailRoute(sessionId) {
  return `#/${LIST_PATH}/${encodeURIComponent(sessionId)}`;
}

/** Accepted conversations worst-first, rejected ones last. */
export function orderConversations(rows) {
  const rejected = (row) => Number(row.status !== "accepted");
  return [...rows].sort(
    (a, b) =>
      rejected(a) - rejected(b) ||
      toCount(b.contradicted_turns) - toCount(a.contradicted_turns) ||
      toCount(b.not_found_turns) - toCount(a.not_found_turns) ||
      toCount(b.unjudged_turns) - toCount(a.unjudged_turns) ||
      String(a.session_id).localeCompare(String(b.session_id)),
  );
}

/** A readable message from a FastAPI error body (string, validation list, or object). */
export function errorDetail(body, status, statusText = "") {
  const fallback = `${`The evaluator returned ${status} ${statusText}`.trim()}.`;
  if (!body || body.detail === undefined || body.detail === null) return fallback;
  const { detail } = body;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => item?.msg || "Invalid request").join(" ");
  }
  if (typeof detail === "object") {
    const session = detail.session_id ? `Session ${detail.session_id}: ` : "";
    return `${session}${detail.error || "The request was rejected."}`;
  }
  return "The evaluator rejected the request.";
}
