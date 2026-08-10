/* Friday–Δ UI — chat-first, panels as views, generative cards, voice, live SSE */
"use strict";

/* ============================== helpers ============================== */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtMoney = (n) => "$" + (Number(n) || 0).toFixed(4);
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString();
const fmtDate = (ts) => new Date(ts * 1000).toLocaleDateString() + " " + fmtTime(ts);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

/* markdown-lite renderer: code, bold, italic, links, lists, tables, headers.
   Line-based single pass — no nested <ul>/<ol> re-wrapping bugs, no empty
   list items, tables parsed properly, "---" dropped (model uses them as
   essay dividers). */
function md(text) {
  if (!text) return "";
  let t = esc(text);
  const blocks = [];
  t = t.replace(/```([\s\S]*?)```/g, (_, c) => { blocks.push(`<pre>${c.trim()}</pre>`); return `\u0000${blocks.length - 1}\u0000`; });
  t = t.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  t = t.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  t = t.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>");
  t = t.replace(/\[([^\]\n]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const isTableRow = (l) => /^\s*\|.*\|\s*$/.test(l);
  const lines = t.split("\n");
  let out = "", inUl = false, inOl = false, listBuf = [];
  const flushList = () => {
    if (listBuf.length) out += `<${inUl ? "ul" : "ol"}>${listBuf.join("")}</${inUl ? "ul" : "ol"}>`;
    listBuf = []; inUl = false; inOl = false;
  };
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i], l = raw.trim();
    if (!l) { flushList(); continue; }
    if (isTableRow(l)) {
      const rows = [];
      while (i < lines.length && isTableRow(lines[i])) { rows.push(lines[i].trim()); i++; }
      i--;
      if (rows.length >= 2) {
        const sep = /^\|?[\s:|-]+\|?$/.test(rows[1].replace(/\|/g, "")) ? 1 : -1;
        const split = (r) => r.replace(/^\||\|$/g, "").split("|").map(c => c.trim());
        const head = split(rows[0]);
        const body = (sep >= 0 ? rows.slice(2) : rows.slice(1)).map(split);
        if (head.length && head.some(h => h)) {
          flushList();
          out += "<table><thead><tr>" + head.map(h => `<th>${h}</th>`).join("") + "</tr></thead><tbody>" +
            body.map(r => "<tr>" + r.map(c => `<td>${c}</td>`).join("") + "</tr>").join("") + "</tbody></table>";
          continue;
        }
      }
      // degenerate pipe line — fall through as plain paragraph
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(l);
    if (h) { flushList(); out += `<h4>${h[2]}</h4>`; continue; }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(l)) { flushList(); continue; }
    // stray empty bullet markers ("- " alone) — model artifact, drop silently
    if (/^[-*•]\s*$/.test(l)) { continue; }
    const ul = /^[-*•]\s+(\S.*)$/.exec(l);
    const ol = /^(\d+)[.)]\s+(\S.*)$/.exec(l);
    if (ul) {
      if (!inUl) { flushList(); inUl = true; }
      listBuf.push(`<li>${ul[1]}</li>`);
      continue;
    }
    if (ol) {
      if (!inOl) { flushList(); inOl = true; }
      listBuf.push(`<li>${ol[2]}</li>`);
      continue;
    }
    flushList();
    out += `<p>${l}</p>`;
  }
  flushList();
  return out.replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.innerHTML = msg;
  $("#toast-stack").appendChild(el);
  setTimeout(() => el.remove(), 7000);
}

/* ============================== app state ============================== */
const state = {
  view: "chat",
  chat: [],
  streaming: false,
  tasks: [],
  books: [],
  atoms: [],
  claims: [],
  tensions: [],
  loops: [],
  constraints: [],
  params: { defaults: {}, live: {} },
  editing: { key: null, val: null },
  focus: { active: null, ends: 0, total: 25 },
};

/* ============================== navigation ============================== */
$$(".nav-item").forEach(btn => btn.addEventListener("click", () => switchView(btn.dataset.view)));
function switchView(v) {
  state.view = v;
  $$(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === v));
  $$(".view").forEach(s => s.classList.toggle("active", s.id === "view-" + v));
  if (v === "tasks") loadTasks();
  if (v === "memory") loadMemory();
  if (v === "focus") loadFocus();
  if (v === "books") loadBooks();
  if (v === "admin") loadAdmin();
  if (v === "chat") $("#composer-input").focus();
}

/* ============================== chat engine ============================== */
let streamingReply = "";   // module-level buffer for the in-flight reply

function addMsg(role, html, opts = {}) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + role;
  const who = role === "user" ? "YOU" : "FRIDAY";
  const ts = opts.ts ? `<span class="ts">${new Date(opts.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>` : "";
  const copy = role === "friday" ? `<button class="copy-btn" title="Copy reply" data-raw="${esc(opts.raw || "")}">⧉</button>` : "";
  wrap.innerHTML = `<div class="who"><span class="who-dot"></span>${who}${copy}${ts}</div><div class="bubble">${html}</div>`;
  $("#chat-stream").appendChild(wrap);
  scrollChat();
  return wrap;
}
/* copy-to-clipboard for assistant replies (delegated — works for restored too) */
document.addEventListener("click", (e) => {
  const b = e.target.closest(".copy-btn");
  if (!b) return;
  const raw = b.dataset.raw || "";
  const done = () => { b.textContent = "✓"; setTimeout(() => { b.textContent = "⧉"; }, 1200); };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(raw).then(done).catch(() => {});
  else { const ta = document.createElement("textarea"); ta.value = raw; document.body.appendChild(ta); ta.select(); try { document.execCommand("copy"); done(); } catch (err) {} ta.remove(); }
});
function scrollChat() { const el = $("#chat-scroll"); if (el) el.scrollTop = el.scrollHeight; }

/* restore previous chats + panels after any refresh — hard refresh must feel
   like nothing was lost */
function dayLabel(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const same = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (same(d, now)) return "Today";
  const yest = new Date(now.getTime() - 86400000);
  if (same(d, yest)) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}
function addDayDivider(label) {
  const div = document.createElement("div");
  div.className = "chat-day";
  div.textContent = label;
  $("#chat-stream").appendChild(div);
}
async function restoreChat() {
  try {
    const r = await api("/api/chat/history?limit=50");
    const turns = (r.turns || []).filter(t => t.user_text && t.reply);
    if (turns.length) {
      let lastDay = "";
      for (const t of turns) {
        const ts = t.created_ts || 0;
        const day = ts ? dayLabel(ts) : "";
        if (day && day !== lastDay) { addDayDivider(day); lastDay = day; }
        addMsg("user", md(t.user_text), { ts });
        addMsg("friday", md(t.reply), { ts, raw: t.reply });
        state.chat.push(t);
      }
      scrollChat();
    } else {
      addWelcome();
    }
  } catch (e) {
    addWelcome();
  }
}
function addWelcome() {
  addMsg("friday", md("**Friday–Δ is live.** One chat for everything — research, tasks, memory, focus, books, settings. Ask me anything, or try: *\"research phone undr 20k and draft mail to sarh\"*, *\"dark mode kar do\"*, *\"start focos 25m allow github\"*."));
}

function addCard(card) { renderCard(card); }

function typingIndicator() {
  const wrap = document.createElement("div");
  wrap.className = "msg friday";
  wrap.id = "typing";
  wrap.innerHTML = `<div class="who">FRIDAY</div><div class="bubble"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></div>`;
  $("#chat-stream").appendChild(wrap);
  scrollChat();
  return wrap;
}

async function sendChat(text) {
  text = (text || "").trim();
  if (!text || state.streaming) return;
  state.streaming = true;
  streamingReply = "";
  addMsg("user", md(text), { ts: Date.now() / 1000 });
  const typing = typingIndicator();
  let replyEl = null;
  const corr = "cor_" + Math.random().toString(16).slice(2, 10);
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, corr_id: corr }),
    });
    if (!res.ok || !res.body) throw new Error("chat failed " + res.status);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let done = false;
    while (!done) {
      const { value, done: d } = await reader.read();
      done = d;
      buf += dec.decode(value || new Uint8Array(), { stream: !done });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
        for (const line of chunk.split("\n")) {
          if (!line.startsWith("data:")) continue;
          let ev; try { ev = JSON.parse(line.slice(5)); } catch (e) { continue; }
          handleChatEvent(ev, typing);
        }
      }
    }
  } catch (e) {
    typing.remove();
    addMsg("friday", `<p>⚠ ${esc(e.message)}</p>`);
  }
  state.streaming = false;
}

function handleChatEvent(ev, typing) {
  switch (ev.type) {
    case "sense": {
      $("#chat-meta").textContent = `· sense ${ev.sense_ms}ms · conf ${(ev.confidence * 100).toFixed(0)}% · ${ev.slots.length} slots`;
      break;
    }
    case "ctrl": {
      if (ev.ctrl.config_deltas && ev.ctrl.config_deltas["ui.theme"]) applyTheme(ev.ctrl.config_deltas["ui.theme"]);
      break;
    }
    case "delta": {
      if (!typing.isConnected) { typing = typingIndicator(); }
      const bubble = typing.querySelector(".bubble");
      if (!bubble) return;
      // filter out raw ctrl JSON blocks that the model sometimes emits
      let t = ev.text || "";
      t = t.replace(/^```json\s*/, "");
      if (t.trim().startsWith("{")) {
        const first = t.split("\n")[0];
        if (first.includes("ctrl") && (first.includes("depth") || first.includes("config_deltas") || first.includes("memory_writes"))) {
          const nl = t.indexOf("\n");
          t = nl >= 0 ? t.slice(nl) : "";
        }
      }
      if (!t) return;
      streamingReply += t;
      bubble.innerHTML = md(streamingReply);
      scrollChat();
      break;
    }
    case "card": renderCard(ev.card); break;
    case "warning": {
      toast(`⚠ ${esc(ev.message)}`, "bad");
      break;
    }
    case "ask": {
      toast(`❓ ${esc(ev.question)}`, "chrome");
      renderCard({ type: "ask", question: ev.question });
      break;
    }
    case "task_event": handleTaskEvent(ev); break;
    case "done": {
      typing.remove();
      $("#chat-meta").textContent = `· ${ev.model} · ${ev.latency_ms}ms · ${fmtMoney(ev.cost_usd)} · ${ev.slots_used} slots`;
      const final = ev.reply || streamingReply || "";
      addMsg("friday", md(final), { ts: Date.now() / 1000, raw: final });
      state.chat.push({ role: "friday", text: final });
      streamingReply = "";
      break;
    }
  }
}

/* ============================== cards ============================== */
function renderCard(card) {
  const wrap = document.createElement("div");
  wrap.className = "msg friday";
  let html = "";
  switch (card.type) {
    case "theme": html = `<div class="card"><h3>🎨 Theme</h3><div class="card-row"><span class="chip ${card.theme === "dark" ? "" : "good"}">${esc(card.theme)} mode applied — no reload</span></div></div>`; break;
    case "task_chip": html = `<div class="card"><h3>🧩 Jobs</h3><div class="card-row"><span class="chip">${card.jobs} job${card.jobs > 1 ? "s" : ""} running in background ▸</span><button class="mini-btn" onclick="switchView('tasks')">open Tasks</button></div></div>`; break;
    case "approvals": html = `<div class="card"><h3>🛡 Approvals</h3>` + (card.items || []).map(a => `<div class="card-row" style="margin:6px 0"><span class="chip warn">${esc(a.kind)}</span><b>${esc(a.title)}</b><button class="mini-btn primary" onclick="approveTask(${a.task_id})">Approve</button><button class="mini-btn bad" onclick="rejectTask(${a.task_id})">Reject</button></div>`).join("") + `</div>`; break;
    case "ask": html = `<div class="card"><h3>❓ One question</h3><p>${esc(card.question || "")}</p></div>`; break;
    case "prefire": html = ""; break;  // silent — results are used, not announced
    case "belief": html = `<div class="card"><h3>🧠 Belief</h3><div class="card-row"><span class="chip alpha">α ${card.alpha}</span><span class="chip beta">β ${card.beta}</span><span class="chip">conf ${(card.confidence * 100).toFixed(0)}%</span></div><p style="margin-top:6px">${esc(card.statement)}</p><div class="card-row" style="margin-top:8px"><button class="mini-btn good" onclick="rateClaim(${card.claim_id},'up')">👍</button><button class="mini-btn bad" onclick="rateClaim(${card.claim_id},'down')">👎</button><button class="mini-btn" onclick="editClaim(${card.claim_id})">✎ edit</button></div></div>`; break;
    case "undo": html = `<div class="card"><div class="card-row"><span class="chip good">✓ reversible</span><span class="undo-chip" onclick="undoLast(${card.task_id || 0})">↩ Undo ${card.seconds || 89}s</span></div></div>`; break;
    case "provider_key": html = `<div class="card"><h3>🔑 API key</h3><div class="card-row"><span class="chip good">${esc(card.scope)}</span><span class="chip">${esc(card.masked)}</span></div></div>`; break;
    case "focus": {
      if (card.action === "start") {
        state.focus.active = { session_id: card.session_id, target_min: card.minutes,
                               start_ts: Date.now() / 1000, drift_count: 0 };
        state.focus.total = card.minutes;
        state.focus.ends = Date.now() / 1000 + card.minutes * 60;
        armNotifications();
      } else if (card.action === "stop") {
        state.focus.active = null;
      }
      html = `<div class="card"><h3>🎯 Focus</h3><div class="card-row"><span class="chip good">${esc(card.message || "")}</span></div></div>`;
      break;
    }
    case "book_sources": html = `<div class="card"><h3>📚 Sources</h3><div class="card-row">${(card.chunks || []).map(c => `<span class="chip">p${c.page}</span>`).join("")}</div></div>`; break;
    case "tracker": html = `<div class="card"><h3>📡 Tracker #${card.tracker_id}</h3>
      <div class="card-row"><span class="chip good">active</span><span class="chip">every ${Math.round(card.frequency_mins / 60)}h</span></div>
      <p style="margin-top:6px">${esc(card.query)}</p>
      <button class="mini-btn" onclick="switchView('tasks')">view in Tasks</button></div>`; break;
    case "note": html = `<div class="card"><h3>💡</h3><p>${esc(card.text || "")}</p></div>`; break;
    case "task_created": html = `<div class="card"><h3>🧩 Task #${card.task_id}</h3><div class="card-row"><span class="chip">${esc(card.status)}</span><button class="mini-btn" onclick="switchView('tasks')">open Tasks</button></div></div>`; break;
    default: html = `<div class="card"><pre>${esc(JSON.stringify(card))}</pre></div>`;
  }
  wrap.innerHTML = `<div class="who">FRIDAY</div>` + html;
  $("#chat-stream").appendChild(wrap);
  scrollChat();
}

async function rateClaim(claimId, dir) {
  await api("/api/memory/rate", { method: "POST", body: JSON.stringify({ claim_id: claimId, direction: dir }) });
  toast(dir === "up" ? "Belief reinforced (α+)" : "Belief weakened (β+)");
  loadMemory();
}
function editClaim(claimId) {
  const c = state.claims.find(x => x.claim_id === claimId);
  if (!c) return;
  const s = prompt("Edit belief:", c.statement);
  if (s && s !== c.statement) {
    api("/api/memory/claim/edit", { method: "POST", body: JSON.stringify({ claim_id: claimId, statement: s }) })
      .then(() => { toast("Belief updated (append, not overwrite)"); loadMemory(); });
  }
}
async function approveTask(id) { const r = await api(`/api/tasks/${id}/approve`, { method: "POST" }); toast("Approved — DAG resuming"); loadTasks(); }
async function rejectTask(id) { await api(`/api/tasks/${id}/reject`, { method: "POST" }); toast("Rejected"); loadTasks(); }
async function undoLast(taskId) { const r = await api("/api/tasks/" + (taskId || "0") + "/undo", { method: "POST" }); toast(r.ok ? "↩ undone: " + esc(r.what) : "nothing reversible to undo"); }

/* ============================== task events ============================== */
function handleTaskEvent(ev) {
  if (ev.type === "task") { toast(`🧩 Task #${ev.task_id} ${ev.status}`, "chrome"); }
  if (ev.type === "task_event" && ev.task_event && ev.task_event.type === "done") { toast(`✅ Task #${ev.task_event.task_id} completed`, "good"); }
  if (ev.type === "approval") { toast(`🛡 Approval needed: ${esc(ev.gate)}`, "chrome"); renderCard({ type: "approvals", items: [{ task_id: ev.task_id, kind: ev.gate, title: "approval required" }] }); }
  if (ev.type === "step_failed") { toast(`❌ step failed: ${esc(ev.error)}`, "bad"); }
}

/* ============================== composer ============================== */
const input = $("#composer-input");
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(input.value); input.value = ""; input.style.height = "auto"; }
});
input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(140, input.scrollHeight) + "px"; });
$("#btn-send").addEventListener("click", () => { sendChat(input.value); input.value = ""; input.style.height = "auto"; });

/* ============================== voice ============================== */
let rec = null, recActive = false, synthOn = true;
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

$("#btn-voice").addEventListener("click", () => {
  if (recActive) stopRec();
  else startRec();
});

function startRec() {
  if (!SR) { toast("Speech recognition not supported in this browser — use Chrome"); return; }
  // barge-in: stop any TTS instantly, locally, zero RTT
  if (speechSynthesis.speaking) { speechSynthesis.cancel(); }
  rec = new SR();
  rec.lang = "en-IN";
  rec.interimResults = true;
  rec.continuous = false;
  $("#composer-voice").classList.remove("hidden");
  $("#btn-voice").classList.add("rec");
  recActive = true;
  let finalText = "";
  rec.onresult = (e) => {
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) finalText += e.results[i][0].transcript + " ";
      else interim += e.results[i][0].transcript;
    }
    $("#voice-hint").textContent = (finalText + interim).trim() || "listening…";
  };
  rec.onerror = () => stopRec();
  rec.onend = () => {
    $("#composer-voice").classList.add("hidden");
    $("#btn-voice").classList.remove("rec");
    recActive = false;
    if (finalText.trim()) {
      input.value = finalText.trim();
      sendChat(finalText.trim());
      input.value = "";
    }
  };
  rec.start();
}
function stopRec() { if (rec) { rec.stop(); } }

/* TTS for replies (opt-in per reply: click 🔊 next to a Friday message) */
function speak(text) {
  if (!synthOn) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text.replace(/<[^>]+>/g, " ").slice(0, 400));
  u.lang = "en-IN"; u.rate = 1.02;
  speechSynthesis.speak(u);
}

/* ============================== tasks panel ============================== */
async function loadTasks() {
  try {
    const data = await api("/api/tasks");
    state.tasks = data.tasks;
    renderKanban(data.tasks);
    renderTrackers(data.trackers || []);
  } catch (e) { toast("tasks: " + esc(e.message)); }
}
function renderTrackers(trackers) {
  const el = document.getElementById("tracker-list");
  if (!el) return;
  el.innerHTML = trackers.map(t => `
    <div class="atom-card"><div class="a-main">
      <div class="a-text">📡 #${t.tracker_id} — ${esc(t.query)}</div>
      <div class="a-meta"><span class="kind-pill">${esc(t.kind)}</span>
        <span>every ${Math.round(t.frequency_mins / 60)}h</span>
        <span class="chip ${t.status === "active" ? "good" : "bad"}">${esc(t.status)}</span>
        <span>${fmtDate(t.created_ts)}</span></div>
    </div></div>`).join("") || `<div class="dim">no trackers yet</div>`;
}
const COLUMNS = [["queued", "Queued"], ["running", "Running"], ["waiting_approval", "Approval"], ["completed", "Completed"], ["failed", "Failed"]];
function renderKanban(tasks) {
  const kb = $("#task-kanban");
  kb.innerHTML = COLUMNS.map(([key, label]) => {
    const items = tasks.filter(t => t.status === key);
    return `<div class="kanban-col"><h4>${label} (${items.length})</h4>` +
      items.map(t => `<div class="task-card" onclick="openTask(${t.task_id})">
        <div class="t-title">${esc(t.title)}</div>
        <div class="t-meta">#${t.task_id} · ${fmtDate(t.created_ts)} · ${fmtMoney(t.cost_usd)}</div>
        ${t.approval_kind ? `<div class="t-meta"><span class="task-status waiting_approval">${esc(t.approval_kind)}</span></div>` : ""}
      </div>`).join("") + `</div>`;
  }).join("");
}
function openTask(id) {
  const t = state.tasks.find(x => x.task_id === id);
  if (!t) return;
  const d = $("#task-detail");
  d.classList.remove("hidden");
  d.innerHTML = `<button class="btn" onclick="$('#task-detail').classList.add('hidden')">✕ close</button>
    <h2 style="margin:10px 0">#${t.task_id} — ${esc(t.title)}</h2>
    <div class="card-row" style="margin-bottom:10px">
      <span class="task-status ${esc(t.status)}">${esc(t.status)}</span>
      <span class="chip">${esc(t.task_type)}</span>
      <span class="chip">${esc(t.autonomy)}</span>
      <span class="chip">${fmtMoney(t.cost_usd)}</span>
    </div>
    <p class="dim">${esc(t.description || "")}</p>
    ${t.status === "waiting_approval" ? `<div class="card-row" style="margin:10px 0"><button class="mini-btn primary" onclick="approveTask(${t.task_id})">Approve</button><button class="mini-btn bad" onclick="rejectTask(${t.task_id})">Reject</button></div>` : ""}
    <h3 style="margin:14px 0 6px">Steps</h3>
    ${(t.steps || []).map(s => `<div class="step-row">
      <div class="s-head"><span>${s.step_index + 1}. ${esc(s.description || s.tool_name)}</span><span class="task-status ${esc(s.status)}">${esc(s.status)}</span></div>
      ${s.output_summary ? `<pre>${esc(s.output_summary.slice(0, 600))}</pre>` : ""}
      ${s.error ? `<pre style="color:var(--bad)">${esc(s.error)}</pre>` : ""}
    </div>`).join("") || "no steps"}
    <h3 style="margin:14px 0 6px">Artifacts</h3>
    ${(t.artifacts || []).map(a => `<div class="card-row" style="margin:4px 0"><a class="mini-btn" href="/api/artifacts/${a.artifact_id}" download>⬇ ${esc(a.title)}</a><span class="dim">${esc(a.mime)}</span></div>`).join("") || "none"}
  `;
}
$("#tasks-refresh").addEventListener("click", loadTasks);

/* ============================== memory panel ============================== */
const KINDS = ["fact", "preference", "pattern", "goal", "episodic", "emotional", "procedure", "correction", "observation"];
KINDS.forEach(k => $("#atom-kind").append(new Option(k, k)));

async function loadMemory() {
  const [atoms, claims, tensions, loops, constraints] = await Promise.all([
    api("/api/memory/atoms"), api("/api/memory/claims"), api("/api/memory/tensions"),
    api("/api/memory/open_loops"), api("/api/memory/constraints"),
  ]);
  state.atoms = atoms.atoms; state.claims = claims.claims;
  state.tensions = tensions.tensions; state.loops = loops.loops; state.constraints = constraints.constraints;
  renderAtoms(state.atoms);
  renderBeliefs(state.claims);
  renderTensions(state.tensions);
  renderLoops(state.loops);
  renderConstraints(state.constraints);
  loadGraph();
}
function renderAtoms(atoms) {
  $("#atom-list").innerHTML = atoms.map(a => `
    <div class="atom-card">
      <div class="a-main">
        <div class="a-text">${esc(a.text)}</div>
        <div class="a-meta">
          <span class="kind-pill ${esc(a.kind)}">${esc(a.kind)}</span>
          <span>importance ${a.importance.toFixed(2)}</span>
          <span>strength ${a.strength.toFixed(2)}</span>
          <span>${fmtDate(a.created_ts)}</span>
          <span>${a.access_count} recalls</span>
        </div>
      </div>
      <div class="card-row">
        <button class="mini-btn good" onclick="rateAtom(${a.atom_id},'up')">👍</button>
        <button class="mini-btn bad" onclick="rateAtom(${a.atom_id},'down')">👎</button>
        <button class="mini-btn" onclick="editAtom(${a.atom_id})">✎</button>
        <button class="mini-btn bad" onclick="deleteAtom(${a.atom_id})">🗑</button>
      </div>
    </div>`).join("") || `<div class="dim">no atoms yet — talk to Friday and they'll appear here</div>`;
}
async function rateAtom(id, dir) { await api("/api/memory/rate", { method: "POST", body: JSON.stringify({ atom_id: id, direction: dir }) }); loadMemory(); }
function editAtom(id) { const a = state.atoms.find(x => x.atom_id === id); if (!a) return; const s = prompt("Edit memory:", a.text); if (s && s !== a.text) api("/api/memory/edit", { method: "POST", body: JSON.stringify({ atom_id: id, text: s }) }).then(loadMemory); }
async function deleteAtom(id) { if (!confirm("Delete this memory (append-only, reversible)?")) return; await api("/api/memory/delete", { method: "POST", body: JSON.stringify({ atom_id: id }) }); loadMemory(); }

function renderBeliefs(claims) {
  $("#belief-list").innerHTML = claims.map(c => `
    <div class="atom-card">
      <div class="a-main">
        <div class="a-text">${esc(c.statement)}</div>
        <div class="a-meta">
          <span class="kind-pill">${esc(c.category)}</span>
          <span class="chip alpha">α ${c.alpha}</span><span class="chip beta">β ${c.beta}</span>
          <span>conf ${(c.confidence * 100).toFixed(0)}%</span>
          <span>stability ${c.stability.toFixed(2)}</span>
          ${c.user_edited ? `<span class="chip good">user-edited</span>` : ""}
        </div>
      </div>
      <div class="card-row">
        <div class="conf-bar"><i style="width:${(c.confidence * 100).toFixed(0)}%"></i></div>
        <button class="mini-btn good" onclick="rateClaim(${c.claim_id},'up')">👍</button>
        <button class="mini-btn bad" onclick="rateClaim(${c.claim_id},'down')">👎</button>
        <button class="mini-btn" onclick="editClaim(${c.claim_id})">✎</button>
      </div>
    </div>`).join("") || `<div class="dim">no beliefs yet</div>`;
}
function renderTensions(tensions) {
  $("#tension-list").innerHTML = tensions.map(t => `
    <div class="atom-card">
      <div class="a-main">
        <div class="a-text">⚡ <b>${esc(t.a)}</b> vs <b>${esc(t.b)}</b></div>
        <div class="a-meta"><span class="chip warn">strength ${t.strength}</span><span class="chip">balance ${t.balance}</span></div>
      </div>
      <button class="mini-btn primary" onclick="resolveTension(${t.tension_id})">resolve</button>
    </div>`).join("") || `<div class="dim">no open tensions — the immune system is quiet</div>`;
}
async function resolveTension(id) { await api(`/api/memory/tensions/${id}/resolve`, { method: "POST", body: JSON.stringify({}) }); loadMemory(); }
function renderLoops(loops) {
  $("#loop-list").innerHTML = loops.map(l => `
    <div class="atom-card">
      <div class="a-main"><div class="a-text">${esc(l.text)}</div>
        <div class="a-meta"><span class="kind-pill">${esc(l.kind)}</span><span>priority ${l.priority}</span><span>${fmtDate(l.created_ts)}</span></div></div>
    </div>`).join("") || `<div class="dim">no open loops — nothing pending</div>`;
}
function renderConstraints(constraints) {
  $("#constraint-list").innerHTML = constraints.map(c => `
    <div class="atom-card"><div class="a-main"><div class="a-text">🚫 ${esc(c.text)}</div>
      <div class="a-meta"><span class="chip">source: ${esc(c.source)}</span><span class="chip">ttl: ${c.ttl_days < 0 ? "forever" : c.ttl_days + "d"}</span></div></div></div>`
  ).join("") || `<div class="dim">no constraints — always-injected ledger is empty</div>`;
}
$("#atom-search").addEventListener("input", () => {
  const q = $("#atom-search").value.toLowerCase();
  renderAtoms(state.atoms.filter(a => !q || a.text.toLowerCase().includes(q)));
});
$("#atom-kind").addEventListener("change", () => {
  const k = $("#atom-kind").value;
  renderAtoms(state.atoms.filter(a => !k || a.kind === k));
});
async function loadGraph() {
  try {
    const g = await api("/api/memory/graph");
    const ents = g.entities.slice(0, 30);
    $("#graph-list").innerHTML = `<h4 style="margin:8px 0">Entities</h4><div class="cards">` + ents.map(e =>
      `<div class="atom-card"><div class="a-main"><div class="a-text">${esc(e.name)}</div><div class="a-meta"><span class="kind-pill">${esc(e.kind)}</span><span>importance ${e.importance.toFixed(2)}</span></div></div></div>`).join("") + `</div>
      <h4 style="margin:14px 0 6px">Edges (${g.edges.length})</h4><div class="cards">` + g.edges.slice(0, 40).map(e =>
      `<div class="atom-card"><div class="a-main"><div class="a-text">${esc(e.src)} —<b>${esc(e.relation)}</b>→ ${esc(e.dst)}</div><div class="a-meta"><span class="chip">w ${e.weight}</span></div></div></div>`).join("") + `</div>`;
  } catch (e) { $("#graph-list").innerHTML = "graph: " + esc(e.message); }
}
$("#explain-go").addEventListener("click", async () => {
  const q = $("#explain-q").value;
  if (!q) return;
  const r = await api("/api/memory/explain?q=" + encodeURIComponent(q));
  $("#explain-out").innerHTML = r.explain.map(s => `
    <div class="atom-card"><div class="a-main">
      <div class="a-text">${esc(s.text)}</div>
      <div class="a-meta"><span class="chip">${esc(s.type)}</span><span class="chip">score ${s.score}</span></div>
      <div class="a-meta">I remembered that because: ${(s.why || []).map(w => `<span class="chip good">${esc(w)}</span>`).join(" ")}</div>
    </div></div>`).join("") + `<div class="dim" style="margin-top:8px">recall confidence: ${(r.confidence * 100).toFixed(0)}%</div>`;
});
$("#sql-go").addEventListener("click", async () => {
  const sql = $("#sql-q").value;
  if (!sql) return;
  try {
    const r = await api("/api/memory/agent_sql", { method: "POST", body: JSON.stringify({ sql }) });
    $("#sql-out").innerHTML = `<div class="card"><div class="dim" style="margin-bottom:6px">${r.count} rows</div><table class="data">` +
      (r.rows.length ? `<thead><tr>${Object.keys(r.rows[0]).map(k => `<th>${esc(k)}</th>`).join("")}</tr></thead>` : "") +
      `<tbody>${r.rows.slice(0, 50).map(row => `<tr>${Object.values(row).map(v => `<td>${esc(String(v).slice(0, 120))}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  } catch (e) { $("#sql-out").innerHTML = `<div class="card" style="color:var(--bad)">${esc(e.message)}</div>`; }
});

/* ============================== focus panel — BODY DOUBLE · ADHD-FRIENDLY ============================== */
/* Friday works ALONGSIDE you: presence, timer, pomodoro work/break cycles,
   micro-commitments, milestone check-ins, drift forgiveness, rewards, ambient
   sound, chimes. Built for ADHD brains: tiny starts, one task, come-back
   without shame. */
state.focus.companionLog = [];     // companion lines for the current session
state.focus.lastMilestone = 0;
state.focus.lastSessionId = null;  // the session we just ended (for summary)
state.focus.stats = null;
state.focus.onBreak = false;       // pomodoro break in progress
state.focus.breakEnd = 0;
state.focus.round = 1;             // pomodoro round number
state.focus.comebacks = 0;         // times you came back after a drift (celebrated!)

/* warm, human lines — rotated so Friday never repeats itself */
const BD_LINES = {
  start: [
    "I'm here with you. Same room, same timer. Let's work.",
    "We're in this together — starting now. No pressure, just begin.",
    "Okay, together: one small step, then the next. I'm right here.",
  ],
  checkin: [
    "Still here with you. Keep going — you're doing great.",
    "Nice and steady. The work is happening.",
    "I can see the progress. Keep the momentum.",
    "You're in it. That's the hard part — done.",
  ],
  milestone: {
    1: "Quarter in — nice. You've started, that's the win.",
    2: "Halfway! You're doing the thing. Reward's getting closer.",
    3: "Almost done — one last push. I'm right here.",
  },
  back: [
    "Welcome back 🧡 Resetting the clock — you've got this.",
    "Back on track! That comeback just counted. We move.",
    "There you are. No guilt, just momentum again.",
  ],
  drift: [
    "You drifted to {d} — no judgment, come back when you're ready.",
    "Hey, noticed {d}. It happens. Take a breath, come back.",
    "Wandering is normal. Let's settle back in — I'm here.",
  ],
  break: [
    "Break time ☕ Stand up, shake out your hands, look at something far away. You earned it.",
    "Pause — water, stretch, blink. Your brain needs the reset.",
    "Break! Walk a lap around the room. I'll keep your spot warm.",
  ],
  breakOver: [
    "Break's over — ready for round {n}? No pressure. Just start.",
    "Back to it whenever you're ready, round {n}. I'm here.",
    "Round {n} time. Tiny step first. Let's go.",
  ],
  end: [
    "That's a wrap 🎉 {m} min focused{t}, {d} drift{s}. You showed up. That's what counts.",
    "Done — {m} minutes{t}. Look at you. Rest, then reward.",
  ],
};

function bdSay(msg, kind = "") {
  state.focus.companionLog.push({ msg, kind, ts: Date.now() });
  renderCompanionLog();
}
function bdRand(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

/* ---------------- sound: chimes + ambient brown noise ---------------- */
let ambient = { on: false, ctx: null };
function chime(kind) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const notes = {
      start:   [[660, 0, .09]],
      warn:    [[880, 0, .08], [880, .13, .08]],
      workEnd: [[880, 0, .1], [880, .16, .1], [880, .32, .18]],
      breakEnd:[[660, 0, .1], [880, .16, .18]],
    }[kind] || [[660, 0, .1]];
    notes.forEach(([f, at, dur]) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, ctx.currentTime + at);
      g.gain.exponentialRampToValueAtTime(0.12, ctx.currentTime + at + .02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + at + dur);
      o.connect(g); g.connect(ctx.destination);
      o.start(ctx.currentTime + at); o.stop(ctx.currentTime + at + dur + .05);
    });
    setTimeout(() => ctx.close(), 1200);
  } catch (e) {}
}
function toggleAmbient(on) {
  const want = on !== undefined ? on : !ambient.on;
  if (want) {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const len = ctx.sampleRate * 2;
      const buf = ctx.createBuffer(1, len, ctx.sampleRate);
      const d = buf.getChannelData(0);
      let last = 0;
      for (let i = 0; i < len; i++) {
        const w = Math.random() * 2 - 1;
        last = (last + 0.02 * w) / 1.02;
        d[i] = last * 3.5;
      }
      const src = ctx.createBufferSource(); src.buffer = buf; src.loop = true;
      const f = ctx.createBiquadFilter(); f.type = "lowpass"; f.frequency.value = 900;
      const g = ctx.createGain(); g.gain.value = 0.22;
      src.connect(f); f.connect(g); g.connect(ctx.destination);
      src.start();
      ambient = { on: true, ctx };
      localStorage.setItem("friday.ambient", "1");
    } catch (e) {}
  } else if (ambient.ctx) {
    try { ambient.ctx.close(); } catch (e) {}
    ambient = { on: false, ctx: null };
    localStorage.setItem("friday.ambient", "0");
  }
}

/* ---------------- session lifecycle ---------------- */
async function startFocus(o = {}) {
  const minutes = o.minutes || +($("#bd-minutes").value) || 25;
  const task = (o.task !== undefined ? o.task : $("#bd-task").value || "").trim();
  const why = (o.why !== undefined ? o.why : $("#bd-why").value || "").trim();
  const first = (o.first !== undefined ? o.first : $("#bd-first").value || "").trim();
  const allow = (o.allow || ($("#bd-allow").value || "").split(",").map(s => s.trim()).filter(Boolean));
  const gentle = o.gentle !== undefined ? o.gentle : $("#bd-gentle").checked;
  const voice = o.voice !== undefined ? o.voice : (!$("#bd-voice").checked || gentle);
  const pomo = { enabled: $("#bd-pomo").checked, breakMin: +($("#bd-break").value) || 5 };
  state.focus.pomo = pomo;
  try { localStorage.setItem("friday.pomo", JSON.stringify(pomo)); } catch (e) {}
  const r = await api("/api/focus/start", { method: "POST", body: JSON.stringify({ minutes, allow, voice, task, why, first_step: first }) });
  if (r.ok) {
    state.focus.companionLog = [{ msg: o.justStart
      ? "5 minutes. That's it. Future-you says thanks — I'm right here."
      : bdRand(BD_LINES.start), kind: "start", ts: Date.now() }];
    state.focus.lastMilestone = 0;
    state.focus.onBreak = false;
    state.focus.round = (o.round || 1);
    state.focus.comebacks = 0;
    chime("start");
    toast(`🎯 Body-double session started — ${minutes} min`, "good");
  } else {
    toast("focus: " + esc(r.error), "bad");
  }
  armNotifications();
  loadFocus();
  return r;
}

async function stopFocus() {
  const r = await api("/api/focus/stop", { method: "POST" });
  if (r.ok) {
    state.focus.onBreak = false;
    toast(r.status === "completed" ? "🎉 Session complete" : "Focus stopped", "chrome");
  }
  loadFocus();
  return r;
}

/* pomodoro transitions (client-driven; server stores mode for drift logic) */
async function bdStartBreak(breakMin) {
  state.focus.onBreak = true;
  state.focus.breakDone = false;
  state.focus.breakEnd = Date.now() / 1000 + (breakMin || 5) * 60;
  try { await api("/api/focus/mode", { method: "POST", body: JSON.stringify({ mode: "break", break_min: breakMin || 5 }) }); } catch (e) {}
  if (state.focus.active) { state.focus.active.mode = "break"; state.focus.active.break_end_ts = state.focus.breakEnd; }
  bdSay(bdRand(BD_LINES.break), "break");
  chime("workEnd");
  renderFocusStudio(state.focus.active, state.focus.stats);
  renderFocusWidget(state.focus.active);
}
async function bdEndBreak(startNext) {
  state.focus.onBreak = false;
  try { await api("/api/focus/mode", { method: "POST", body: JSON.stringify({ mode: "work" }) }); } catch (e) {}
  if (state.focus.active) { state.focus.active.mode = "work"; state.focus.active.break_end_ts = null; }
  if (startNext) {
    const s = state.focus.active;
    const n = (state.focus.round || 1) + 1;
    await stopFocus();
    await startFocus({
      minutes: state.focus.total || 25,
      task: s && s.task, why: s && s.why, first: s && s.first_step,
      allow: s ? JSON.parse(s.allow_domains || "[]") : [],
      voice: true, round: n,
    });
    bdSay(bdRand(BD_LINES.breakOver).replace("{n}", n), "checkin");
  } else {
    chime("breakEnd");
    renderFocusStudio(state.focus.active, state.focus.stats);
  }
}

/* milestone check-ins (deterministic, gentle) + reward reminders */
function bdCheckMilestones(s) {
  const total = (s.target_min || 25) * 60;
  const elapsed = Date.now() / 1000 - s.start_ts;
  const frac = Math.min(1, elapsed / total);
  const ms = Math.floor(frac * 4);
  if (ms > state.focus.lastMilestone) {
    state.focus.lastMilestone = ms;
    if (BD_LINES.milestone[ms]) {
      bdSay(BD_LINES.milestone[ms], "checkin");
    } else if (ms === 2 && s.why) {
      bdSay(`Halfway! Remember the why — “${s.why}” is waiting on the other side.`, "checkin");
    }
  }
}

function bdFmt(s) {
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

/* 7-day focus chain (don't break the chain 🔥) */
function bdChain(stats) {
  const days = [];
  for (let i = 6; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86400000);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    days.push({ key, label: ["S", "M", "T", "W", "T", "F", "S"][d.getDay()], min: 0 });
  }
  const dayMin = {};
  (stats.sessions || []).forEach(s => {
    if (s.status === "completed" || s.status === "abandoned") {
      const d = new Date(s.start_ts * 1000);
      const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      const m = Math.max(0, Math.round(((s.end_ts || Date.now() / 1000) - s.start_ts) / 60));
      dayMin[key] = (dayMin[key] || 0) + m;
    }
  });
  const max = Math.max(1, ...days.map(d => dayMin[d.key] || 0));
  return days.map(d => {
    const v = dayMin[d.key] || 0;
    const h = Math.max(3, Math.round((v / max) * 40));
    return `<div class="chain-day" title="${v} min"><div class="chain-bar" style="height:${h}px"></div><span>${d.label}</span></div>`;
  }).join("");
}

function renderFocusStudio(s, stats) {
  const host = $("#bd-host");
  if (!host) return;
  if (!s) {
    const st = stats || {};
    const l = st.learned || {};
    const sessions = st.sessions || [];
    host.innerHTML = `
      <div class="bd-idle">
        <div class="bd-companion idle">
          <img src="/static/companion.png" class="bd-avatar" alt="Friday"/>
          <div class="bd-companion-name">Friday</div>
          <div class="bd-companion-status">Ready when you are — I'll work alongside you.</div>
          <div class="bd-adhd-tips">
            <div class="bd-tip-chip">⚡ 5 minutes counts</div>
            <div class="bd-tip-chip">🎯 one task at a time</div>
            <div class="bd-tip-chip">🧡 forgive, come back</div>
          </div>
        </div>
        <div class="bd-start-card card">
          <h3>What are we working on?</h3>
          <input id="bd-task" type="text" placeholder="One task. Just one. (e.g. build the agent UI)" maxlength="120"/>
          <div class="bd-field-hint dim">✳️ <b>First tiny step</b> — makes starting easy</div>
          <input id="bd-first" type="text" placeholder="e.g. open the editor and write one function" maxlength="120"/>
          <div class="bd-field-hint dim">🎁 <b>Reward after</b> — ADHD brains work on rewards</div>
          <input id="bd-why" type="text" placeholder="e.g. then I watch one episode" maxlength="120"/>
          <div class="bd-presets">
            ${[15, 25, 45, 60].map(m => `<button class="bd-preset ${m === 25 ? "active" : ""}" data-min="${m}">${m}m</button>`).join("")}
            <input id="bd-minutes" type="number" value="25" min="1" max="180" title="custom minutes"/>
          </div>
          <div class="bd-opts">
            <label class="bd-check"><input id="bd-pomo" type="checkbox" checked/> pomodoro · break</label>
            <input id="bd-break" type="number" value="5" min="2" max="20" title="break minutes" style="width:64px"/> <span class="dim">min</span>
            <label class="bd-check"><input id="bd-gentle" type="checkbox"/> gentle</label>
          </div>
          <div class="bd-opts">
            <input id="bd-allow" type="text" placeholder="allow domains, comma (e.g. github.com)"/>
            <label class="bd-check"><input id="bd-voice" type="checkbox" checked/> voice nudges</label>
            <label class="bd-check"><input id="bd-ambient" type="checkbox" ${ambient.on ? "checked" : ""}/> brown noise</label>
          </div>
          <button id="bd-go" class="btn primary big">▶ Start — I'm here with you</button>
          <button id="bd-just5" class="btn ghost big">⚡ Just start — 5 minutes</button>
          <div class="bd-tip dim">ADHD-friendly: start tiny, state the reward, and I'll keep the rhythm with chimes + check-ins.</div>
        </div>
      </div>
      <div class="bd-record">
        <h3>Your focus record</h3>
        <div class="bd-chain">${bdChain(st)}</div>
        <div class="ov-grid">
          <div class="ov-tile"><div class="v">${st.week ? st.week.minutes : 0}<small>m</small></div><div class="l">this week · ${st.week ? st.week.count : 0} sessions</div></div>
          <div class="ov-tile"><div class="v">${st.streak_days || 0}<small>d</small></div><div class="l">day streak 🔥</div></div>
          <div class="ov-tile"><div class="v">${st.best_day_min || 0}<small>m</small></div><div class="l">best day</div></div>
          <div class="ov-tile"><div class="v">${st.avg_min || 0}<small>m</small></div><div class="l">avg session</div></div>
          <div class="ov-tile"><div class="v">${st.total_completed || 0}</div><div class="l">completed</div></div>
          <div class="ov-tile"><div class="v">${st.total_drifts || 0}</div><div class="l">drifts · ${st.nudges_total || 0} nudges</div></div>
        </div>
        <div class="dim" style="margin:10px 0 4px">peak drift hours: ${(l.peak_hours || []).map(h => `${h}:00`).join(", ") || "—"} · top distractions: ${(l.top_distraction_domains || []).map(esc).join(", ") || "—"}</div>
        ${sessions.length ? `<h4 style="margin:12px 0 6px">Recent sessions</h4>` + sessions.slice(0, 8).map(x => `
          <div class="hist-row">
            <span class="chip ${x.status}">${esc(x.status)}</span>
            <span>${fmtDate(x.start_ts)}</span>
            <span><b>${Math.round(((x.end_ts || Date.now() / 1000) - x.start_ts) / 60)}</b>/${x.target_min}m</span>
            <span class="dim">drifts ${x.drift_count}</span>
            ${x.task ? `<span class="dim">· ${esc(x.task)}</span>` : ""}
            ${x.why ? `<span class="dim">· 🎁 ${esc(x.why)}</span>` : ""}
          </div>`).join("") : ""}
      </div>`;
    // preset chips
    $$(".bd-preset", host).forEach(b => b.addEventListener("click", () => {
      $$(".bd-preset", host).forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      $("#bd-minutes").value = b.dataset.min;
    }));
    const go = $("#bd-go");
    if (go) go.addEventListener("click", () => startFocus());
    const j5 = $("#bd-just5");
    if (j5) j5.addEventListener("click", () => startFocus({ minutes: 5, justStart: true }));
    const amb = $("#bd-ambient");
    if (amb) amb.addEventListener("change", () => toggleAmbient(amb.checked));
    return;
  }
  /* ---------------- active session ---------------- */
  const onBreak = s.mode === "break" && state.focus.onBreak;
  const now = Date.now() / 1000;
  const workEnd = s.start_ts + s.target_min * 60;
  const left = onBreak
    ? Math.max(0, (s.break_end_ts || state.focus.breakEnd || now) - now)
    : Math.max(0, workEnd - now);
  const pct = Math.max(0, Math.min(1, onBreak ? 1 - left / ((s.break_end_ts || now + 300) - now) : 1 - left / (s.target_min * 60)));
  const el = Math.floor((now - s.start_ts) / 60);
  const drifts = s.drift_count || 0;
  const doms = (stats && stats.recent_drifts || []).filter(d => d.session_id === s.session_id).map(d => d.domain);
  host.innerHTML = `
    <div class="bd-active">
      <div class="bd-main">
        <div class="bd-companion active ${onBreak ? "onbreak" : ""}">
          <img src="/static/companion.png" class="bd-avatar ${onBreak ? "" : "pulse"}" alt="Friday"/>
          <div class="bd-companion-name">Friday</div>
          <div class="bd-companion-status">${onBreak ? "on break with you ☕" : "working alongside you · in flow"}</div>
          ${state.focus.round > 1 ? `<div class="bd-round">round ${state.focus.round}</div>` : ""}
        </div>
        <div class="bd-timer-card card ${onBreak ? "break" : ""}">
          <div class="bd-mode-label">${onBreak ? "☕ BREAK" : "🎯 FOCUS"}</div>
          <div class="bd-timer-ring">
            <svg viewBox="0 0 120 120">
              <defs><linearGradient id="bdGrad" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0%" stop-color="#ff7849"/><stop offset="100%" stop-color="#ff5e3a"/>
              </linearGradient>
              <linearGradient id="bdGradBreak" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0%" stop-color="#0ea5a4"/><stop offset="100%" stop-color="#34d399"/>
              </linearGradient></defs>
              <circle class="bd-ring-bg" cx="60" cy="60" r="52"/>
              <circle class="bd-ring-fg" id="bd-ring-fg" cx="60" cy="60" r="52" stroke="url(#${onBreak ? "bdGradBreak" : "bdGrad"})"/>
            </svg>
            <div class="bd-timer-time" id="bd-timer-time">${bdFmt(left)}</div>
          </div>
          <div class="bd-timer-task">${onBreak ? "stand · water · look far away" : (s.task ? `🎯 ${esc(s.task)}` : "deep work")}</div>
          ${!onBreak && s.first_step ? `<div class="bd-first-step dim">first step: ${esc(s.first_step)}</div>` : ""}
          ${!onBreak && s.why ? `<div class="bd-reward dim">🎁 after: ${esc(s.why)}</div>` : ""}
          <div class="bd-timer-sub dim">${onBreak ? "" : `${el}m elapsed · ${Math.round(pct * 100)}% done`}</div>
          ${!onBreak && drifts > 0 ? `<button id="bd-back" class="btn ghost back">🧡 I'm back — reset the clock</button>` : ""}
          <div class="bd-actions">
            ${onBreak
              ? `<button id="bd-break-next" class="btn primary">▶ Start next round</button><button id="bd-break-end" class="btn ghost">I'm done</button>`
              : `<button id="bd-stop" class="btn danger">■ End session</button><button id="bd-plus5" class="btn ghost">+5 min</button>`}
            <button id="bd-ambient-btn" class="btn ghost ${ambient.on ? "ambient-on" : ""}" title="brown noise">${ambient.on ? "🔊 noise on" : "🔈 noise"}</button>
          </div>
        </div>
      </div>
      <div class="bd-side">
        <div class="card bd-log-card">
          <h3>Friday's log</h3>
          <div id="bd-log" class="bd-log"></div>
        </div>
        <div class="card bd-stats-card">
          <h3>This session</h3>
          <div class="bd-stat"><span>drifts</span><b id="bd-drifts">${drifts}</b></div>
          <div class="bd-stat"><span>comebacks</span><b>${state.focus.comebacks}</b></div>
          <div class="bd-stat"><span>elapsed</span><b>${el}m</b></div>
          <div class="bd-stat"><span>target</span><b>${s.target_min}m</b></div>
          ${doms.length ? `<div class="bd-doms dim">drifted to: ${doms.slice(0, 4).map(esc).join(", ")}</div>` : `<div class="bd-doms dim">no drifts yet — locked in 🔒</div>`}
        </div>
      </div>
    </div>`;
  const ring = $("#bd-ring-fg");
  if (ring) ring.style.strokeDashoffset = (326.7 * (1 - pct)).toFixed(1);
  const stop = $("#bd-stop");
  if (stop) stop.addEventListener("click", () => stopFocus());
  const plus = $("#bd-plus5");
  if (plus) plus.addEventListener("click", async () => {
    await api("/api/focus/stop", { method: "POST" });
    const mins = Math.max(1, Math.round(left / 60) + 5);
    await startFocus({ minutes: mins, task: s.task, why: s.why, first: s.first_step, allow: JSON.parse(s.allow_domains || "[]"), voice: true, round: state.focus.round });
    bdSay("I added 5 minutes — we're in this together.");
  });
  const back = $("#bd-back");
  if (back) back.addEventListener("click", () => {
    state.focus.comebacks += 1;
    bdSay(bdRand(BD_LINES.back), "checkin");
    chime("start");
    renderFocusStudio(state.focus.active, state.focus.stats);
  });
  const bnext = $("#bd-break-next");
  if (bnext) bnext.addEventListener("click", () => bdEndBreak(true));
  const bend = $("#bd-break-end");
  if (bend) bend.addEventListener("click", () => bdEndBreak(false));
  const amb = $("#bd-ambient-btn");
  if (amb) amb.addEventListener("click", () => { toggleAmbient(); renderFocusStudio(state.focus.active, state.focus.stats); });
  renderCompanionLog();
}

function renderCompanionLog() {
  const log = $("#bd-log");
  if (!log) return;
  log.innerHTML = state.focus.companionLog.map(l => `
    <div class="bd-line ${l.kind}">
      <img src="/static/companion.png" class="bd-log-avatar" alt=""/>
      <span>${esc(l.msg)}</span>
      <span class="dim bd-log-ts">${new Date(l.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
    </div>`).join("") || `<div class="dim">I'm here.</div>`;
  log.scrollTop = log.scrollHeight;
}

function renderFocusWidget(s) {
  const idle = $("#focus-widget-idle"), act = $("#focus-widget-active");
  if (!idle || !act) return;
  if (s) {
    idle.classList.add("hidden"); act.classList.remove("hidden");
    const onBreak = s.mode === "break" && state.focus.onBreak;
    const left = onBreak
      ? Math.max(0, (s.break_end_ts || 0) - Date.now() / 1000)
      : Math.max(0, (s.start_ts + s.target_min * 60) - Date.now() / 1000);
    const t = $("#bd-mini-time"); if (t) t.textContent = (onBreak ? "☕ " : "") + bdFmt(left);
    const task = $("#bd-mini-task"); if (task) task.textContent = onBreak ? "on break" : (s.task ? s.task : "deep work");
    const d = $("#focus-drifts"); if (d) d.textContent = `drifts: ${s.drift_count || 0}${state.focus.round > 1 ? ` · R${state.focus.round}` : ""}`;
    const w = $("#focus-widget"); if (w) w.classList.remove("hidden");
  } else {
    idle.classList.remove("hidden"); act.classList.add("hidden");
  }
}

/* companion reaction when a drift is detected — gentle, no judgment */
function bdOnDrift(domain) {
  if (!state.focus.active) return;
  if (state.focus.onBreak) return;         // break = wandering allowed
  const d = String(domain || "").replace(/^www\./, "").slice(0, 40);
  bdSay(bdRand(BD_LINES.drift).replace("{d}", d || "another tab"), "drift");
  if (state.view === "focus") {
    api("/api/focus/stats").then(st => { state.focus.stats = st; renderFocusStudio(state.focus.active, st); }).catch(() => {});
  }
}

async function loadFocus() {
  const [active, stats] = await Promise.all([api("/api/focus/active"), api("/api/focus/stats")]);
  const s = active.session;
  state.focus.stats = stats;
  // resume break state after refresh: server persisted mode + break_end_ts
  if (s && s.mode === "break") {
    state.focus.onBreak = true;
    state.focus.breakEnd = s.break_end_ts || (Date.now() / 1000 + 300);
  } else if (s) {
    state.focus.onBreak = false;
  }
  // detect session end (worker completes / user stopped elsewhere)
  if (state.focus.active && !s) {
    const ended = state.focus.active;
    state.focus.lastSessionId = ended.session_id;
    const mins = Math.max(0, Math.round((Date.now() / 1000 - ended.start_ts) / 60));
    const dr = ended.drift_count || 0;
    const wrap = mins >= (ended.target_min || 25) * 0.8;
    state.focus.onBreak = false;
    bdSay(bdRand(BD_LINES.end)
      .replace("{m}", mins)
      .replace("{t}", ended.task ? ` on “${ended.task}”` : "")
      .replace("{d}", dr)
      .replace("{s}", dr === 1 ? "" : "s"), "end");
    if (state.view === "focus") {
      toast(wrap ? "🎉 Session complete — nice work" : "Focus session ended", wrap ? "good" : "chrome");
    }
  }
  state.focus.active = s;
  state.focus.total = s ? s.target_min : 25;
  state.focus.ends = s ? s.start_ts + s.target_min * 60 : 0;
  if (s && s.session_id !== state.focus.lastSessionId) {
    // fresh session (just started here or in chat) → seed the companion log
    state.focus.lastSessionId = s.session_id;
    state.focus.lastMilestone = 0;
    state.focus.onBreak = false;
    state.focus.comebacks = 0;
    if (!state.focus.companionLog.length || state.focus.companionLog[0].kind !== "start") {
      state.focus.companionLog = [{ msg: `I'm here with you — ${s.target_min} min${s.task ? ` on “${s.task}”` : ""}${s.why ? `, then: ${s.why}` : ""}. Same room, same timer. Let's work.`, kind: "start", ts: Date.now() }];
    }
  }
  if (s && !state.focus.onBreak) bdCheckMilestones(s);
  renderFocusStudio(s, stats);
  renderFocusWidget(s);
}

/* sidebar + header quick entry points */
$("#side-focus-quick")?.addEventListener("click", () => { switchView("focus"); });
$("#header-focus-btn")?.addEventListener("click", () => { switchView("focus"); });
$("#focus-refresh")?.addEventListener("click", () => loadFocus());

/* ============================== books panel ============================== */
$("#book-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  const toastEl = toast(`📚 uploading <b>${esc(file.name)}</b>… (any size, streamed)`, "chrome");
  try {
    const r = await fetch("/api/books/upload", { method: "POST", body: fd }).then(r => r.json());
    toast(`📚 ${esc(file.name)}: ${r.chunks} chunks / ${r.pages} pages — ready`, "good");
    loadBooks();
  } catch (err) {
    toast("upload failed: " + esc(err.message), "bad");
  }
  e.target.value = "";
});
async function loadBooks() {
  const { books } = await api("/api/books");
  state.books = books;
  $("#book-list").innerHTML = books.map(b => `
    <div class="card book-card" onclick="openBook(${b.book_id})">
      <div style="font-size:22px">📕</div>
      <div class="a-main" style="flex:1">
        <div class="a-text"><b>${esc(b.title)}</b></div>
        <div class="a-meta dim">${b.pages} pages · ${b.status}</div>
        <div class="book-progress"><div class="bar"><div class="bar-fill" style="width:${(b.progress * 100).toFixed(0)}%"></div></div></div>
      </div>
      <button class="mini-btn" onclick="event.stopPropagation();openBook(${b.book_id})">open</button>
    </div>`).join("") || `<div class="dim">no books yet — upload a PDF (no size limit) and Friday will OCR + index it</div>`;
}
function openBook(id) {
  const b = state.books.find(x => x.book_id === id);
  if (!b) return;
  const d = $("#book-reader");
  d.classList.remove("hidden");
  d.innerHTML = `<button class="btn" onclick="$('#book-reader').classList.add('hidden')">✕ close</button>
    <h2 style="margin:10px 0">${esc(b.title)}</h2>
    <div class="card-row" style="margin-bottom:10px"><span class="chip">${b.pages} pages</span><span class="chip">${esc(b.status)}</span>
      <button class="mini-btn" onclick="bookQuiz(${b.book_id})">📝 quiz me</button>
      <button class="mini-btn" onclick="bookPpt(${b.book_id})">📊 make PPT</button></div>
    <h3>Discuss the book</h3>
    <div class="form-row">
      <input id="book-q" placeholder="ask about page 244, an exercise, a chapter…"/>
      <button class="mini-btn primary" onclick="bookAsk(${b.book_id})">ask</button>
    </div>
    <div id="book-answers"></div>`;
}
async function bookAsk(id) {
  const q = $("#book-q").value;
  if (!q) return;
  const box = $("#book-answers");
  box.innerHTML += `<div class="msg user" style="max-width:100%"><div class="bubble">${md(q)}</div></div>`;
  const typing = document.createElement("div");
  typing.className = "msg friday"; typing.innerHTML = `<div class="bubble"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></div>`;
  box.appendChild(typing);
  try {
    const res = await fetch(`/api/books/${id}/ask`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q }) });
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = ""; let reply = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i; while ((i = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 2);
        if (line.startsWith("data:")) { try { const ev = JSON.parse(line.slice(5)); if (ev.type === "delta") { reply += ev.text; typing.querySelector(".bubble").innerHTML = md(reply); } if (ev.type === "done") { typing.querySelector(".bubble").innerHTML = md(reply); } } catch (e) {} }
      }
    }
  } catch (e) { typing.remove(); box.innerHTML += `<div class="dim" style="color:var(--bad)">${esc(e.message)}</div>`; }
}
async function bookQuiz(id) {
  const r = await api(`/api/books/${id}/quiz`, { method: "POST", body: JSON.stringify({}) });
  const box = $("#book-answers");
  box.innerHTML = (r.questions || []).map((q, i) => `<div class="card"><h3>Q${i + 1} (p${q.page})</h3><p>${esc(q.q)}</p></div>`).join("") || `<div class="dim">no questions yet — ingest more of the book</div>`;
}
async function bookPpt(id) {
  const r = await api(`/api/books/${id}/ppt`, { method: "POST", body: JSON.stringify({}) });
  toast(`📊 PPTX ready — <a href="/api/artifacts/${r.artifact_id}" download>download</a>`);
}

/* ============================== admin panel ============================== */
async function loadAdmin() {
  const ov = await api("/api/admin/overview");
  renderOverview(ov);
  renderKeys(ov.provider_keys);
  loadParams();
  loadSpend();
  loadTurns();
  loadGenome();
}
function renderOverview(ov) {
  const c = ov.counts || {};
  $("#admin-overview").innerHTML = `
    <div class="ov-grid">
      ${[["llm", ov.llm_provider], ["search", ov.search_provider], ["spend today", fmtMoney(ov.spend_today_usd)], ["budget pct", ov.budget_pct + "%"], ["ask left", ov.ask_budget_left], ["atoms", c.atoms], ["events", c.events], ["beliefs", c.claims], ["tensions", c.tensions], ["tasks", c.tasks], ["running", c.running_tasks], ["approvals", c.waiting_approval], ["trackers", c.trackers], ["books", c.books], ["skills", c.skills], ["turns 24h", c.turns_today], ["avg latency", ov.latency && ov.latency.a ? Math.round(ov.latency.a) + "ms" : "—"], ["avg cost", ov.cost_avg && ov.cost_avg.a ? fmtMoney(ov.cost_avg.a) : "—"]]
      .map(([k, v]) => `<div class="ov-tile"><div class="v">${esc(String(v))}</div><div class="k">${esc(k)}</div></div>`).join("")}
    </div>
    ${ov.gym ? `<div class="card"><h3>Last Gym report</h3><pre style="white-space:pre-wrap;font-size:12px">${esc(JSON.stringify(JSON.parse(ov.gym), null, 1))}</pre></div>` : ""}
    <div class="card"><h3>Genome (recent commits)</h3><div class="cards">${(ov.genome_log || []).map(g => `<div class="hist-row"><span class="chip">${esc(g.sha)}</span><span>${esc(g.msg)}</span><span class="dim">${esc(g.date)}</span></div>`).join("")}</div></div>`;
}
function renderKeys(keys) {
  $("#provider-keys").innerHTML = `<h4 style="margin:10px 0">Provider keys (masked)</h4>` +
    keys.map(k => `<div class="hist-row"><span class="chip">${esc(k.provider)}</span><span class="chip">${esc(k.scope)}</span><span>${esc(k.masked)}</span><span class="chip ${k.active ? "good" : "bad"}">${k.active ? "active" : "off"}</span><span class="dim">${esc(k.source)}</span></div>`).join("") || `<div class="dim">no keys — configure below or just tell Friday your key in chat</div>`;
}
$("#mc-save").addEventListener("click", async () => {
  const body = { provider: $("#mc-provider").value, scope: "default", api_key: $("#mc-key").value };
  if (!body.api_key) return toast("enter a key");
  try { const r = await api("/api/admin/models/configure", { method: "POST", body: JSON.stringify(body) }); $("#mc-result").innerHTML = `<span class="chip good">✓ ${esc(r.provider)} → ${esc(r.masked)} (live)</span>`; $("#mc-key").value = ""; loadAdmin(); }
  catch (e) { $("#mc-result").innerHTML = `<span class="chip bad">${esc(e.message)}</span>`; }
});
$("#mc-test").addEventListener("click", async () => {
  $("#mc-result").innerHTML = "testing…";
  const r = await api("/api/admin/providers/test", { method: "POST", body: JSON.stringify({ api_key: $("#mc-key").value || null }) });
  $("#mc-result").innerHTML = r.ok ? `<span class="chip good">✓ replied: ${esc(r.reply)}</span>` : `<span class="chip bad">✗ ${esc(r.error)}</span>`;
});
$("#oc-go").addEventListener("click", async () => {
  const kind = $("#oc-kind").value;
  const value = $("#oc-value").value;
  try { const r = await api("/api/admin/oneclick", { method: "POST", body: JSON.stringify({ kind, value }) }); $("#oc-result").innerHTML = `<span class="chip good">✓ ${esc(r.note)}</span>`; }
  catch (e) { $("#oc-result").innerHTML = `<span class="chip bad">${esc(e.message)}</span>`; }
});
function copyServerUrl() {
  const url = location.origin;
  navigator.clipboard.writeText(url).then(() => {
    $("#oc-ext-status").innerHTML = `<span class="chip good">✓ copied: ${esc(url)}</span>`;
  }).catch(() => {
    $("#oc-ext-status").innerHTML = `<span class="chip">${esc(url)}</span>`;
  });
}

/* params tree — every config knob, live-editable */
let paramDirty = {};
async function loadParams() {
  const p = await api("/api/admin/params");
  state.params = p;
  renderParams(p.defaults, "", p.live);
}
function renderParams(node, prefix, live) {
  const box = $("#param-tree");
  let html = "";
  const walk = (obj, path) => {
    for (const [k, v] of Object.entries(obj)) {
      const key = path ? path + "." + k : k;
      const isLive = key in live;
      if (v && typeof v === "object" && !Array.isArray(v)) {
        html += `<div class="param-group"><h4>${esc(k)}</h4>`;
        walk(v, key);
        html += `</div>`;
      } else {
        const cur = key in paramDirty ? paramDirty[key] : (isLive ? live[key] : v);
        html += `<div class="param-row" data-key="${esc(key)}">
          <span class="pk" title="${esc(key)}">${esc(key)}</span>
          ${renderParamInput(key, cur, v, isLive)}
          ${isLive ? `<span class="live-tag">LIVE</span>` : ""}
        </div>`;
      }
    }
  };
  walk(node, "");
  box.innerHTML = html;
  $$(".param-row", box).forEach(row => {
    const key = row.dataset.key;
    const el = row.querySelector("input,select");
    if (!el) return;
    el.addEventListener("change", () => {
      let val = el.type === "checkbox" ? el.checked : el.type === "number" ? +el.value : el.value;
      paramDirty[key] = val;
      row.querySelector(".live-tag")?.remove();
      row.insertAdjacentHTML("beforeend", `<span class="live-tag">DIRTY</span>`);
    });
  });
}
function renderParamInput(key, cur, def, isLive) {
  if (typeof cur === "boolean") return `<input type="checkbox" ${cur ? "checked" : ""}/>`;
  if (typeof cur === "number") return `<input type="number" step="any" value="${cur}"/>`;
  if (Array.isArray(cur)) return `<input value="${esc(cur.join(","))}" placeholder="comma list"/>`;
  return `<input value="${esc(cur)}" placeholder="${esc(String(def))}"/>`;
}
$("#param-search").addEventListener("input", () => {
  const q = $("#param-search").value.toLowerCase();
  $$(".param-row").forEach(r => r.style.display = !q || r.dataset.key.toLowerCase().includes(q) ? "" : "none");
  $$(".param-group").forEach(g => g.style.display = [...g.querySelectorAll(".param-row")].some(r => r.style.display !== "none") ? "" : "none");
});
$("#param-save").addEventListener("click", async () => {
  if (!Object.keys(paramDirty).length) return toast("nothing changed");
  const r = await api("/api/admin/settings", { method: "PUT", body: JSON.stringify(paramDirty) });
  paramDirty = {};
  toast(`saved ${r.updated.length} params — live`);
  loadParams();
});

async function loadSpend() {
  const d = await api("/api/admin/spend");
  const max = Math.max(0.001, ...d.series.map(s => s.usd));
  $("#spend-chart").innerHTML = `<div class="card"><h3>Spend — last ${d.series.length} days</h3><div class="card-row" style="align-items:flex-end;height:120px;gap:4px">` +
    d.series.map(s => `<div style="flex:1;background:linear-gradient(180deg,var(--accent),var(--accent2));height:${Math.max(3, (s.usd / max) * 100)}%;border-radius:4px 4px 0 0;min-width:14px" title="${s.date}: ${fmtMoney(s.usd)} (${s.turns} turns)"></div>`).join("") + `</div></div>`;
  $("#spend-table").innerHTML = `<table class="data"><thead><tr><th>date</th><th>usd</th><th>turns</th></tr></thead><tbody>` +
    d.series.map(s => `<tr><td>${esc(s.date)}</td><td>${fmtMoney(s.usd)}</td><td>${s.turns}</td></tr>`).join("") + `</tbody></table>`;
}
async function loadTurns() {
  const d = await api("/api/admin/turns?limit=40");
  $("#turns-table").innerHTML = `<table class="data"><thead><tr><th>time</th><th>model</th><th>ms</th><th>usd</th><th>outcome</th><th>user</th><th>reply</th></tr></thead><tbody>` +
    d.turns.map(t => `<tr><td>${fmtDate(t.created_ts)}</td><td>${esc(t.model)}</td><td>${t.latency_ms}</td><td>${fmtMoney(t.cost_usd)}</td><td>${esc(t.outcome || "")}</td><td>${esc(String(t.user_text).slice(0, 60))}</td><td>${esc(String(t.reply || "").slice(0, 80))}</td></tr>`).join("") + `</tbody></table>`;
}
async function loadEval() {
  try {
    const r = await api("/api/eval/report");
    if (!r.ok) { $("#eval-summary").innerHTML = `<div class="dim">${esc(r.error)}</div>`; return; }
    const s = r.summary || {};
    $("#eval-summary").innerHTML = `
      <div class="ov-grid">
        <div class="ov-tile"><div class="v">${s.passed}/${s.scenarios}</div><div class="k">scenarios passed</div></div>
        <div class="ov-tile"><div class="v">${s.elapsed_s}s</div><div class="k">eval time</div></div>
        <div class="ov-tile"><div class="v">${esc(s.generated || "")}</div><div class="k">generated</div></div>
      </div>`;
    const dl = await api("/api/eval/report");
    if (dl.ok && dl.files) {
      $("#eval-details").innerHTML = Object.entries(dl.files).map(([n, u]) =>
        `<div class="hist-row"><span>${esc(n)}</span><a class="mini-btn" href="${u}" download>⬇ download</a></div>`).join("");
    }
  } catch (e) { $("#eval-summary").innerHTML = `<div class="dim">${esc(e.message)}</div>`; }
}

async function loadGenome() {
  const [log, skills] = await Promise.all([api("/api/genome/log"), api("/api/genome/skills")]);
  $("#genome-log").innerHTML = log.log.map(g => `<div class="hist-row"><span class="chip">${esc(g.sha)}</span><span>${esc(g.msg)}</span><span class="dim">${esc(g.date)}</span></div>`).join("") || `<div class="dim">no commits</div>`;
  $("#genome-skills").innerHTML = skills.skills.map(s => `
    <div class="atom-card"><div class="a-main"><div class="a-text">${esc(s.name)} <span class="kind-pill">${esc(s.category)}</span></div>
      <div class="a-meta">${esc(s.description)}</div>
      <div class="a-meta">${(s.tags || []).map(t => `<span class="chip">${esc(t)}</span>`).join(" ")}
        <span class="chip ${s.has_verify ? "good" : "bad"}">verify ${s.has_verify ? "✓" : "✗"}</span>
        <span class="chip ${s.has_cassette ? "good" : "bad"}">cassette ${s.has_cassette ? "✓" : "✗"}</span></div></div></div>`).join("");
}

/* ============================== tabs wiring ============================== */
$$(".tab[data-mtab]").forEach(t => t.addEventListener("click", () => {
  $$(".tab[data-mtab]").forEach(x => x.classList.toggle("active", x === t));
  $$(".mtab").forEach(x => x.classList.toggle("active", x.id === "mtab-" + t.dataset.mtab));
  if (t.dataset.mtab === "graph") loadGraph();
}));
$$(".tab[data-atab]").forEach(t => t.addEventListener("click", () => {
  $$(".tab[data-atab]").forEach(x => x.classList.toggle("active", x === t));
  $$(".atab").forEach(x => x.classList.toggle("active", x.id === "atab-" + t.dataset.atab));
  if (t.dataset.atab === "spend") loadSpend();
  if (t.dataset.atab === "turns") loadTurns();
  if (t.dataset.atab === "genome") loadGenome();
  if (t.dataset.atab === "eval") loadEval();
}));

/* ============================== focus sensor ============================== */
/* PWA-level drift sensor: when a focus session is active and this tab loses
   visibility (user switched to another tab/app), report a drift so Friday can
   nudge immediately. Full tab tracking comes from the MV3 extension (download
   via /api/extension/zip), this covers the no-extension case. */
let lastDriftReport = 0;

function reportDrift() {
  if (!state.focus.active) return;
  if (state.focus.onBreak) return;   // breaks are drift-free by design
  const now = Date.now();
  if (now - lastDriftReport < 3000) return;          // client throttle
  lastDriftReport = now;
  fetch("/api/focus/drift", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: location.href.slice(0, 500), title: document.title.slice(0, 200) }),
  })
    .then(r => r.json())
    .then(res => {
      // ACT on the server's nudges IMMEDIATELY — don't wait for the SSE
      // stream (hidden tabs throttle SSE; this path is instant).
      if (res && res.nudges && res.nudges.length) {
        for (const n of res.nudges) {
          if (n.channel === "toast") toast(`🎯 ${esc(n.message)}`, "focus");
          if (n.channel === "chrome") notifyChrome("🎯 Friday — focus", n.message);
          if (n.channel === "voice") speakNudge(n.message);
        }
        beep();
        bdOnDrift(location.hostname || location.href);
      }
    })
    .catch(() => {});
}
document.addEventListener("visibilitychange", () => { if (document.hidden) reportDrift(); });
window.addEventListener("blur", () => { if (document.hidden) reportDrift(); });
window.addEventListener("pagehide", () => { if (state.focus.active) reportDrift(); });

/* Chrome-desktop notifications for nudges — visible even when the Friday tab
   is hidden (this is the "immediate nudge" the user asked for). */
function notifyChrome(title, body) {
  if (!("Notification" in window)) return;
  if (Notification.permission === "granted") {
    try { new Notification(title, { body, icon: "/static/icon128.png" }); } catch (e) {}
  } else if (Notification.permission !== "denied") {
    Notification.requestPermission();
  }
}
function armNotifications() {
  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission();
  }
}
/* audible nudge (works even if the tab is hidden — user asked for immediacy) */
function speakNudge(text) {
  try {
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "en-IN"; u.rate = 1.05;
    speechSynthesis.speak(u);
  } catch (e) {}
}
function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = "square"; o.frequency.value = 880;
    g.gain.value = 0.06;
    o.connect(g); g.connect(ctx.destination);
    o.start(); setTimeout(() => { o.stop(); ctx.close(); }, 220);
  } catch (e) {}
}
/* request notification permission on first user gesture (chat focus-start
   doesn't go through the panel button, so this covers that path) */
["pointerdown", "keydown", "touchstart"].forEach(evt =>
  window.addEventListener(evt, () => armNotifications(), { once: true, passive: true }));

/* ============================== live loops ============================== */
async function pollOverview() {
  try {
    const ov = await api("/api/admin/overview");
    $("#spend-today").textContent = fmtMoney(ov.spend_today_usd);
    $("#budget-total").textContent = "/ " + fmtMoney(ov.daily_budget_usd);
    $("#budget-bar").style.width = Math.min(100, ov.budget_pct) + "%";
    $("#ask-budget").textContent = "ask budget: " + ov.ask_budget_left + " left";
    $("#sys-status").textContent = `${ov.llm_provider} · ${ov.search_provider} · ${ov.counts.atoms} atoms`;
    // status pill in the chat header
    const pill = $("#status-text");
    if (pill) {
      const p = String(ov.llm_provider || "");
      if (p.includes("sim")) { pill.textContent = "offline"; }
      else if (p) { pill.textContent = "live · " + p; }
    }
  } catch (e) {}
}
function pollFocus() {
  const s = state.focus.active;
  if (!s) {
    const hm = $("#header-focus-mini");
    if (hm) hm.classList.add("hidden");
    return;
  }
  const now = Date.now() / 1000;
  const onBreak = state.focus.onBreak && s.mode === "break";
  const workEnd = s.start_ts + s.target_min * 60;
  let left, pct;
  if (onBreak) {
    left = Math.max(0, (s.break_end_ts || state.focus.breakEnd || now) - now);
    const denom = Math.max(1, (s.break_end_ts || state.focus.breakEnd || now + 300) - now + left);
    pct = Math.max(0, Math.min(1, 1 - left / denom));
    if (left <= 0 && !state.focus.breakDone) {
      state.focus.breakDone = true;
      chime("breakEnd");
      bdSay(bdRand(BD_LINES.breakOver).replace("{n}", (state.focus.round || 1) + 1), "checkin");
      renderFocusStudio(s, state.focus.stats);
      renderFocusWidget(s);
    }
  } else {
    left = Math.max(0, workEnd - now);
    pct = Math.max(0, Math.min(1, 1 - left / (s.target_min * 60)));
    if (left <= 0) {
      const pomo = state.focus.pomo && state.focus.pomo.enabled;
      if (pomo) {
        state.focus.breakDone = false;
        bdStartBreak((state.focus.pomo && state.focus.pomo.breakMin) || 5);
      } else {
        stopFocus();
      }
      return;
    }
    bdCheckMilestones(s);
  }
  const ring = $("#focus-ring-fg");
  if (ring) ring.style.strokeDashoffset = (163.4 * (1 - pct)).toFixed(1);
  const t = $("#focus-time"); if (t) t.textContent = bdFmt(left);
  const d = $("#focus-drifts"); if (d) d.textContent = `${onBreak ? "☕ " : ""}drifts: ${s.drift_count}${state.focus.round > 1 ? ` · R${state.focus.round}` : ""}${s.task ? ` · ${s.task}` : ""}`;
  const mt = $("#bd-mini-time"); if (mt) mt.textContent = (onBreak ? "☕ " : "") + bdFmt(left);
  const hm = $("#header-focus-mini");
  if (hm) { hm.textContent = (onBreak ? "☕ " : "") + bdFmt(left); hm.classList.remove("hidden"); }
  const big = $("#bd-timer-time");
  if (big) {
    big.textContent = bdFmt(left);
    const ring2 = $("#bd-ring-fg");
    if (ring2) ring2.style.strokeDashoffset = (326.7 * (1 - pct)).toFixed(1);
  }
}
/* nudge SSE stream → toasts */
async function nudgeStream() {
  try {
    const res = await fetch("/api/nudges/stream");
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i; while ((i = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 2);
        if (!line.startsWith("data:")) continue;
        try {
          const ev = JSON.parse(line.slice(5));
          if (ev.type === "nudge") {
            toast(esc(ev.nudge.message), ev.nudge.channel);
            notifyChrome("🎯 Friday — focus", ev.nudge.message);
            if (ev.nudge.channel === "voice" || ev.nudge.kind === "focus") { const u = new SpeechSynthesisUtterance(ev.nudge.message); u.lang = "en-IN"; speechSynthesis.speak(u); }
            if (ev.nudge.kind === "focus") bdOnDrift((ev.nudge.url) || (ev.nudge.message.match(/https?:\/\/([^\s/]+)/) || [])[1] || "somewhere");
          }
        } catch (e) {}
      }
    }
  } catch (e) {}
  setTimeout(nudgeStream, 4000);
}

function applyTheme(t) { document.documentElement.dataset.theme = t; }
/* theme toggle — persists to the server so refresh keeps it */
$("#theme-toggle").addEventListener("click", async () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(next);
  try { await api("/api/admin/settings", { method: "PUT", body: JSON.stringify({ "ui.theme": next }) }); }
  catch (e) {}
  toast(next === "dark" ? "Dark mode on" : "Light mode on", "good");
});

/* ============================== boot ============================== */
(async function boot() {
  setInterval(pollOverview, 10000);
  try { const p = JSON.parse(localStorage.getItem("friday.pomo") || ""); if (p && p.enabled !== undefined) state.focus.pomo = p; } catch (e) {}
  state.focus.pomo = state.focus.pomo || { enabled: true, breakMin: 5 };
  if (localStorage.getItem("friday.ambient") === "1") toggleAmbient(true);
  // offline-sandbox banner: show when the server has no LLM key configured
  try {
    const ov = await api("/api/admin/overview");
    if (ov.llm_provider && String(ov.llm_provider).includes("sim")) {
      const b = $("#offline-banner");
      b.classList.remove("hidden");
      // this page IS served through the tunnel — point at the current origin
      $("#offline-banner-link").textContent = location.origin;
      $("#offline-banner-link").style.cursor = "pointer";
      $("#offline-banner-link").onclick = () => location.href = location.origin;
    }
  } catch (e) {}
  // restore previous chats and preload every panel — a hard refresh must show
  // everything immediately, no empty panels until you click them
  restoreChat();
  Promise.all([loadTasks(), loadMemory(), loadFocus(), loadBooks(), loadAdmin()]).catch(() => {});
  setInterval(async () => {           // keep focus state fresh for the sensor
    try {
      const a = await api("/api/focus/active");
      const was = state.focus.active;
      const now = a.session;
      const changed = (was && !now) || (!was && now) || (was && now && (was.session_id !== now.session_id || was.drift_count !== now.drift_count || was.mode !== now.mode));
      state.focus.active = now;
      state.focus.total = now ? now.target_min : 25;
      state.focus.ends = now ? now.start_ts + now.target_min * 60 : 0;
      if (changed) loadFocus();          // full re-render on state change
      else renderFocusWidget(now);       // lightweight time refresh otherwise
    } catch (e) {}
  }, 3000);
  setInterval(pollFocus, 1000);
  setInterval(async () => { if (state.view === "tasks") loadTasks(); }, 8000);
  setInterval(async () => { if (state.view === "focus") loadFocus(); }, 15000);
  pollOverview();
  nudgeStream();
  // theme from live settings
  try {
    const r = await api("/api/admin/settings");
    if (r && r["ui.theme"]) applyTheme(r["ui.theme"]);
  } catch (e) {}
})();
