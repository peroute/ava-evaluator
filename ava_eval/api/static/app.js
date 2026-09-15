// Ava Evaluator dashboard: plain JavaScript, no framework, no build step.
//
// Two views, chosen by the URL hash:
//   #/                          conversations list  (GET /health, GET /conversations)
//   #/conversations/<session>   one conversation    (GET /conversations/{session_id})
//
// Only GET requests are made. Every value from the API is written with
// textContent, never innerHTML, so transcript text cannot inject markup.

"use strict";

const view = document.getElementById("view");
const stubBanner = document.getElementById("stub-banner");

// Bumped on every navigation, so a slow response for a page the user has
// already left is ignored instead of overwriting the current page.
let renderToken = 0;

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = body.detail;
    } catch {
      // not JSON; keep the status line
    }
    throw new Error(detail);
  }
  return response.json();
}

// el("td", { className: "num" }, "5") -> <td class="num">5</td>
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

function badge(text, variant) {
  return el("span", { className: `badge badge-${variant}` }, text);
}

function show(...nodes) {
  view.replaceChildren(...nodes);
}

function loadingState(message) {
  return el("div", { className: "state muted" }, message);
}

function emptyState(message) {
  return el("div", { className: "state muted" }, message);
}

function errorState(message, withBack) {
  return el(
    "div",
    { className: "state state-error", role: "alert" },
    el("strong", {}, "Could not load data. "),
    message,
    withBack ? el("div", {}, el("a", { href: "#/", className: "back" }, "← Back to conversations")) : null,
  );
}

// The API returns timestamps already in the display time zone, e.g.
// "2026-09-13T13:48:01-05:00". Show them as-is rather than re-converting to
// the browser's zone, so the dashboard matches the report and pgAdmin.
function formatTimestamp(value) {
  if (!value) return "—";
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?$/.exec(value);
  if (!match) return value;
  const zone = match[3] ? ` (UTC${match[3] === "Z" ? "" : match[3]})` : "";
  return `${match[1]} ${match[2]}${zone}`;
}

function dash(value) {
  return value === null || value === undefined || value === "" ? "—" : value;
}

// ---------------------------------------------------------------------------
// Stub-mode banner
// ---------------------------------------------------------------------------

async function loadHealthBanner() {
  try {
    const health = await getJson("/health");
    const stubbed = [];
    if (health.stub_llm) stubbed.push("language model (AVA_STUB_LLM)");
    if (health.stub_embed) stubbed.push("embeddings (AVA_STUB_EMBED)");
    if (stubbed.length) {
      stubBanner.replaceChildren(
        el("strong", {}, "Offline stub mode: "),
        `the ${stubbed.join(" and ")} ${stubbed.length > 1 ? "are" : "is"} stubbed. `,
        "Classifications and verdicts shown here are placeholder output, not real model results.",
      );
      stubBanner.hidden = false;
    } else {
      stubBanner.hidden = true;
    }
  } catch (error) {
    stubBanner.replaceChildren(
      el("strong", {}, "Health check failed: "),
      `${error.message}. Stub mode could not be determined.`,
    );
    stubBanner.hidden = false;
  }
}

// ---------------------------------------------------------------------------
// View 1: conversations
// ---------------------------------------------------------------------------

async function renderConversations(token) {
  document.title = "Conversations · Ava Evaluator";
  show(loadingState("Loading conversations…"));

  let conversations;
  try {
    conversations = await getJson("/conversations");
  } catch (error) {
    if (token === renderToken) show(errorState(error.message, false));
    return;
  }
  if (token !== renderToken) return;

  const heading = el(
    "div",
    {},
    el("h1", {}, "Conversations"),
    el("p", { className: "muted" }, `${conversations.length} ingested conversation(s). Select one to see its turns and claims.`),
  );

  if (conversations.length === 0) {
    show(heading, emptyState("No conversations yet. Ingest transcripts with: python -m ava_eval ingest"));
    return;
  }

  const rows = conversations.map((c) => {
    const detailHash = `#/conversations/${encodeURIComponent(c.session_id)}`;
    const sessionCell = el("td", {}, el("a", { href: detailHash }, c.session_id));
    if (c.status === "rejected" && c.note) {
      sessionCell.append(el("div", { className: "note" }, c.note));
    }

    const row = el(
      "tr",
      { className: "clickable", tabIndex: 0, title: `Open ${c.session_id}` },
      sessionCell,
      el("td", {}, badge(c.status, c.status === "accepted" || c.status === "rejected" ? c.status : "neutral")),
      el("td", {}, dash(c.source_file)),
      el("td", { className: "nowrap" }, formatTimestamp(c.ingested_at)),
      el("td", { className: "num" }, c.turn_count),
      el("td", { className: "num" }, c.contradicted_turns),
      el("td", { className: "num" }, c.not_found_turns),
      el("td", { className: "num" }, c.supported_turns),
    );
    row.addEventListener("click", (event) => {
      if (event.target.closest("a")) return; // the link navigates by itself
      location.hash = detailHash;
    });
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter") location.hash = detailHash;
    });
    return row;
  });

  const table = el(
    "div",
    { className: "table-wrap" },
    el(
      "table",
      {},
      el(
        "thead",
        {},
        el(
          "tr",
          {},
          el("th", {}, "Session"),
          el("th", {}, "Status"),
          el("th", {}, "Source file"),
          el("th", {}, "Ingested"),
          el("th", { className: "num", title: "Turns stored for this conversation" }, "Turns"),
          el("th", { className: "num", title: "Turns whose worst verdict is CONTRADICTED" }, "Contradicted"),
          el("th", { className: "num", title: "Turns whose worst verdict is NOT_FOUND" }, "Not found"),
          el("th", { className: "num", title: "Turns whose claims were all SUPPORTED" }, "Supported"),
        ),
      ),
      el("tbody", {}, ...rows),
    ),
  );

  show(heading, table);
}

// ---------------------------------------------------------------------------
// View 2: one conversation
// ---------------------------------------------------------------------------

const VERDICTS = new Set(["SUPPORTED", "CONTRADICTED", "NOT_FOUND"]);

// Step 3 state for a turn. turn_verdict is checked first because
// contains_claims alone is false both for filler AND for turns that were
// never classified (the view defaults unclassified turns to false).
function step3State(turn) {
  switch (turn.turn_verdict) {
    case "UNCLASSIFIED":
      return { label: "Not classified", variant: "neutral" };
    case "NOT_EVALUATED":
      return { label: "Filler / no checkable claim", variant: "neutral" };
    case "NO_CLAIMS":
      return { label: "Checkable claim (none extracted)", variant: "neutral" };
    default:
      if (VERDICTS.has(turn.turn_verdict) || turn.contains_claims === true) {
        return { label: "Checkable claim", variant: "neutral" };
      }
      return { label: dash(turn.turn_verdict), variant: "neutral" };
  }
}

function turnResult(turn) {
  if (!VERDICTS.has(turn.turn_verdict)) return "—";
  const claims = `${turn.claim_count} claim${turn.claim_count === 1 ? "" : "s"}`;
  return el("span", {}, badge(turn.turn_verdict, turn.turn_verdict), " ", el("span", { className: "muted" }, claims));
}

function summaryBlock(summary) {
  // Each label/value pair is wrapped in its own <div> so the grid lays out
  // pairs, not loose labels and values.
  const item = (label, value) => el("div", {}, el("dt", {}, label), el("dd", {}, value));
  const list = el(
    "dl",
    { className: "summary" },
    item("Status", badge(summary.status, summary.status === "accepted" || summary.status === "rejected" ? summary.status : "neutral")),
    item("Source file", dash(summary.source_file)),
    item("Ingested", formatTimestamp(summary.ingested_at)),
    item("Turns", summary.turn_count),
    item("Contradicted turns", summary.contradicted_turns),
    item("Not found turns", summary.not_found_turns),
    item("Supported turns", summary.supported_turns),
    item("Skipped turns", summary.skipped_turns),
  );
  if (summary.note) {
    const note = item("Rejection note", summary.note);
    note.className = "summary-wide";
    list.append(note);
  }
  return list;
}

function turnsTable(turns) {
  const rows = turns.map((t) => {
    const state = step3State(t);
    return el(
      "tr",
      {},
      el("td", { className: "num" }, t.pair_index),
      el("td", { className: "text" }, t.caller_asked),
      el("td", { className: "text" }, t.agent_said),
      el("td", {}, badge(state.label, state.variant)),
      el("td", { className: "nowrap" }, turnResult(t)),
    );
  });
  return el(
    "div",
    { className: "table-wrap" },
    el(
      "table",
      {},
      el(
        "thead",
        {},
        el(
          "tr",
          {},
          el("th", { className: "num" }, "#"),
          el("th", {}, "Caller asked"),
          el("th", {}, "Agent said"),
          el("th", {}, "Step 3"),
          el("th", {}, "Turn result"),
        ),
      ),
      el("tbody", {}, ...rows),
    ),
  );
}

function evidenceCell(claim) {
  const cell = el("td", {});
  const source = [claim.evidence_source_doc, claim.evidence_section].filter(Boolean).join(" › ");
  if (source) cell.append(el("div", {}, source));
  if (claim.evidence_doc_version_date) {
    cell.append(el("div", { className: "muted" }, `Version ${claim.evidence_doc_version_date}`));
  }
  if (claim.rationale) cell.append(el("div", { className: "muted" }, claim.rationale));
  if (claim.evidence_text) {
    cell.append(
      el("details", {}, el("summary", {}, "Evidence text"), el("p", { className: "evidence" }, claim.evidence_text)),
    );
  }
  if (!cell.childNodes.length) cell.append("—");
  return cell;
}

function claimsTable(claims) {
  const rows = claims.map((c) =>
    el(
      "tr",
      {},
      el("td", { className: "num" }, c.pair_index),
      el("td", {}, dash(c.subject_tag)),
      el("td", { className: "text" }, c.claim_text),
      el("td", {}, c.verdict ? badge(c.verdict, c.verdict) : badge("Not judged", "neutral")),
      el("td", {}, dash(c.tag)),
      el("td", { className: "num" }, dash(c.attempts)),
      evidenceCell(c),
    ),
  );
  return el(
    "div",
    { className: "table-wrap" },
    el(
      "table",
      {},
      el(
        "thead",
        {},
        el(
          "tr",
          {},
          el("th", { className: "num", title: "Turn the claim came from" }, "Turn"),
          el("th", {}, "Subject"),
          el("th", {}, "Claim"),
          el("th", {}, "Verdict"),
          el("th", {}, "Tag"),
          el("th", { className: "num", title: "Judge attempts" }, "Attempts"),
          el("th", {}, "Evidence"),
        ),
      ),
      el("tbody", {}, ...rows),
    ),
  );
}

async function renderConversation(sessionId, token) {
  document.title = `${sessionId} · Ava Evaluator`;
  const back = el("a", { href: "#/", className: "back" }, "← Back to conversations");
  show(back, loadingState(`Loading ${sessionId}…`));

  let data;
  try {
    data = await getJson(`/conversations/${encodeURIComponent(sessionId)}`);
  } catch (error) {
    if (token === renderToken) show(back, errorState(error.message, false));
    return;
  }
  if (token !== renderToken) return;

  const { summary, turns, claims } = data;
  const parts = [back, el("h1", {}, summary.session_id), summaryBlock(summary)];

  parts.push(el("h2", {}, `Turns (${turns.length})`));
  if (turns.length) {
    parts.push(turnsTable(turns));
  } else if (summary.status === "rejected") {
    parts.push(emptyState("This conversation was rejected at ingest, so it has no turns. See the rejection note above."));
  } else {
    parts.push(emptyState("No turns stored for this conversation."));
  }

  parts.push(el("h2", {}, `Claims (${claims.length})`));
  parts.push(
    claims.length
      ? claimsTable(claims)
      : emptyState("No claims for this conversation. Claims appear after python -m ava_eval evaluate extracts them."),
  );

  show(...parts);
}

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------

function route() {
  const token = ++renderToken;
  const match = /^#\/conversations\/(.+)$/.exec(location.hash);
  if (match) {
    let sessionId;
    try {
      sessionId = decodeURIComponent(match[1]);
    } catch {
      show(errorState("That conversation link is malformed.", true));
      return;
    }
    renderConversation(sessionId, token);
  } else {
    renderConversations(token);
  }
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", route);
loadHealthBanner();
route();
