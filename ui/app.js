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

/* markdown-lite renderer: code, bold, italic, links, lists, tables, headers */
function md(text) {
  if (!text) return "";
  let t = esc(text);
  const blocks = [];
  t = t.replace(/```([\s\S]*?)```/g, (_, c) => { blocks.push(`<pre>${c}</pre>`); return `\u0000${blocks.length - 1}\u0000`; });
  t = t.replace(/`([^`]+)`/g, "<code>$1</code>");
  t = t.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  t = t.replace(/\*([^*]+)\*/g, "<i>$1</i>");
  t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const tableRe = /((?:\|.*\|(?:\r?\n|$))+)/g;
  t = t.replace(tableRe, (tb) => {
    const rows = tb.trim().split("\n").map(r => r.replace(/^\||\|$/g, "").split("|").map(c => c.trim()));
    if (rows.length < 2) return tb;
    const head = rows[0], body = rows.slice(2).length ? rows.slice(2) : rows.slice(1);
    return "<table><thead><tr>" + head.map(h => `<th>${h}</th>`).join("") + "</tr></thead><tbody>" +
      body.map(r => "<tr>" + r.map(c => `<td>${c}</td>`).join("") + "</tr>").join("") + "</tbody></table>";
  });
  t = t.replace(/^### (.*)$/gm, "<h4>$1</h4>");
  t = t.replace(/^## (.*)$/gm, "<h4>$1</h4>");
  t = t.replace(/^# (.*)$/gm, "<h4>$1</h4>");
  t = t.replace(/^[-*] (.*)$/gm, "<li>$1</li>");
  t = t.replace(/(<li>[\s\S]*?<\/li>)/g, (m) => `<ul>${m}</ul>`);
  t = t.replace(/<\/ul><ul>/g, "");
  t = t.replace(/^(\d+)\. (.*)$/gm, "<li>$2</li>");
  t = t.replace(/(<li>[\s\S]*?<\/li>)/g, (m) => `<ol>${m}</ol>`);
  t = t.replace(/<\/ol><ol>/g, "");
  t = t.split("\n").map(p => p.trim() ? (p.startsWith("<") ? p : `<p>${p}</p>`) : "").join("");
  t = t.replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
  return t;
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

function addMsg(role, html) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + role;
  wrap.innerHTML = `<div class="who">${role === "user" ? "YOU" : "FRIDAY"}</div><div class="bubble">${html}</div>`;
  $("#chat-stream").appendChild(wrap);
  scrollChat();
  return wrap;
}
function scrollChat() { const el = $("#chat-scroll"); el.scrollTop = el.scrollHeight; }

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
  addMsg("user", md(text));
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
      const wrap = document.createElement("div");
      wrap.className = "msg friday";
      wrap.innerHTML = `<div class="who">FRIDAY</div><div class="bubble">${md(ev.reply || streamingReply || "")}</div>`;
      $("#chat-stream").appendChild(wrap);
      scrollChat();
      state.chat.push({ role: "friday", text: ev.reply || streamingReply });
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

/* ============================== focus panel ============================== */
$("#focus-start").addEventListener("click", async () => {
  const minutes = +$("#focus-minutes").value || 25;
  const allow = $("#focus-allow").value.split(",").map(s => s.trim()).filter(Boolean);
  const r = await api("/api/focus/start", { method: "POST", body: JSON.stringify({ minutes, allow }) });
  toast(r.ok ? `🎯 Focus ${minutes}m started` : `focus: ${esc(r.error)}`);
  armNotifications();      // ask for notification permission so drift nudges are visible
  loadFocus();
});
$("#focus-stop").addEventListener("click", async () => {
  await api("/api/focus/stop", { method: "POST" });
  toast("Focus stopped");
  loadFocus();
});
async function loadFocus() {
  const [active, stats] = await Promise.all([api("/api/focus/active"), api("/api/focus/stats")]);
  const s = active.session;
  if (s) {
    state.focus.active = s; state.focus.total = s.target_min;
    state.focus.ends = s.start_ts + s.target_min * 60;
    $("#focus-start").classList.add("hidden");
    $("#focus-stop").classList.remove("hidden");
    $("#focus-live").innerHTML = `<div class="dim">session #${s.session_id} · allow: ${esc(JSON.parse(s.allow_domains || "[]").join(", ") || "none")} · drifts: ${s.drift_count}</div>
      <div class="dim" style="margin-top:6px">📡 Drift sensor: this tab is tracked (tab-switch = drift). For full browser tracking install the extension: <a href="/api/extension/zip" download style="color:var(--accent)">friday-sensor.zip</a> → chrome://extensions → Load unpacked.</div>`;
  } else {
    state.focus.active = null;
    $("#focus-start").classList.remove("hidden");
    $("#focus-stop").classList.add("hidden");
    $("#focus-live").innerHTML = `<div class="dim">no active session</div>`;
  }
  const l = stats.learned || {};
  $("#focus-learned").innerHTML = `
    <div class="dim">peak drift hours: ${(l.peak_hours || []).map(h => `${h}:00`).join(", ") || "—"}</div>
    <div class="dim" style="margin-top:6px">top distraction domains: ${(l.top_distraction_domains || []).map(esc).join(", ") || "—"}</div>
    <div class="dim" style="margin-top:6px">total drifts tracked: ${stats.total_drifts || 0}</div>`;
  $("#focus-history").innerHTML = (stats.sessions || []).map(s => `
    <div class="hist-row"><span>#${s.session_id} · ${fmtDate(s.start_ts)}</span>
      <span class="task-status ${esc(s.status)}">${esc(s.status)}</span>
      <span>${Math.round((s.end_ts || Date.now() / 1000 - s.start_ts) / 60)}/${s.target_min}m</span>
      <span>drifts ${s.drift_count}</span></div>`).join("") || `<div class="dim">no sessions yet</div>`;
}

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
  } catch (e) {}
}
function pollFocus() {
  const s = state.focus.active;
  const w = $("#focus-widget");
  if (s) {
    w.classList.remove("hidden");
    const left = Math.max(0, state.focus.ends - Date.now() / 1000);
    const pct = Math.max(0, Math.min(1, 1 - left / (state.focus.total * 60)));
    $("#focus-ring-fg").style.strokeDashoffset = (163.4 * (1 - pct)).toFixed(1);
    const m = Math.floor(left / 60), sec = Math.floor(left % 60);
    $("#focus-time").textContent = `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
    $("#focus-drifts").textContent = `drifts: ${s.drift_count}`;
  } else {
    w.classList.add("hidden");
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
          }
        } catch (e) {}
      }
    }
  } catch (e) {}
  setTimeout(nudgeStream, 4000);
}

function applyTheme(t) { document.documentElement.dataset.theme = t; }

/* ============================== boot ============================== */
(async function boot() {
  setInterval(pollOverview, 10000);
  // offline-sandbox banner: show when the server has no LLM key configured
  try {
    const ov = await api("/api/admin/overview");
    if (ov.llm_provider && String(ov.llm_provider).includes("sim")) {
      const b = $("#offline-banner");
      b.classList.remove("hidden");
      $("#offline-banner-link").textContent = "https://copyrighted-recognition-plane-undertake.trycloudflare.com";
      $("#offline-banner-link").style.cursor = "pointer";
      $("#offline-banner-link").onclick = () => location.href = "https://copyrighted-recognition-plane-undertake.trycloudflare.com";
    }
  } catch (e) {}
  setInterval(async () => {           // keep focus state fresh for the sensor
    try {
      const a = await api("/api/focus/active");
      if (a.session) { state.focus.active = a.session; state.focus.total = a.session.target_min;
                       state.focus.ends = a.session.start_ts + a.session.target_min * 60; }
      else state.focus.active = null;
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
  addMsg("friday", md("**Friday–Δ is live.** One chat for everything — research, tasks, memory, focus, books, settings. Ask me anything, or try: *\"research phone undr 20k and draft mail to sarh\"*, *\"dark mode kar do\"*, *\"start focos 25m allow github\"*."));
})();
