const state = {
  authed: false,
  dashboard: null,
  sessionId: null,
  detailMode: null,
  activeScreen: "chat",
  historySessions: [],
};

const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    credentials: "same-origin",
    ...options,
  });
  if (response.status === 401) {
    showLogin();
    throw new Error("Authentication required");
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `Request failed: ${response.status}`);
  }
  return response.json();
}

function titleize(value) {
  return String(value).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatTime(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(
    new Date(value)
  );
}

function showLogin() {
  state.authed = false;
  $("#loginView").hidden = false;
  $("#appView").hidden = true;
}

function showApp() {
  state.authed = true;
  $("#loginView").hidden = true;
  $("#appView").hidden = false;
  setScreen(state.activeScreen);
}

function empty(container, text) {
  container.innerHTML = "";
  const node = document.createElement("div");
  node.className = "item";
  const p = document.createElement("p");
  p.className = "subtle";
  p.textContent = text;
  node.appendChild(p);
  container.appendChild(node);
}

function renderMetrics(metrics = {}) {
  const row = document.createElement("div");
  row.className = "metrics-row";
  for (const [key, value] of Object.entries(metrics).slice(0, 4)) {
    const pill = document.createElement("span");
    pill.className = "metric-pill";
    const label = document.createElement("span");
    label.textContent = titleize(key);
    const strong = document.createElement("strong");
    strong.textContent = String(value);
    pill.append(label, strong);
    row.appendChild(pill);
  }
  return row;
}

function renderPreview(card) {
  const preview = document.createElement("div");
  preview.className = "section-preview";
  const firstSection = (card.sections || []).find((section) => section.items?.length);
  if (!firstSection) {
    const p = document.createElement("p");
    p.className = "subtle";
    p.textContent = "No details yet.";
    preview.appendChild(p);
    return preview;
  }
  const ul = document.createElement("ul");
  for (const item of firstSection.items.slice(0, 3)) {
    const li = document.createElement("li");
    const when = item.due_at || item.starts_at || item.occurred_at;
    li.textContent = `${item.label || item.value || "Untitled"}${when ? ` · ${formatTime(when)}` : ""}`;
    ul.appendChild(li);
  }
  preview.appendChild(ul);
  return preview;
}

function renderCard(card) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `schema-card ${card.mode}`;
  button.dataset.mode = card.mode;

  const titleRow = document.createElement("div");
  titleRow.className = "card-title-row";
  const title = document.createElement("h2");
  title.textContent = card.title;
  const priority = document.createElement("span");
  priority.className = "metric";
  priority.textContent = String(card.priority);
  titleRow.append(title, priority);

  const summary = document.createElement("p");
  summary.className = "summary";
  summary.textContent = card.summary;

  button.append(titleRow, summary, renderMetrics(card.metrics), renderPreview(card));
  button.addEventListener("click", () => openDetail(card.mode));
  return button;
}

function renderDashboard(data) {
  state.dashboard = data;
  const latestRun = data.agent_runs?.[0];
  $("#agentStatus").textContent = latestRun ? latestRun.status : "Idle";

  const grid = $("#cardsGrid");
  grid.innerHTML = "";
  const modes = data.card_order || ["execution", "analysis", "journal", "persona"];
  for (const mode of modes) {
    if (data.cards?.[mode]) {
      grid.appendChild(renderCard(data.cards[mode]));
    }
  }
  if (!grid.children.length) {
    empty(grid, "No dashboard cards yet.");
  }

  renderChatHistory(data.chat_messages || []);
}

function renderChatHistory(messages) {
  const chat = $("#chatMessages");
  chat.innerHTML = "";
  for (const message of [...messages].reverse().slice(-10)) {
    addMessage(message.role === "assistant" ? "assistant" : "user", message.content, false);
    state.sessionId = message.session_id || state.sessionId;
  }
}

function renderHistorySessions(sessions) {
  const content = $("#historyContent");
  content.innerHTML = "";
  if (!sessions.length) {
    empty(content, "No chat sessions yet.");
    return;
  }
  for (const session of sessions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-session";
    button.dataset.sessionId = session.id;
    const title = document.createElement("strong");
    title.textContent = session.title || `Chat ${session.id}`;
    const meta = document.createElement("span");
    meta.className = "subtle";
    meta.textContent = `${session.message_count} messages${session.last_message_at ? ` · ${formatTime(session.last_message_at)}` : ""}`;
    const preview = document.createElement("span");
    preview.className = "subtle";
    preview.textContent = session.preview || "No preview";
    button.append(title, meta, preview);
    content.appendChild(button);
  }
}

async function openHistory() {
  const history = await api("/api/chat/history");
  state.historySessions = history.sessions || [];
  renderHistorySessions(state.historySessions);
  $("#historyModal").hidden = false;
}

function closeHistory() {
  $("#historyModal").hidden = true;
}

async function loadChatSession(sessionId) {
  const history = await api(`/api/chat/history?session_id=${sessionId}`);
  state.sessionId = history.session.id;
  renderChatHistory(history.messages || []);
  closeHistory();
  setScreen("chat");
}

function setScreen(screen) {
  state.activeScreen = screen === "overview" ? "overview" : "chat";
  $("#chatView").hidden = state.activeScreen !== "chat";
  $("#overviewView").hidden = state.activeScreen !== "overview";
  $("#chatNavButton").classList.toggle("active", state.activeScreen === "chat");
  $("#overviewNavButton").classList.toggle("active", state.activeScreen === "overview");
  if (state.activeScreen === "chat") {
    $("#chatMessages").scrollTop = $("#chatMessages").scrollHeight;
  }
}

async function refreshDashboard() {
  const data = await api("/api/dashboard");
  renderDashboard(data);
}

function addMessage(role, text, scroll = true) {
  const node = document.createElement("div");
  node.className = `message ${role}`;
  node.textContent = text;
  $("#chatMessages").appendChild(node);
  if (scroll) {
    $("#chatMessages").scrollTop = $("#chatMessages").scrollHeight;
  }
}

function renderDetailCard(card) {
  const wrapper = document.createElement("div");
  wrapper.className = "detail-content";

  const summary = document.createElement("p");
  summary.className = "summary";
  summary.textContent = card.summary;
  wrapper.append(renderMetrics(card.metrics), summary);

  for (const section of card.sections || []) {
    const sectionNode = document.createElement("section");
    sectionNode.className = "detail-section";
    const heading = document.createElement("h3");
    heading.textContent = section.title || "Details";
    sectionNode.appendChild(heading);
    if (!section.items?.length) {
      const p = document.createElement("p");
      p.className = "subtle";
      p.textContent = "No items yet.";
      sectionNode.appendChild(p);
    } else {
      const list = document.createElement("ul");
      list.className = "detail-list";
      for (const item of section.items) {
        const li = document.createElement("li");
        const label = item.label || item.value || "Untitled";
        const when = item.due_at || item.starts_at || item.occurred_at;
        li.textContent = `${label}${item.kind ? ` · ${item.kind}` : ""}${when ? ` · ${formatTime(when)}` : ""}`;
        if (item.time_item_id && item.status !== "complete") {
          const actions = document.createElement("div");
          actions.className = "detail-actions";
          const complete = document.createElement("button");
          complete.type = "button";
          complete.className = "ghost";
          complete.dataset.completeItem = item.time_item_id;
          complete.textContent = "Complete";
          const snooze = document.createElement("button");
          snooze.type = "button";
          snooze.className = "ghost";
          snooze.dataset.snoozeItem = item.time_item_id;
          snooze.textContent = "Snooze";
          actions.append(complete, snooze);
          li.appendChild(actions);
        }
        list.appendChild(li);
      }
      sectionNode.appendChild(list);
    }
    wrapper.appendChild(sectionNode);
  }
  return wrapper;
}

async function openDetail(mode) {
  state.detailMode = mode;
  const card = state.dashboard?.cards?.[mode];
  if (!card) return;
  $("#detailMode").textContent = titleize(mode);
  $("#detailTitle").textContent = card.title;
  const detail = $("#detailContent");
  detail.innerHTML = "";
  detail.appendChild(renderDetailCard(card));

  const history = await api(`/api/cards/${mode}/history`);
  const historySection = document.createElement("section");
  historySection.className = "detail-section";
  const heading = document.createElement("h3");
  heading.textContent = "History";
  historySection.appendChild(heading);
  const historyList = document.createElement("div");
  historyList.className = "history-list";
  for (const item of history.cards.slice(1, 8)) {
    const node = document.createElement("article");
    node.className = "item";
    const title = document.createElement("strong");
    title.textContent = `${item.title} · ${formatTime(item.created_at)}`;
    const p = document.createElement("p");
    p.textContent = item.summary;
    node.append(title, p);
    historyList.appendChild(node);
  }
  if (!historyList.children.length) {
    const p = document.createElement("p");
    p.className = "subtle";
    p.textContent = "No previous cards yet.";
    historyList.appendChild(p);
  }
  historySection.appendChild(historyList);
  detail.appendChild(historySection);
  $("#detailModal").hidden = false;
}

function closeDetail() {
  $("#detailModal").hidden = true;
  state.detailMode = null;
}

async function boot() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }

  try {
    await api("/api/auth/me");
    showApp();
    await refreshDashboard();
  } catch {
    showLogin();
  }
}

$("#loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#loginError").textContent = "";
  try {
    await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ password: $("#passwordInput").value }),
    });
    showApp();
    await refreshDashboard();
  } catch (error) {
    $("#loginError").textContent = error.message;
  }
});

$("#logoutButton").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

$("#chatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  const message = $("#chatInput").value.trim();
  if (!message) return;
  $("#chatInput").value = "";
  addMessage("user", message);
  button.disabled = true;
  try {
    const response = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message, session_id: state.sessionId }),
    });
    state.sessionId = response.session_id || state.sessionId;
    addMessage("assistant", response.answer);
    await refreshDashboard();
  } catch (error) {
    addMessage("assistant", error.message);
  } finally {
    button.disabled = false;
  }
});

$("#chatNavButton").addEventListener("click", () => setScreen("chat"));
$("#overviewNavButton").addEventListener("click", () => setScreen("overview"));
$("#historyButton").addEventListener("click", () => openHistory());

$("#historyModal").addEventListener("click", async (event) => {
  if (event.target.matches("[data-close-history]")) {
    closeHistory();
    return;
  }
  const sessionButton = event.target.closest("[data-session-id]");
  if (sessionButton) {
    await loadChatSession(sessionButton.dataset.sessionId);
  }
});

$("#detailModal").addEventListener("click", async (event) => {
  if (event.target.matches("[data-close-detail]")) {
    closeDetail();
    return;
  }
  const completeId = event.target.dataset.completeItem;
  const snoozeId = event.target.dataset.snoozeItem;
  if (!completeId && !snoozeId) return;
  event.target.disabled = true;
  if (completeId) {
    await api(`/api/time-items/${completeId}/complete`, { method: "POST" });
  }
  if (snoozeId) {
    await api(`/api/time-items/${snoozeId}/snooze`, { method: "POST", body: JSON.stringify({ days: 1 }) });
  }
  await refreshDashboard();
  if (state.detailMode) {
    await openDetail(state.detailMode);
  }
});

boot();
