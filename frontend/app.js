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

const byId = (id) => document.getElementById(id);

const fileInput = byId("transcript-file");
const textarea = byId("transcript-json");
const evaluateButton = byId("evaluate-button");
const submissionPanel = byId("submission-panel");
const jsonMessage = byId("json-message");
const loadingState = byId("loading-state");
const errorPanel = byId("error-panel");
const errorTitle = byId("error-title");
const errorMessage = byId("error-message");
const errorLink = byId("error-link");
const stubWarning = byId("stub-warning");
const navLinks = document.querySelectorAll("[data-nav]");

const listStatus = byId("list-status");
const listBody = byId("conversation-rows");
const refreshButton = byId("refresh-conversations");

const resultSession = byId("result-session");
const resultNote = byId("result-note");
const summaryGrid = byId("summary-grid");
const turnOrder = byId("turn-order");
const turnFilter = byId("turn-filter");
const turnList = byId("turn-list");

const VIEWS = { submit: byId("submit-view"), list: byId("list-view"), detail: byId("results") };

// Verdicts that are findings, each with its own callout. UNJUDGED is not one of
// them: it is a gap in this run and gets the "incomplete" callout instead.
const FINDINGS = new Set(["CONTRADICTED", "NOT_FOUND"]);
const OPEN_CONVERSATIONS = { href: "#/conversations", text: "Open Conversations" };
const BACK_TO_LIST = { href: "#/conversations", text: "Back to Conversations" };

// The result on screen, kept so the order/filter controls re-render without a fetch.
let shownResult = null;
// The last POST response, used once for the navigation to its own detail route.
let lastSubmission = null;
// Bumped on every navigation. A slow fetch that lands after the user moved on
// sees a newer token and drops its result instead of overwriting the new view.
let navToken = 0;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function displayValue(value, fallback = "Not available") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function messageOf(error) {
  return error instanceof Error ? error.message : "An unexpected error occurred.";
}

function badge(info) {
  return element("span", `state-badge badge-${info.tone}`, info.label);
}

function setBusy(busy) {
  fileInput.disabled = busy;
  textarea.disabled = busy;
  evaluateButton.disabled = busy;
  submissionPanel.setAttribute("aria-busy", String(busy));
  loadingState.hidden = !busy;
}

function showJsonError(message) {
  jsonMessage.textContent = message;
  jsonMessage.hidden = false;
  textarea.setAttribute("aria-invalid", "true");
}

function clearJsonError() {
  jsonMessage.textContent = "";
  jsonMessage.hidden = true;
  textarea.removeAttribute("aria-invalid");
}

function showRequestError(title, message, link) {
  errorTitle.textContent = title;
  errorMessage.textContent = message;
  errorLink.hidden = !link;
  if (link) {
    errorLink.href = link.href;
    errorLink.textContent = link.text;
  }
  errorPanel.hidden = false;
}

function hideRequestError() {
  errorPanel.hidden = true;
}

function parseTranscript() {
  clearJsonError();
  try {
    return JSON.parse(textarea.value);
  } catch (error) {
    showJsonError(`JSON syntax error: ${error.message}`);
    return null;
  }
}

async function responseError(response) {
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  return errorDetail(body, response.status, response.statusText);
}

// --- results ------------------------------------------------------------------

function summaryItem(label, value, alert = false) {
  const item = element("div", `summary-item${alert ? " is-alert" : ""}`);
  item.append(element("span", "summary-label", label));
  item.append(element("strong", "summary-value", value));
  return item;
}

function renderSummary(summary) {
  resultSession.textContent = displayValue(summary.session_id, "Unknown session");
  const unjudged = toCount(summary.unjudged_turns);
  summaryGrid.replaceChildren(
    summaryItem("Turns", toCount(summary.turn_count)),
    summaryItem("Contradicted", toCount(summary.contradicted_turns)),
    summaryItem("Not found", toCount(summary.not_found_turns)),
    summaryItem("Supported", toCount(summary.supported_turns)),
    summaryItem("Not judged", unjudged, unjudged > 0),
    summaryItem("Skipped", toCount(summary.skipped_turns)),
  );
  const rejected = Boolean(summary.status) && summary.status !== "accepted";
  resultNote.hidden = !rejected;
  resultNote.textContent = rejected
    ? `This transcript was rejected at ingest: ${displayValue(summary.note, "no reason recorded")}`
    : "";
}

function detailRow(label, value, fallback) {
  const row = element("div", "detail-row");
  row.append(element("dt", "detail-label", label));
  row.append(element("dd", "detail-value", displayValue(value, fallback)));
  return row;
}

function renderClaim(claim, claimNumber) {
  const state = claimState(claim);
  const card = element("article", "claim-card");
  const heading = element("div", "claim-heading");
  heading.append(element("h4", "claim-number", `Claim ${claimNumber}`), badge(stateInfo(state)));
  card.append(heading);
  card.append(element("p", "claim-text", displayValue(claim.claim_text, "No claim text returned")));

  const details = element("dl", "claim-details");
  details.append(detailRow("Subject", claim.subject_tag, "Not specified"));

  if (state === "UNJUDGED") {
    // The judge never answered, so there is no rationale or evidence to show.
    // "No rationale returned" rows would misreport that as an empty answer.
    card.append(
      element("p", "callout callout-unjudged", "The judge could not evaluate this claim. Re-run the evaluation."),
      details,
    );
    return card;
  }

  if (isStale(claim)) {
    card.append(
      element(
        "p",
        "stale-notice",
        "The knowledge base changed after this verdict, so the evidence shown may not be what was judged. Re-run to refresh.",
      ),
    );
  }
  if (claim.tag) details.append(detailRow("Tag", tagLabel(claim.tag)));
  details.append(detailRow("Why", claim.rationale, "No rationale returned"));
  details.append(detailRow("Evidence", claim.evidence_text, "No evidence returned"));
  details.append(detailRow("Source", claim.evidence_source_doc, "No source document returned"));
  if (claim.evidence_section) details.append(detailRow("Section", claim.evidence_section));
  if (claim.evidence_chunk_id) details.append(detailRow("Chunk", claim.evidence_chunk_id));
  if (claim.evidence_doc_version_date) {
    details.append(detailRow("Document version", claim.evidence_doc_version_date));
  }
  if (claim.attempts !== null && claim.attempts !== undefined) {
    details.append(detailRow("Judge attempts", claim.attempts));
  }
  card.append(details);
  return card;
}

function renderTurn(turn, claims) {
  const info = stateInfo(turn.turn_verdict);
  const incomplete = isIncomplete(turn);
  const tone = incomplete && !FINDINGS.has(turn.turn_verdict) ? "unjudged" : info.tone;

  // A native <details>: opening and closing needs no JS, and turns that need
  // review start open while everything else stays collapsed.
  const card = element("details", `turn-card turn-${tone}`);
  card.open = needsReview(turn);

  const heading = element("summary", "turn-heading");
  const titleGroup = element("div");
  titleGroup.append(element("p", "turn-label", `Turn ${toCount(turn.pair_index) + 1}`));
  titleGroup.append(element("span", "turn-index", `pair_index ${displayValue(turn.pair_index, "unknown")}`));
  heading.append(titleGroup, badge(info));
  card.append(heading);

  if (FINDINGS.has(turn.turn_verdict)) {
    card.append(element("p", `callout callout-${info.tone}`, info.description));
  }
  if (incomplete) {
    const count = toCount(turn.unjudged_count);
    card.append(
      element(
        "p",
        "callout callout-unjudged",
        `${count} claim${count === 1 ? "" : "s"} not judged, so this turn's result is incomplete. Re-run the evaluation.`,
      ),
    );
  }

  const exchange = element("div", "exchange-grid");
  const caller = element("section", "speech-block");
  caller.append(element("h3", null, "Caller"));
  caller.append(element("p", null, displayValue(turn.caller_asked, "No caller text returned")));
  const ava = element("section", "speech-block speech-ava");
  ava.append(element("h3", null, "AVA"));
  ava.append(element("p", null, displayValue(turn.agent_said, "No AVA response returned")));
  exchange.append(caller, ava);
  card.append(exchange);

  const line = element("div", "evaluation-line");
  if (!FINDINGS.has(turn.turn_verdict) && !incomplete) {
    line.append(element("span", null, info.description));
  }
  line.append(
    element(
      "span",
      "count-note",
      `${toCount(turn.claim_count)} claim(s) · ${toCount(turn.contradicted_count)} contradicted · ` +
        `${toCount(turn.not_found_count)} not found · ${toCount(turn.supported_count)} supported · ` +
        `${toCount(turn.unjudged_count)} not judged`,
    ),
  );
  card.append(line);

  const claimSection = element("section", "claims-section");
  claimSection.append(element("h3", null, `Extracted claims (${claims.length})`));
  if (!claims.length) {
    claimSection.append(element("p", "empty-note", "No claims returned for this turn."));
  } else {
    claims.forEach((claim, index) => claimSection.append(renderClaim(claim, index + 1)));
  }
  card.append(claimSection);
  return card;
}

function renderTurns() {
  if (!shownResult) return;
  const claimsByTurn = groupClaimsByTurn(shownResult.claims);
  const turns = filterTurns(orderTurns(shownResult.turns, turnOrder.value), turnFilter.value);
  turnList.replaceChildren(
    ...turns.map((turn) => renderTurn(turn, claimsByTurn.get(String(turn.pair_index)) ?? [])),
  );
  if (!turns.length) {
    const message = shownResult.turns.length ? "No turns need review." : "No turn results were returned.";
    turnList.append(element("p", "empty-note panel", message));
  }
}

function renderResults(data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new Error("The evaluator returned an unexpected response.");
  }
  if (!data.summary || !Array.isArray(data.turns) || !Array.isArray(data.claims)) {
    throw new Error("The evaluator response is missing summary, turn, or claim data.");
  }
  shownResult = data;
  renderSummary(data.summary);
  renderTurns();
}

async function showConversation(id, token) {
  if (lastSubmission && lastSubmission.sessionId === id) {
    // Just evaluated: render the POST response instead of fetching it straight
    // back. Used once, so a later visit or reload fetches fresh rows.
    const { data } = lastSubmission;
    lastSubmission = null;
    renderResults(data);
    return;
  }

  shownResult = null;
  resultSession.textContent = id;
  resultNote.hidden = true;
  summaryGrid.replaceChildren();
  turnList.replaceChildren(element("p", "empty-note panel", "Loading conversation…"));

  const response = await fetch("/api/conversations/" + encodeURIComponent(id), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(await responseError(response));
  const data = await response.json();
  if (token === navToken) renderResults(data);
}

// --- conversation list --------------------------------------------------------

function renderConversationRow(row) {
  const accepted = row.status === "accepted";
  const tr = element("tr", accepted ? null : "is-rejected");

  const session = element("td", "session-cell");
  if (accepted) {
    const link = element("a", null, row.session_id);
    link.href = detailRoute(row.session_id);
    session.append(link);
  } else {
    session.append(element("span", null, displayValue(row.session_id, "Unknown session")));
    session.append(element("span", "rejected-note", `Rejected: ${displayValue(row.note, "no reason recorded")}`));
  }
  tr.append(session);

  const counts = [
    [row.turn_count, false],
    [row.contradicted_turns, false],
    [row.not_found_turns, false],
    [row.supported_turns, false],
    [row.unjudged_turns, true],
    [row.skipped_turns, false],
  ];
  for (const [value, alertWhenPositive] of counts) {
    const cell = element("td", "count-cell", accepted ? toCount(value) : "—");
    if (accepted && alertWhenPositive && toCount(value) > 0) cell.classList.add("is-alert");
    tr.append(cell);
  }
  return tr;
}

async function showConversations(token) {
  listStatus.hidden = false;
  listStatus.textContent = "Loading conversations…";
  listBody.replaceChildren();

  const response = await fetch("/api/conversations", { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(await responseError(response));
  const rows = await response.json();
  if (token !== navToken) return;
  if (!Array.isArray(rows)) throw new Error("The evaluator returned an unexpected response.");

  listBody.replaceChildren(...orderConversations(rows).map(renderConversationRow));
  listStatus.hidden = rows.length > 0;
  listStatus.textContent = rows.length ? "" : "No conversations yet. Submit a transcript to evaluate one.";
}

// --- routing ------------------------------------------------------------------

async function route() {
  const token = ++navToken;
  const target = parseRoute(window.location.hash);
  const navView = target.view === "detail" ? "list" : target.view;

  hideRequestError();
  for (const [name, view] of Object.entries(VIEWS)) view.hidden = name !== target.view;
  navLinks.forEach((link) => {
    if (link.dataset.nav === navView) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });

  try {
    if (target.view === "list") await showConversations(token);
    else if (target.view === "detail") await showConversation(target.id, token);
  } catch (error) {
    if (token !== navToken) return;
    if (target.view === "list") {
      listStatus.textContent = "Conversations could not be loaded.";
      showRequestError("Could not load conversations", messageOf(error));
    } else {
      turnList.replaceChildren();
      showRequestError("Could not load conversation", messageOf(error), BACK_TO_LIST);
    }
  }
}

// --- submission ---------------------------------------------------------------

async function evaluateTranscript() {
  const payload = parseTranscript();
  if (payload === null) return;

  hideRequestError();
  setBusy(true);

  try {
    const response = await fetch("/api/evaluations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (response.status === 504) {
      // nginx gave up waiting, but the pipeline runs on in the API and saves its
      // results, so the conversation list is where they will appear.
      showRequestError(
        "Evaluation is still running",
        "The evaluation took longer than the connection allows. It may still finish, so check Conversations in a minute.",
        OPEN_CONVERSATIONS,
      );
      return;
    }
    if (!response.ok) {
      const titles = {
        422: "Transcript validation failed",
        500: "Evaluation failed",
        503: "Evaluator unavailable",
      };
      showRequestError(titles[response.status] || "Evaluation request failed", await responseError(response));
      return;
    }

    const data = await response.json();
    const sessionId = data?.session_id ?? data?.summary?.session_id;
    if (!sessionId) throw new Error("The evaluator response is missing its session id.");
    lastSubmission = { sessionId, data };

    // Navigate to the result's own URL so it can be reloaded or shared. Setting the
    // same hash again fires no hashchange, so route directly in that case.
    const hash = detailRoute(sessionId);
    if (window.location.hash === hash) await route();
    else window.location.hash = hash;
  } catch (error) {
    showRequestError(
      "Could not reach the evaluator",
      `${messageOf(error)} If an evaluation was running, it may still finish, so check Conversations.`,
      OPEN_CONVERSATIONS,
    );
  } finally {
    setBusy(false);
  }
}

function loadSelectedFile() {
  const file = fileInput.files && fileInput.files[0];
  if (!file) return;

  const reader = new FileReader();
  reader.addEventListener("load", () => {
    textarea.value = typeof reader.result === "string" ? reader.result : "";
    parseTranscript();
  });
  reader.addEventListener("error", () => {
    showJsonError("The selected file could not be read.");
  });
  reader.readAsText(file);
}

async function loadHealth() {
  try {
    const response = await fetch("/api/health", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("Health check failed");
    const health = await response.json();
    stubWarning.hidden = !(health.stub_llm === true || health.stub_embed === true);
  } catch {
    stubWarning.hidden = true;
  }
}

fileInput.addEventListener("change", loadSelectedFile);
textarea.addEventListener("input", clearJsonError);
evaluateButton.addEventListener("click", evaluateTranscript);
turnOrder.addEventListener("change", renderTurns);
turnFilter.addEventListener("change", renderTurns);
refreshButton.addEventListener("click", route);
window.addEventListener("hashchange", route);

loadHealth();
route();
