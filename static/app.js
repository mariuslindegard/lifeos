const state = {
  authed: false,
  activeScreen: "overview",
  sessionId: null,
  draftChat: false,
  chatLoadToken: 0,
  overview: null,
  persona: null,
  historySessions: [],
  chatStreaming: false,
  sessionStreamController: null,
  sessionStreamId: null,
  sessionStreamMessageId: null,
  runningAssistantMessage: null,
  screenScroll: {
    overview: 0,
    chat: null,
    persona: 0,
  },
};

const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function sanitizeUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  if (/^(https?:|mailto:|tel:|\/|#)/i.test(raw)) {
    return raw.replaceAll('"', "%22");
  }
  return "";
}

function parseInlineMarkdown(value) {
  let html = escapeHtml(value);
  const tokens = [];
  html = html.replace(/`([^`\n]+)`/g, (_match, code) => {
    const token = `@@CODE${tokens.length}@@`;
    tokens.push(`<code>${code}</code>`);
    return token;
  });
  html = html.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)/g, (_match, alt, url, title) => {
    const safeUrl = sanitizeUrl(url);
    if (!safeUrl) return escapeHtml(_match);
    const titleAttr = title ? ` title="${escapeHtml(title)}"` : "";
    return `<img src="${safeUrl}" alt="${escapeHtml(alt)}"${titleAttr}>`;
  });
  html = html.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)/g, (_match, label, url, title) => {
    const safeUrl = sanitizeUrl(url);
    if (!safeUrl) return escapeHtml(_match);
    const titleAttr = title ? ` title="${escapeHtml(title)}"` : "";
    const external = /^(https?:|mailto:|tel:)/i.test(safeUrl) ? ' target="_blank" rel="noreferrer"' : "";
    return `<a href="${safeUrl}"${titleAttr}${external}>${label}</a>`;
  });
  html = html.replace(/\*\*\*([^*]+)\*\*\*/g, "<strong><em>$1</em></strong>");
  html = html.replace(/___([^_]+)___/g, "<strong><em>$1</em></strong>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  html = html.replace(/(^|[^\*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  html = html.replace(/(^|[^_])_([^_\n]+)_(?!_)/g, "$1<em>$2</em>");
  html = html.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  for (let index = 0; index < tokens.length; index += 1) {
    html = html.replace(`@@CODE${index}@@`, tokens[index]);
  }
  return html;
}

function splitTableRow(line) {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map((cell) => cell.trim());
}

function renderMarkdown(source) {
  const text = String(source || "").replace(/\r\n?/g, "\n");
  const lines = text.split("\n");
  const blocks = [];
  let index = 0;

  const isTableDivider = (line) => /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$/.test(line.trim());
  const isTableRow = (line) => line.includes("|");

  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();
    if (!trimmed) {
      index += 1;
      continue;
    }
    const fenceMatch = trimmed.match(/^```(\w+)?\s*$/);
    if (fenceMatch) {
      index += 1;
      const codeLines = [];
      while (index < lines.length && !lines[index].trim().startsWith("```")) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) {
        index += 1;
      }
      const language = fenceMatch[1] ? ` class="language-${escapeHtml(fenceMatch[1])}"` : "";
      blocks.push(`<pre><code${language}>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
      continue;
    }
    const headingMatch = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      blocks.push(`<h${level}>${parseInlineMarkdown(headingMatch[2])}</h${level}>`);
      index += 1;
      continue;
    }
    if (/^([-*_])(?:\s*\1){2,}\s*$/.test(trimmed)) {
      blocks.push("<hr>");
      index += 1;
      continue;
    }
    if (isTableRow(trimmed) && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
      const headers = splitTableRow(lines[index]);
      index += 2;
      const rows = [];
      while (index < lines.length && lines[index].trim() && isTableRow(lines[index])) {
        rows.push(splitTableRow(lines[index]));
        index += 1;
      }
      const head = headers.map((cell) => `<th>${parseInlineMarkdown(cell)}</th>`).join("");
      const body = rows
        .map((row) => `<tr>${row.map((cell) => `<td>${parseInlineMarkdown(cell)}</td>`).join("")}</tr>`)
        .join("");
      blocks.push(`<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`);
      continue;
    }
    if (/^>\s?/.test(trimmed)) {
      const quoteLines = [];
      while (index < lines.length && /^>\s?/.test(lines[index].trim())) {
        quoteLines.push(lines[index].trim().replace(/^>\s?/, ""));
        index += 1;
      }
      blocks.push(`<blockquote>${renderMarkdown(quoteLines.join("\n"))}</blockquote>`);
      continue;
    }
    if (/^[-+*]\s+/.test(trimmed)) {
      const items = [];
      while (index < lines.length && /^[-+*]\s+/.test(lines[index].trim())) {
        items.push(lines[index].trim().replace(/^[-+*]\s+/, ""));
        index += 1;
      }
      blocks.push(`<ul>${items.map((item) => `<li>${parseInlineMarkdown(item)}</li>`).join("")}</ul>`);
      continue;
    }
    if (/^\d+\.\s+/.test(trimmed)) {
      const items = [];
      while (index < lines.length && /^\d+\.\s+/.test(lines[index].trim())) {
        items.push(lines[index].trim().replace(/^\d+\.\s+/, ""));
        index += 1;
      }
      blocks.push(`<ol>${items.map((item) => `<li>${parseInlineMarkdown(item)}</li>`).join("")}</ol>`);
      continue;
    }
    const paragraphLines = [];
    while (
      index < lines.length &&
      lines[index].trim() &&
      !/^```/.test(lines[index].trim()) &&
      !/^(#{1,6})\s+/.test(lines[index].trim()) &&
      !/^>\s?/.test(lines[index].trim()) &&
      !/^[-+*]\s+/.test(lines[index].trim()) &&
      !/^\d+\.\s+/.test(lines[index].trim()) &&
      !/^([-*_])(?:\s*\1){2,}\s*$/.test(lines[index].trim()) &&
      !(isTableRow(lines[index].trim()) && index + 1 < lines.length && isTableDivider(lines[index + 1]))
    ) {
      paragraphLines.push(lines[index].trim());
      index += 1;
    }
    blocks.push(`<p>${parseInlineMarkdown(paragraphLines.join(" "))}</p>`);
  }

  return blocks.join("");
}

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

function setKeyboardOpen(active) {
  $("#appView").classList.toggle("keyboard-open", Boolean(active));
}

function screenScroller(screen = state.activeScreen) {
  if (screen === "overview") return $("#overviewContent");
  if (screen === "chat") return $("#chatMessages");
  if (screen === "persona") return $("#personaContent");
  return null;
}

function saveScreenScroll(screen = state.activeScreen) {
  const node = screenScroller(screen);
  if (!node) return;
  state.screenScroll[screen] = node.scrollTop;
}

function scrollChatToLatest() {
  const chat = $("#chatMessages");
  chat.scrollTop = chat.scrollHeight;
  state.screenScroll.chat = chat.scrollTop;
}

function restoreScreenScroll(screen = state.activeScreen) {
  const node = screenScroller(screen);
  if (!node) return;
  if (screen === "chat") {
    if (state.screenScroll.chat == null) {
      scrollChatToLatest();
      return;
    }
    node.scrollTop = state.screenScroll.chat;
    return;
  }
  node.scrollTop = state.screenScroll[screen] || 0;
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
  const previousTop = container.scrollTop;
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
  if (state.activeScreen === "overview") {
    container.scrollTop = previousTop;
    state.screenScroll.overview = container.scrollTop;
  }
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
  const previousTop = container.scrollTop;
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
  if (state.activeScreen === "persona") {
    container.scrollTop = previousTop;
    state.screenScroll.persona = container.scrollTop;
  }
}

function runningAssistantMessage(messages = []) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === "assistant" && message.analysis_status === "running") {
      return message;
    }
  }
  return null;
}

function stopSessionStream() {
  if (state.sessionStreamController) {
    state.sessionStreamController.abort();
    state.sessionStreamController = null;
  }
  state.sessionStreamId = null;
  state.sessionStreamMessageId = null;
}

async function consumeEventStream(response, onEvent) {
  const decoder = new TextDecoder();
  const reader = response.body.getReader();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const lines = frame.split("\n");
      let eventName = "message";
      const dataLines = [];
      for (const line of lines) {
        if (line.startsWith("event:")) {
          eventName = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trim());
        }
      }
      if (dataLines.length) {
        onEvent(eventName, JSON.parse(dataLines.join("\n")));
      }
      boundary = buffer.indexOf("\n\n");
    }
    if (done) {
      break;
    }
  }
}

async function connectToRunningSession(message) {
  if (!message?.id || !message.session_id || state.chatStreaming) return;
  if (state.activeScreen !== "chat") return;
  if (state.sessionStreamMessageId === message.id) return;
  stopSessionStream();
  const controller = new AbortController();
  state.sessionStreamController = controller;
  state.sessionStreamId = message.session_id;
  state.sessionStreamMessageId = message.id;
  const knownContentLength = (message.content || "").length;
  const knownThinkingLength = message.content ? 0 : String(message.metadata?.thinking || "").length;
  const lastWorkingNote = message.content ? "" : String(message.metadata?.working_note || "");
  try {
    const params = new URLSearchParams({
      assistant_message_id: String(message.id),
      session_id: String(message.session_id),
      known_content_length: String(knownContentLength),
      known_thinking_length: String(knownThinkingLength),
      last_working_note: lastWorkingNote,
    });
    const response = await fetch(`/api/chat/stream/live?${params.toString()}`, {
      credentials: "same-origin",
      signal: controller.signal,
    });
    if (response.status === 401) {
      showLogin();
      throw new Error("Authentication required");
    }
    if (!response.ok || !response.body) {
      throw new Error(`Request failed: ${response.status}`);
    }

    const messageNodes = Array.from($("#chatMessages").children);
    let workingNode = messageNodes[messageNodes.length - 1] || null;
    let answerStarted = Boolean(message.content);
    let thinkingSeen = knownThinkingLength > 0;

    await consumeEventStream(response, (eventName, payload) => {
      if (controller.signal.aborted) return;
      if (state.sessionId !== message.session_id || state.sessionStreamMessageId !== message.id) return;
      if (eventName === "working_note") {
        if (!workingNode) {
          workingNode = addMessage("assistant", "", true, "working-note");
        }
        setMessageText(workingNode, payload.text || "Working...");
        return;
      }
      if (eventName === "thinking_delta") {
        if (!workingNode) {
          workingNode = addMessage("assistant", "", true, "working-note");
        }
        const delta = payload.text || "";
        if (!delta) {
          return;
        }
        if (!thinkingSeen) {
          thinkingSeen = true;
          const existing = workingNode.textContent.trim();
          workingNode.textContent = existing ? `${existing}\n\n${delta}` : delta;
        } else {
          workingNode.textContent += delta;
        }
        scrollChatToLatest();
        return;
      }
      if (eventName === "answer_start") {
        if (workingNode?.classList.contains("working-note")) {
          workingNode.classList.remove("working-note");
          if (!workingNode.classList.contains("assistant")) {
            workingNode.classList.add("assistant");
          }
          setMessageMarkdown(workingNode, false);
          if (!message.content) {
            setMessageContent(workingNode, "");
          }
        }
        if (!workingNode) {
          workingNode = addMessage("assistant", "", true, "", { markdown: false });
        }
        answerStarted = true;
        return;
      }
      if (eventName === "answer_delta") {
        if (!workingNode) {
          workingNode = addMessage("assistant", "", true, "", { markdown: false });
        }
        if (workingNode.classList.contains("working-note")) {
          workingNode.classList.remove("working-note");
          if (!answerStarted) {
            setMessageContent(workingNode, "");
          }
        }
        answerStarted = true;
        appendMessageContent(workingNode, payload.text || "");
        scrollChatToLatest();
        return;
      }
      if (eventName === "done") {
        setMessageMarkdown(workingNode, true);
        stopSessionStream();
      }
      if (eventName === "error") {
        stopSessionStream();
      }
    });
    if (!controller.signal.aborted) {
      stopSessionStream();
    }
  } catch (error) {
    if (controller.signal.aborted) return;
    console.error(error);
    stopSessionStream();
  }
}

function renderChatHistory(messages) {
  const chat = $("#chatMessages");
  const chatHidden = $("#chatView").hidden;
  chat.innerHTML = "";
  for (const message of messages || []) {
    const isRunningAssistant = message.role === "assistant" && message.analysis_status === "running";
    const text =
      isRunningAssistant && !message.content
        ? message.metadata?.thinking || message.metadata?.working_note || "Working..."
        : message.content;
    const extraClass = isRunningAssistant && !message.content ? "working-note" : "";
    addMessage(message.role === "assistant" ? "assistant" : "user", text, false, extraClass, {
      markdown: message.role === "assistant" && message.analysis_status !== "running",
    });
    state.sessionId = message.session_id || state.sessionId;
  }
  const running = runningAssistantMessage(messages || []);
  state.runningAssistantMessage = running;
  if (running && state.activeScreen === "chat" && !state.chatStreaming) {
    void connectToRunningSession(running);
  } else if (!running) {
    stopSessionStream();
  }
  if (chatHidden) {
    state.screenScroll.chat = null;
    return;
  }
  scrollChatToLatest();
}

function shouldRenderMarkdown(node) {
  return node.classList.contains("assistant") && node.dataset.markdown === "true";
}

function setMessageContent(node, text) {
  if (!node) return;
  const rawText = String(text || "");
  node.dataset.rawText = rawText;
  if (shouldRenderMarkdown(node)) {
    node.innerHTML = `<div class="markdown-body">${renderMarkdown(rawText)}</div>`;
    return;
  }
  node.textContent = rawText;
}

function appendMessageContent(node, text) {
  if (!node) return;
  setMessageContent(node, `${node.dataset.rawText || ""}${text || ""}`);
}

function setMessageMarkdown(node, active) {
  if (!node) return;
  node.dataset.markdown = active ? "true" : "false";
  setMessageContent(node, node.dataset.rawText || "");
}

function addMessage(role, text, scroll = true, extraClass = "", options = {}) {
  const node = document.createElement("article");
  node.className = `message ${role}`;
  if (extraClass) {
    node.classList.add(extraClass);
  }
  node.dataset.markdown = options.markdown ? "true" : "false";
  setMessageContent(node, text);
  $("#chatMessages").appendChild(node);
  if (scroll) {
    scrollChatToLatest();
  }
  return node;
}

function setMessageText(node, text) {
  if (!node) return;
  setMessageContent(node, text);
  scrollChatToLatest();
}

function removeMessage(node) {
  if (node?.parentNode) {
    node.parentNode.removeChild(node);
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
  const loadToken = state.chatLoadToken + 1;
  state.chatLoadToken = loadToken;
  const history = await api(`/api/chat/history?session_id=${sessionId}`);
  if (loadToken !== state.chatLoadToken) return;
  state.draftChat = false;
  state.sessionId = history.session.id;
  renderChatHistory(history.messages || []);
  closeHistory();
  setScreen("chat");
}

function setScreen(screen) {
  saveScreenScroll();
  const previousScreen = state.activeScreen;
  state.activeScreen = ["overview", "chat", "persona"].includes(screen) ? screen : "overview";
  $("#overviewView").hidden = state.activeScreen !== "overview";
  $("#chatView").hidden = state.activeScreen !== "chat";
  $("#personaView").hidden = state.activeScreen !== "persona";
  $("#overviewNavButton").classList.toggle("active", state.activeScreen === "overview");
  $("#chatNavButton").classList.toggle("active", state.activeScreen === "chat");
  $("#personaNavButton").classList.toggle("active", state.activeScreen === "persona");
  if (state.activeScreen !== "chat") {
    setKeyboardOpen(false);
    if (previousScreen === "chat" && !state.chatStreaming) {
      stopSessionStream();
    }
  }
  restoreScreenScroll();
  if (
    state.activeScreen === "chat" &&
    !state.chatStreaming &&
    !state.sessionId &&
    !state.draftChat &&
    !$("#chatMessages").childElementCount
  ) {
    void loadLatestChatSession();
  } else if (state.activeScreen === "chat" && state.runningAssistantMessage && !state.chatStreaming) {
    void connectToRunningSession(state.runningAssistantMessage);
  }
}

async function refreshAppData() {
  const [overview, persona] = await Promise.all([api("/api/overview"), api("/api/persona")]);
  renderOverview(overview);
  renderPersona(persona);
}

async function loadLatestChatSession() {
  const loadToken = state.chatLoadToken + 1;
  state.chatLoadToken = loadToken;
  const sessions = await api("/api/chat/history");
  if (loadToken !== state.chatLoadToken) return;
  state.historySessions = sessions.sessions || [];
  if (!state.historySessions.length) {
    state.draftChat = false;
    $("#chatMessages").innerHTML = "";
    state.sessionId = null;
    state.runningAssistantMessage = null;
    state.screenScroll.chat = 0;
    stopSessionStream();
    return;
  }
  const latest = state.historySessions[0];
  const history = await api(`/api/chat/history?session_id=${latest.id}`);
  if (loadToken !== state.chatLoadToken) return;
  state.draftChat = false;
  state.sessionId = history.session.id;
  renderChatHistory(history.messages || []);
}

async function streamChatResponse(message) {
  const createNewSession = state.draftChat && !state.sessionId;
  stopSessionStream();
  const response = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ message, session_id: state.sessionId, create_new_session: createNewSession }),
  });
  if (response.status === 401) {
    showLogin();
    throw new Error("Authentication required");
  }
  if (!response.ok || !response.body) {
    let detail = {};
    try {
      detail = await response.json();
    } catch {}
    throw new Error(detail.detail || `Request failed: ${response.status}`);
  }

  let workingNode = addMessage("assistant", "Working...", true, "working-note");
  let answerNode = null;
  let terminalError = null;
  let thinkingSeen = false;

  function handleStreamEvent(eventName, payload) {
    if (eventName === "session") {
      state.draftChat = false;
      state.sessionId = payload.session_id || state.sessionId;
      state.runningAssistantMessage = {
        id: payload.assistant_message_id,
        session_id: payload.session_id || state.sessionId,
        content: "",
        metadata: { thinking: "", working_note: "Working..." },
        analysis_status: "running",
      };
      return;
    }
    if (eventName === "working_note") {
      if (!workingNode) {
        workingNode = addMessage("assistant", "", true, "working-note");
      }
      if (thinkingSeen) {
        return;
      }
      if (state.runningAssistantMessage) {
        state.runningAssistantMessage.metadata.working_note = payload.text || "Working...";
      }
      setMessageText(workingNode, payload.text || "Working...");
      return;
    }
    if (eventName === "thinking_delta") {
      if (!workingNode) {
        workingNode = addMessage("assistant", "", true, "working-note");
      }
      const delta = payload.text || "";
      if (!delta) {
        return;
      }
      if (!thinkingSeen) {
        thinkingSeen = true;
        const existing = workingNode.textContent.trim();
        workingNode.textContent = existing ? `${existing}\n\n${delta}` : delta;
      } else {
        workingNode.textContent += delta;
      }
      if (state.runningAssistantMessage) {
        state.runningAssistantMessage.metadata.thinking += delta;
      }
      scrollChatToLatest();
      return;
    }
    if (eventName === "answer_start") {
      removeMessage(workingNode);
      workingNode = null;
      if (!answerNode) {
        answerNode = addMessage("assistant", "", true, "", { markdown: false });
      }
      if (state.runningAssistantMessage) {
        state.runningAssistantMessage.content = "";
      }
      return;
    }
    if (eventName === "answer_delta") {
      if (!answerNode) {
        removeMessage(workingNode);
        workingNode = null;
        answerNode = addMessage("assistant", "", true, "", { markdown: false });
      }
      appendMessageContent(answerNode, payload.text || "");
      if (state.runningAssistantMessage) {
        state.runningAssistantMessage.content += payload.text || "";
      }
      scrollChatToLatest();
      return;
    }
    if (eventName === "error") {
      terminalError = payload.message || "Streaming failed.";
      removeMessage(workingNode);
      workingNode = null;
      if (!answerNode) {
        answerNode = addMessage("assistant", terminalError, true, "", { markdown: false });
      } else {
        setMessageText(answerNode, terminalError);
      }
      state.runningAssistantMessage = null;
      return;
    }
    if (eventName === "done") {
      setMessageMarkdown(answerNode, true);
      state.runningAssistantMessage = null;
    }
  }

  await consumeEventStream(response, handleStreamEvent);
  if (terminalError) {
    throw new Error(terminalError);
  }
}

function startNewChat() {
  stopSessionStream();
  state.chatLoadToken += 1;
  state.draftChat = true;
  state.sessionId = null;
  state.runningAssistantMessage = null;
  state.screenScroll.chat = 0;
  $("#chatMessages").innerHTML = "";
  closeHistory();
  setScreen("chat");
  $("#chatInput").focus();
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
$("#newChatButton").addEventListener("click", () => startNewChat());
$("#chatInput").addEventListener("focus", () => setKeyboardOpen(true));
$("#chatInput").addEventListener("blur", () => setKeyboardOpen(false));

$("#overviewContent").addEventListener("scroll", () => saveScreenScroll("overview"));
$("#chatMessages").addEventListener("scroll", () => saveScreenScroll("chat"));
$("#personaContent").addEventListener("scroll", () => saveScreenScroll("persona"));

$("#chatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  const message = $("#chatInput").value.trim();
  if (!message || state.chatStreaming) return;
  $("#chatInput").value = "";
  addMessage("user", message);
  state.chatStreaming = true;
  button.disabled = true;
  $("#chatInput").disabled = true;
  try {
    await streamChatResponse(message);
    await refreshAppData();
  } catch (error) {
    addMessage("assistant", String(error.message || "Streaming failed."));
  } finally {
    state.chatStreaming = false;
    button.disabled = false;
    $("#chatInput").disabled = false;
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
