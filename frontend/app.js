"use strict";

const fileInput = document.getElementById("transcript-file");
const textarea = document.getElementById("transcript-json");
const evaluateButton = document.getElementById("evaluate-button");
const submissionPanel = document.getElementById("submission-panel");
const jsonMessage = document.getElementById("json-message");
const loadingState = document.getElementById("loading-state");
const errorPanel = document.getElementById("error-panel");
const errorTitle = document.getElementById("error-title");
const errorMessage = document.getElementById("error-message");
const results = document.getElementById("results");
const resultSession = document.getElementById("result-session");
const summaryGrid = document.getElementById("summary-grid");
const turnList = document.getElementById("turn-list");
const stubWarning = document.getElementById("stub-warning");

const KNOWN_STATES = new Set([
  "CONTRADICTED",
  "NOT_FOUND",
  "SUPPORTED",
  "NOT_EVALUATED",
  "UNCLASSIFIED",
  "NO_CLAIMS",
]);

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function displayValue(value, fallback = "Not available") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function stateLabel(value) {
  const labels = {
    CONTRADICTED: "Contradicted",
    NOT_FOUND: "Not found in KB",
    SUPPORTED: "Supported",
    NOT_EVALUATED: "Not evaluated",
    UNCLASSIFIED: "Unclassified",
    NO_CLAIMS: "No claims extracted",
  };
  return labels[value] || displayValue(value, "Unknown state");
}

function stateBadge(value) {
  const state = KNOWN_STATES.has(value) ? value : "UNKNOWN";
  return element("span", `state-badge state-${state.toLowerCase()}`, stateLabel(value));
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

function showRequestError(title, message) {
  errorTitle.textContent = title;
  errorMessage.textContent = message;
  errorPanel.hidden = false;
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

function errorDetail(body, response) {
  if (!body || body.detail === undefined) {
    return `The evaluator returned ${response.status} ${response.statusText}.`;
  }
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) {
    return body.detail
      .map((item) => displayValue(item.msg, "Invalid request"))
      .join(" ");
  }
  if (typeof body.detail === "object") {
    const session = body.detail.session_id ? `Session ${body.detail.session_id}: ` : "";
    return `${session}${displayValue(body.detail.error, "The request was rejected.")}`;
  }
  return "The evaluator rejected the request.";
}

async function responseError(response) {
  let body = null;
  try {
    body = await response.json();
  } catch {
    return `The evaluator returned ${response.status} ${response.statusText}.`;
  }
  return errorDetail(body, response);
}

function summaryItem(label, value) {
  const item = element("div", "summary-item");
  item.append(element("span", "summary-label", label));
  item.append(element("strong", "summary-value", displayValue(value, "0")));
  return item;
}

function renderSummary(data) {
  const summary = data.summary;
  resultSession.textContent = displayValue(data.session_id, "Unknown session");
  summaryGrid.replaceChildren(
    summaryItem("Turns", summary.turn_count),
    summaryItem("Contradicted", summary.contradicted_turns),
    summaryItem("Not found", summary.not_found_turns),
    summaryItem("Supported", summary.supported_turns),
    summaryItem("Skipped", summary.skipped_turns),
  );
}

function detailRow(label, value, fallback) {
  const row = element("div", "detail-row");
  row.append(element("dt", "detail-label", label));
  row.append(element("dd", "detail-value", displayValue(value, fallback)));
  return row;
}

function renderClaim(claim, claimNumber) {
  const card = element("article", "claim-card");
  const heading = element("div", "claim-heading");
  heading.append(element("h4", "claim-number", `Claim ${claimNumber}`));
  heading.append(stateBadge(claim.verdict));
  card.append(heading);

  const claimText = element("p", "claim-text", displayValue(claim.claim_text, "No claim text returned"));
  card.append(claimText);

  const details = element("dl", "claim-details");
  details.append(detailRow("Subject", claim.subject_tag, "Not specified"));
  if (claim.tag) details.append(detailRow("Tag", claim.tag));
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

function evaluationDescription(turn) {
  if (turn.turn_verdict === "UNCLASSIFIED") return "This turn has not been classified.";
  if (turn.turn_verdict === "NOT_EVALUATED") {
    return "No checkable factual claim was identified in this turn.";
  }
  if (turn.turn_verdict === "NO_CLAIMS") {
    return "The turn was flagged, but no claim was extracted.";
  }
  return turn.contains_claims
    ? "This turn contains checkable factual content."
    : "No checkable factual content was recorded.";
}

function renderTurn(turn, claims) {
  const failed = turn.turn_verdict === "CONTRADICTED" || turn.turn_verdict === "NOT_FOUND";
  const card = element("article", `turn-card${failed ? " turn-failed" : ""}`);
  const heading = element("div", "turn-heading");
  const titleGroup = element("div");
  titleGroup.append(element("p", "turn-label", `Turn ${Number(turn.pair_index) + 1}`));
  titleGroup.append(
    element("span", "turn-index", `pair_index ${displayValue(turn.pair_index, "unknown")}`),
  );
  heading.append(titleGroup, stateBadge(turn.turn_verdict));
  card.append(heading);

  if (failed) card.append(element("p", "failure-callout", "This turn requires review."));

  const exchange = element("div", "exchange-grid");
  const caller = element("section", "speech-block");
  caller.append(element("h3", null, "Caller"));
  caller.append(element("p", null, displayValue(turn.caller_asked, "No caller text returned")));
  const ava = element("section", "speech-block speech-ava");
  ava.append(element("h3", null, "AVA"));
  ava.append(element("p", null, displayValue(turn.agent_said, "No AVA response returned")));
  exchange.append(caller, ava);
  card.append(exchange);

  const state = element("div", "evaluation-line");
  state.append(element("strong", null, "Evaluation state: "));
  state.append(element("span", null, evaluationDescription(turn)));
  state.append(
    element(
      "span",
      "count-note",
      `${displayValue(turn.claim_count, "0")} claim(s) · ${displayValue(
        turn.contradicted_count,
        "0",
      )} contradicted · ${displayValue(turn.not_found_count, "0")} not found · ${displayValue(
        turn.supported_count,
        "0",
      )} supported`,
    ),
  );
  card.append(state);

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

function renderResults(data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new Error("The evaluator returned an unexpected response.");
  }
  if (!data.summary || !Array.isArray(data.turns) || !Array.isArray(data.claims)) {
    throw new Error("The evaluator response is missing summary, turn, or claim data.");
  }

  const claimsByTurn = new Map();
  data.claims.forEach((claim) => {
    const key = String(claim.pair_index);
    if (!claimsByTurn.has(key)) claimsByTurn.set(key, []);
    claimsByTurn.get(key).push(claim);
  });

  renderSummary(data);
  turnList.replaceChildren();
  data.turns.forEach((turn) => {
    turnList.append(renderTurn(turn, claimsByTurn.get(String(turn.pair_index)) || []));
  });
  if (!data.turns.length) {
    turnList.append(element("p", "empty-note panel", "No turn results were returned."));
  }
  results.hidden = false;
  results.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function evaluateTranscript() {
  const payload = parseTranscript();
  if (payload === null) return;

  errorPanel.hidden = true;
  results.hidden = true;
  setBusy(true);

  try {
    const response = await fetch("/api/evaluations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const message = await responseError(response);
      const titles = {
        422: "Transcript validation failed",
        500: "Evaluation failed",
        503: "Evaluator unavailable",
      };
      showRequestError(titles[response.status] || "Evaluation request failed", message);
      return;
    }
    renderResults(await response.json());
  } catch (error) {
    showRequestError(
      "Could not reach the evaluator",
      error instanceof Error ? error.message : "An unexpected network error occurred.",
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
loadHealth();
