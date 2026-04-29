const state = {
  authed: false,
  activeScreen: "overview",
  sessionId: null,
  overview: null,
  persona: null,
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

function formatTime(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatDay(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  }).format(new Date(value));
}

function titleize(value) {
  return String(value).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
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
  node.className = "empty-state";
  node.textContent = text;
  container.appendChild(node);
}

function metricPill(label, value) {
  const pill = document.createElement("span");
  pill.className = "metric-pill";
  const strong = document.createElement("strong");
  strong.textContent = String(value);
  pill.append(strong, document.createTextNode(label));
  return pill;
}

function renderStringList(title, items, className = "mini-list") {
  const section = document.createElement("section");
  section.className = "summary-column";
  const heading = document.createElement("h3");
  heading.textContent = title;
  section.appendChild(heading);
  const list = document.createElement("ul");
  list.className = className;
  const source = Array.isArray(items) && items.length ? items : ["No signal yet."];
  for (const item of source) {
    const li = document.createElement("li");
    li.textContent = item;
    list.appendChild(li);
  }
  section.appendChild(list);
  return section;
}

function renderUrgentItem(item) {
  const article = document.createElement("article");
  article.className = "urgent-item";
  const copy = document.createElement("div");
  copy.className = "urgent-copy";
  const title = document.createElement("strong");
  title.textContent = item.label || "Untitled";
  const meta = document.createElement("p");
  const when = item.due_at || item.starts_at;
  meta.textContent = `${titleize(item.kind || "task")}${when ? ` · ${formatTime(when)}` : ""}`;
  copy.append(title, meta);
  article.appendChild(copy);
  if (item.status !== "complete" && item.time_item_id) {
    const actions = document.createElement("div");
    actions.className = "urgent-actions";
    const complete = document.createElement("button");
    complete.type = "button";
    complete.className = "ghost compact-button";
    complete.dataset.completeItem = item.time_item_id;
    complete.textContent = "Complete";
    const snooze = document.createElement("button");
    snooze.type = "button";
    snooze.className = "ghost compact-button";
    snooze.dataset.snoozeItem = item.time_item_id;
    snooze.textContent = "Snooze";
    actions.append(complete, snooze);
    article.appendChild(actions);
  }
  return article;
}

function renderMilestone(summary) {
  const article = document.createElement("article");
  article.className = "milestone-card";

  const header = document.createElement("div");
  header.className = "milestone-head";
  const label = document.createElement("p");
  label.className = "milestone-label";
  label.textContent = summary.label || titleize(summary.period_key);
  const title = document.createElement("h2");
  title.textContent = summary.headline || summary.title;
  const windowMeta = document.createElement("p");
  windowMeta.className = "subtle";
  windowMeta.textContent = `${formatDay(summary.window_start)} to ${formatDay(summary.window_end)}`;
  header.append(label, title, windowMeta);

  const narrative = document.createElement("p");
  narrative.className = "milestone-narrative";
  narrative.textContent = summary.narrative || summary.body;

  const metrics = document.createElement("div");
  metrics.className = "metrics-row";
  for (const [key, value] of Object.entries(summary.metrics || {}).slice(0, 6)) {
    metrics.appendChild(metricPill(titleize(key), value));
  }

  const columns = document.createElement("div");
  columns.className = "summary-grid";
  columns.append(
    renderStringList("Wins", summary.wins),
    renderStringList("Risks", summary.risks),
    renderStringList("Patterns", summary.patterns),
    renderStringList("Carry Forward", summary.carry_forward_points),
  );

  article.append(header, narrative, metrics, columns);
  if (summary.open_loops?.length) {
    article.appendChild(renderStringList("Open Loops", summary.open_loops, "mini-list open-loop-list"));
  }
  return article;
}

function renderBriefList(title, items) {
  if (!items?.length) return null;
  return renderStringList(title, items);
}

function renderPersonaSummaryCard(title, text) {
  const section = document.createElement("section");
  section.className = "persona-group persona-summary-card";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const body = document.createElement("p");
  body.className = "reflection-message";
  body.textContent = text || "No signal yet.";
  section.append(heading, body);
  return section;
}

function renderOverview(data) {
  state.overview = data;
  const container = $("#overviewContent");
  container.innerHTML = "";
  $("#agentStatus").textContent = data.latest_run?.status || "Idle";

  const surface = document.createElement("section");
  surface.className = "reflection-surface";

  const hero = document.createElement("header");
  hero.className = "reflection-hero";
  const title = document.createElement("h2");
  title.textContent = data.brief_title || "Dagens brief";
  const message = document.createElement("p");
  message.className = "reflection-message";
  message.textContent = data.brief_message || "Begynn å chatte for å gi meg noe å analysere!";
  const meta = document.createElement("p");
  meta.className = "subtle";
  meta.textContent = data.generated_at ? `Generated ${formatTime(data.generated_at)}` : "";
  hero.append(title, message);

  const brief = data.brief_payload || {};
  const metrics = document.createElement("div");
  metrics.className = "metrics-row";
  for (const [key, value] of Object.entries(brief.metrics || {}).slice(0, 5)) {
    metrics.appendChild(metricPill(titleize(key), value));
  }
  if (metrics.childNodes.length) {
    hero.appendChild(metrics);
  }

  const briefGrid = document.createElement("div");
  briefGrid.className = "summary-grid";
  for (const node of [
    renderBriefList("Today Focus", brief.today_focus),
    renderBriefList("Recent Signals", brief.recent_relevant_signals),
    renderBriefList("Tips", brief.tips),
  ]) {
    if (node) briefGrid.appendChild(node);
  }
  if (briefGrid.childNodes.length) {
    hero.appendChild(briefGrid);
  }

  hero.appendChild(meta);
  surface.appendChild(hero);

  const urgentSection = document.createElement("section");
  urgentSection.className = "surface-section";
  const urgentHead = document.createElement("div");
  urgentHead.className = "section-head";
  const urgentTitle = document.createElement("h3");
  urgentTitle.textContent = "Urgent Items";
  urgentHead.appendChild(urgentTitle);
  urgentSection.appendChild(urgentHead);
  if (data.urgent_items?.length) {
    const urgentList = document.createElement("div");
    urgentList.className = "urgent-list";
    for (const item of data.urgent_items) {
      urgentList.appendChild(renderUrgentItem(item));
    }
    urgentSection.appendChild(urgentList);
  } else {
    const p = document.createElement("p");
    p.className = "subtle";
    p.textContent = "No urgent items are active.";
    urgentSection.appendChild(p);
  }
  surface.appendChild(urgentSection);

  const milestoneSection = document.createElement("details");
  milestoneSection.className = "surface-section history-section";
  const milestoneHead = document.createElement("summary");
  milestoneHead.className = "section-head history-toggle";
  const milestoneTitle = document.createElement("h3");
  milestoneTitle.textContent = "Earlier Reflections";
  const milestoneMeta = document.createElement("p");
  milestoneMeta.className = "subtle";
  milestoneMeta.textContent = data.milestones?.length ? `${data.milestones.length} rolling windows` : "No history yet";
  milestoneHead.append(milestoneTitle, milestoneMeta);
  milestoneSection.appendChild(milestoneHead);

  const milestoneBody = document.createElement("div");
  milestoneBody.className = "history-stack";
  if (data.milestones?.length) {
    const stack = document.createElement("div");
    stack.className = "milestone-stack";
    for (const summary of data.milestones) {
      stack.appendChild(renderMilestone(summary));
    }
    milestoneBody.appendChild(stack);
  } else {
    const p = document.createElement("p");
    p.className = "subtle";
    p.textContent = "No milestone summaries yet.";
    milestoneBody.appendChild(p);
  }
  milestoneSection.appendChild(milestoneBody);
  surface.appendChild(milestoneSection);

  container.appendChild(surface);
}

function renderPersonaGroup(title, items) {
  const section = document.createElement("section");
  section.className = "persona-group";
  const heading = document.createElement("h3");
  heading.textContent = title;
  section.appendChild(heading);
  if (!items?.length) {
    const p = document.createElement("p");
    p.className = "subtle";
    p.textContent = "No inferred memories yet.";
    section.appendChild(p);
    return section;
  }
  const list = document.createElement("div");
  list.className = "memory-list";
  for (const item of items) {
    const article = document.createElement("article");
    article.className = "memory-item";
    const body = document.createElement("p");
    body.textContent = item.content;
    const meta = document.createElement("p");
    meta.className = "subtle";
    meta.textContent = `Confidence ${Math.round((item.confidence || 0) * 100)}%`;
    article.append(body, meta);
    list.appendChild(article);
  }
  section.appendChild(list);
  return section;
}

function renderPersona(data) {
  state.persona = data;
  const container = $("#personaContent");
  container.innerHTML = "";
  const profile = data.stable_profile || {};

  const wrapper = document.createElement("div");
  wrapper.className = "persona-layout";

  const formCard = document.createElement("section");
  formCard.className = "persona-card";
  const formHead = document.createElement("div");
  formHead.className = "section-head";
  const formTitle = document.createElement("h2");
  formTitle.textContent = "Stable Profile";
  const formMeta = document.createElement("p");
  formMeta.className = "subtle";
  formMeta.textContent = data.updated_at ? `Updated ${formatTime(data.updated_at)}` : "";
  formHead.append(formTitle, formMeta);

  const form = document.createElement("form");
  form.id = "personaForm";
  form.className = "persona-form";
  const fields = [
    ["name", "Name", "text"],
    ["gender", "Gender", "text"],
  ];
  for (const [key, labelText, type] of fields) {
    const label = document.createElement("label");
    label.className = "field";
    const span = document.createElement("span");
    span.textContent = labelText;
    const input = document.createElement("input");
    input.name = key;
    input.type = type;
    input.value = profile[key] ?? "";
    label.append(span, input);
    form.appendChild(label);
  }

  const saveRow = document.createElement("div");
  saveRow.className = "form-actions";
  const save = document.createElement("button");
  save.type = "submit";
  save.textContent = "Save Persona";
  const status = document.createElement("p");
  status.id = "personaStatus";
  status.className = "subtle";
  saveRow.append(save, status);
  form.append(saveRow);

  formCard.append(formHead, form);

  const inferredCard = document.createElement("section");
  inferredCard.className = "persona-card";
  const inferredHead = document.createElement("div");
  inferredHead.className = "section-head";
  const inferredTitle = document.createElement("h2");
  inferredTitle.textContent = "How LifeOS Sees You";
  inferredHead.appendChild(inferredTitle);
  inferredCard.appendChild(inferredHead);
  const summary = data.inferred_profile_summary || {};
  inferredCard.append(
    renderPersonaSummaryCard("How LifeOS sees you", summary.identity),
    renderPersonaSummaryCard("Current wellbeing baseline", summary.wellbeing_baseline),
    renderPersonaSummaryCard("Focus and goals", summary.focus_and_goals),
    renderPersonaSummaryCard("Preferences and work style", summary.preferences_and_work_style),
  );
  const groups = data.inferred_groups || {};
  for (const key of ["traits", "preferences", "goals", "health_patterns", "work_style", "wellbeing_signals", "other"]) {
    inferredCard.appendChild(renderPersonaGroup(titleize(key), groups[key]));
  }

  wrapper.append(formCard, inferredCard);
  container.appendChild(wrapper);
}

function renderChatHistory(messages) {
  const chat = $("#chatMessages");
  chat.innerHTML = "";
  for (const message of messages || []) {
    addMessage(message.role === "assistant" ? "assistant" : "user", message.content, false);
    state.sessionId = message.session_id || state.sessionId;
  }
  chat.scrollTop = chat.scrollHeight;
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
  state.activeScreen = ["overview", "chat", "persona"].includes(screen) ? screen : "overview";
  $("#overviewView").hidden = state.activeScreen !== "overview";
  $("#chatView").hidden = state.activeScreen !== "chat";
  $("#personaView").hidden = state.activeScreen !== "persona";
  $("#overviewNavButton").classList.toggle("active", state.activeScreen === "overview");
  $("#chatNavButton").classList.toggle("active", state.activeScreen === "chat");
  $("#personaNavButton").classList.toggle("active", state.activeScreen === "persona");
}

async function refreshAppData() {
  const [overview, persona] = await Promise.all([api("/api/overview"), api("/api/persona")]);
  renderOverview(overview);
  renderPersona(persona);
}

async function loadLatestChatSession() {
  const sessions = await api("/api/chat/history");
  state.historySessions = sessions.sessions || [];
  if (!state.historySessions.length) {
    $("#chatMessages").innerHTML = "";
    return;
  }
  const latest = state.historySessions[0];
  const history = await api(`/api/chat/history?session_id=${latest.id}`);
  state.sessionId = history.session.id;
  renderChatHistory(history.messages || []);
}

async function boot() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
  try {
    await api("/api/auth/me");
    showApp();
    await Promise.all([refreshAppData(), loadLatestChatSession()]);
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
    await Promise.all([refreshAppData(), loadLatestChatSession()]);
  } catch (error) {
    $("#loginError").textContent = error.message;
  }
});

$("#logoutButton").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

$("#overviewNavButton").addEventListener("click", () => setScreen("overview"));
$("#chatNavButton").addEventListener("click", () => setScreen("chat"));
$("#personaNavButton").addEventListener("click", () => setScreen("persona"));
$("#historyButton").addEventListener("click", () => openHistory());

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
    await refreshAppData();
  } catch (error) {
    addMessage("assistant", error.message);
  } finally {
    button.disabled = false;
  }
});

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

$("#overviewContent").addEventListener("click", async (event) => {
  const completeId = event.target.dataset.completeItem;
  const snoozeId = event.target.dataset.snoozeItem;
  if (!completeId && !snoozeId) return;
  event.target.disabled = true;
  try {
    if (completeId) {
      await api(`/api/time-items/${completeId}/complete`, { method: "POST" });
    }
    if (snoozeId) {
      await api(`/api/time-items/${snoozeId}/snooze`, { method: "POST", body: JSON.stringify({ days: 1 }) });
    }
    await refreshAppData();
  } finally {
    event.target.disabled = false;
  }
});

$("#personaContent").addEventListener("submit", async (event) => {
  if (event.target.id !== "personaForm") return;
  event.preventDefault();
  const form = new FormData(event.target);
  const payload = {};
  for (const [key, value] of form.entries()) {
    payload[key] = String(value).trim();
  }
  const status = $("#personaStatus");
  status.textContent = "Saving...";
  try {
    const response = await api("/api/persona", { method: "PATCH", body: JSON.stringify(payload) });
    renderPersona(response);
    status.textContent = "Saved.";
  } catch (error) {
    status.textContent = error.message;
  }
});

boot();
