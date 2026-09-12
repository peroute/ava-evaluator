// Step 1 Inspector - plain JavaScript, no framework and no build step.
// Every call is a GET against the read-only /api endpoints.

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

function showError(message) {
  const box = document.getElementById("error");
  box.textContent = message;
  box.hidden = !message;
}

// --- Overview ---------------------------------------------------------------

async function loadSummary() {
  const summary = await getJson("/api/summary");
  document.getElementById("stat-conversations").textContent = summary.conversations_accepted;
  document.getElementById("stat-turns").textContent = summary.turns_stored;
  document.getElementById("stat-quarantine").textContent = summary.quarantined_records;
}

// --- Conversations ----------------------------------------------------------

async function loadSessions() {
  const data = await getJson("/api/conversations");
  const list = document.getElementById("session-list");
  list.replaceChildren();

  if (data.conversations.length === 0) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "No conversations stored.";
    list.append(empty);
    return;
  }

  data.conversations.forEach((conversation) => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.className = "session-button";
    button.textContent = conversation.session_id;

    const count = document.createElement("span");
    count.className = "count";
    count.textContent = `${conversation.turn_count} turns`;
    button.append(count);

    button.addEventListener("click", () => {
      document
        .querySelectorAll(".session-button")
        .forEach((other) => other.classList.remove("is-active"));
      button.classList.add("is-active");
      loadTurns(conversation.session_id);
    });

    item.append(button);
    list.append(item);
  });
}

async function loadTurns(sessionId) {
  const table = document.getElementById("turns-table");
  const body = document.getElementById("turns-body");
  try {
    const data = await getJson(`/api/conversations/${encodeURIComponent(sessionId)}`);
    document.getElementById("turns-heading").textContent =
      `${data.session_id} - ${data.turn_count} turns`;
    body.replaceChildren();

    // The API already returns turns in pair_index order.
    data.turns.forEach((turn) => {
      const row = document.createElement("tr");
      row.append(cell(turn.pair_index, "narrow"), cell(turn.caller_asked), cell(turn.agent_said));
      body.append(row);
    });

    table.hidden = false;
    showError("");
  } catch (error) {
    table.hidden = true;
    showError(error.message);
  }
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

// --- Quarantine -------------------------------------------------------------

async function loadQuarantine() {
  const data = await getJson("/api/quarantine");
  const body = document.getElementById("quarantine-body");
  body.replaceChildren();
  document.getElementById("quarantine-empty").hidden = data.records.length > 0;

  data.records.forEach((record) => {
    const row = document.createElement("tr");
    row.append(
      cell(record.id, "narrow"),
      cell(record.session_id === null ? "(none)" : record.session_id),
      cell(record.reason),
      cell(record.created_at),
      payloadCell(record.raw_payload)
    );
    body.append(row);
  });
}

// raw_payload can be long, so hide it behind a native <details> toggle.
function payloadCell(payload) {
  const td = document.createElement("td");
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = "view";
  const pre = document.createElement("pre");
  pre.textContent = prettyJson(payload);
  details.append(summary, pre);
  td.append(details);
  return td;
}

function prettyJson(text) {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text; // not JSON (e.g. a malformed-file path) - show it as-is
  }
}

// --- Tabs and startup -------------------------------------------------------

function setUpTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((other) => other.classList.remove("is-active"));
      tab.classList.add("is-active");
      document.querySelectorAll(".panel").forEach((panel) => {
        panel.hidden = panel.id !== `panel-${tab.dataset.panel}`;
      });
    });
  });
}

async function loadAll() {
  try {
    showError("");
    await Promise.all([loadSummary(), loadSessions(), loadQuarantine()]);
  } catch (error) {
    showError(`Could not load data: ${error.message}`);
  }
}

setUpTabs();
document.getElementById("refresh").addEventListener("click", loadAll);
loadAll();
