/* Friday Focus — clean, minimal body-doubling website. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) { let m = res.statusText; try { m = (await res.json()).detail || m; } catch (e) {} throw new Error(m); }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}
function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toast-stack").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}
function bdFmt(s) {
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

const state = {
  view: "focus",
  session: null, stats: null,
  total: 25, ends: 0,
  phase: "warmup",
  onBreak: false, breakEnd: 0, breakDone: false,
  round: 1, comebacks: 0,
  lastMilestone: 0, lastSessionId: null,
  celebrating: false, lastSummary: null,
  coach: [], coachTab: "coach",
  pomo: { enabled: true, breakMin: 5 },
  sound: { on: false, kind: "noise", ctx: null },
};

const LINES = {
  start: ["I'm here with you. Same room, same timer. Let's work.", "We're in this together — starting now. No pressure, just begin."],
  milestone: { 1: "Quarter in — nice. You've started, that's the win.", 2: "Halfway! You're doing the thing. Reward's getting closer.", 3: "Almost done — one last push. I'm right here." },
  deep: ["Deep work zone — I'll stay quiet and hold the space.", "You're in flow. I can feel it. Keep going."],
  push: ["Final push! Home stretch. One last burst — you've got this.", "Almost there. Finish strong, then the reward is yours."],
  back: ["Welcome back 🧡 Resetting the clock — you've got this.", "Back on track! That comeback just counted. We move."],
  drift: ["You drifted to {d} — no judgment, come back when you're ready.", "Hey, noticed {d}. It happens. Take a breath, come back."],
  break: ["Break time ☕ Stand up, shake out your hands. You earned it.", "Pause — water, stretch, blink. Your brain needs the reset."],
  breakOver: ["Break's over — ready for round {n}? No pressure. Just start.", "Round {n} time. Tiny step first. Let's go."],
  thought: ["Got it — I'm holding that thought for you. Back to work. 🧠", "Safe with me. That's one less thing your brain has to carry."],
  end: ["That's a wrap 🎉 {m} min focused{t}, {d} drift{s}. You showed up.", "Done — {m} minutes{t}. Look at you. Rest, then reward."],
};
const rnd = (a) => a[Math.floor(Math.random() * a.length)];
const log = [];           // companion lines
function say(m, k = "") { log.push({ m, k, ts: Date.now() }); renderLog(); }
const PHASE_LABEL = { warmup: "WARM-UP", deep: "DEEP WORK", push: "FINAL PUSH", break: "BREAK" };

/* ---------- sound ---------- */
function chime(kind) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const t0 = ctx.currentTime;
    const notes = {
      start: [[523.25, 0, .35], [783.99, .12, .45]],
      warn: [[659.25, 0, .3], [659.25, .2, .35]],
      workEnd: [[783.99, 0, .35], [659.25, .18, .4], [523.25, .36, .5]],
      breakEnd: [[523.25, 0, .3], [659.25, .15, .35], [783.99, .3, .5]],
      win: [[523.25, 0, .25], [659.25, .12, .25], [783.99, .24, .3], [1046.5, .38, .6]],
    }[kind] || [[523.25, 0, .35]];
    notes.forEach(([f, at, dur]) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t0 + at);
      g.gain.exponentialRampToValueAtTime(0.11, t0 + at + .02);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + at + dur);
      o.connect(g); g.connect(ctx.destination);
      o.start(t0 + at); o.stop(t0 + at + dur + .05);
    });
    setTimeout(() => ctx.close(), 1800);
  } catch (e) {}
}
function toggleSound(kind) {
  if (state.sound.ctx) { try { state.sound.ctx.close(); } catch (e) {} state.sound = { on: false, kind: "noise", ctx: null }; }
  if (!kind) { localStorage.setItem("ff.sound", "0"); return; }
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const len = ctx.sampleRate * 2;
    const buf = ctx.createBuffer(1, len, ctx.sampleRate);
    const d = buf.getChannelData(0);
    let last = 0;
    if (kind === "rain") {
      let wob = 0;
      for (let i = 0; i < len; i++) {
        const w = Math.random() * 2 - 1;
        last = (last + 0.028 * w) / 1.028;
        wob += (Math.random() * 2 - 1) * 0.004; wob *= 0.9995;
        d[i] = last * 4.2 * (0.75 + 0.25 * Math.sin(i / 2400 + wob * 40));
      }
    } else {
      for (let i = 0; i < len; i++) { const w = Math.random() * 2 - 1; last = (last + 0.02 * w) / 1.02; d[i] = last * 3.5; }
    }
    const s = ctx.createBufferSource(); s.buffer = buf; s.loop = true;
    const f = ctx.createBiquadFilter(); f.type = "lowpass"; f.frequency.value = kind === "rain" ? 1600 : 900;
    const g = ctx.createGain(); g.gain.value = kind === "rain" ? 0.16 : 0.22;
    s.connect(f); f.connect(g); g.connect(ctx.destination);
    s.start();
    state.sound = { on: true, kind, ctx };
    localStorage.setItem("ff.sound", kind);
  } catch (e) {}
}

/* ---------- confetti ---------- */
function confetti() {
  const colors = ["#ff5e3a", "#ff9a3d", "#0ea5a4", "#34d399", "#a78bfa", "#fbbf24"];
  const host = document.createElement("div");
  host.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:600;overflow:hidden";
  document.body.appendChild(host);
  for (let i = 0; i < 50; i++) {
    const p = document.createElement("div");
    const size = 6 + Math.random() * 8;
    p.style.cssText = `position:absolute;left:${Math.random() * 100}%;top:-20px;width:${size}px;height:${size * 0.6}px;border-radius:2px;background:${colors[i % colors.length]};opacity:${0.7 + Math.random() * 0.3}`;
    host.appendChild(p);
    p.animate([
      { transform: "translate(0,0) rotate(0deg)", opacity: 1 },
      { transform: `translate(${(Math.random() - .5) * 200}px, 105vh) rotate(${360 + Math.random() * 540}deg)`, opacity: 0.6 },
    ], { duration: (2 + Math.random() * 2.5) * 1000, easing: "cubic-bezier(.2,.6,.3,1)", delay: Math.random() * 400 });
  }
  setTimeout(() => host.remove(), 6000);
}

/* ---------- phase ---------- */
function phase() {
  if (state.onBreak) return "break";
  const total = (state.session.target_min || 25) * 60;
  const el = Date.now() / 1000 - state.session.start_ts;
  const f = Math.min(1, el / total);
  if (f < 0.2) return "warmup";
  if (f >= 0.85) return "push";
  return "deep";
}
function checkMilestones() {
  const s = state.session; if (!s) return;
  const total = (s.target_min || 25) * 60;
  const el = Date.now() / 1000 - s.start_ts;
  const ms = Math.floor(Math.min(1, el / total) * 4);
  if (ms > state.lastMilestone) {
    state.lastMilestone = ms;
    if (LINES.milestone[ms]) say(LINES.milestone[ms], "checkin");
    else if (ms === 2 && s.why) say(`Halfway! Remember the why — “${s.why}” is waiting on the other side.`, "checkin");
  }
  const ph = phase();
  if (ph !== state.phase) {
    const prev = state.phase; state.phase = ph;
    if ((ph === "deep" && prev !== "deep") || (ph === "push" && prev !== "push")) {
      say(rnd(ph === "deep" ? LINES.deep : LINES.push), "checkin");
      chime("warn");
    }
  }
  const v = $("#view-focus");
  if (v) v.classList.remove("phase-warmup", "phase-deep", "phase-push", "phase-break", "phase-celebrate");
  if (v) v.classList.add("phase-" + (state.celebrating ? "celebrate" : ph));
}

/* ---------- focus view ---------- */
function greeting() {
  const h = new Date().getHours();
  if (h < 5) return "Up late? Impressive. Let's make it count.";
  if (h < 12) return "Good morning! Let's plan a session you'll actually enjoy.";
  if (h < 17) return "Hey! Perfect time to carve out some focus.";
  if (h < 22) return "Evening session — nice. Let's make it a good one.";
  return "Late-night focus crew. I'm here with you.";
}

async function startSession(o = {}) {
  const minutes = o.minutes || +($("#ff-minutes").value) || 25;
  const task = (o.task !== undefined ? o.task : $("#ff-task").value || "").trim();
  const why = ($("#ff-why").value || "").trim();
  const first = ($("#ff-first").value || "").trim();
  const plan = ($("#ff-plan").value || "").trim();
  const energy = +($("#ff-energy").dataset.val || 0);
  const mood = +($("#ff-mood").dataset.val || 0);
  state.pomo = { enabled: $("#ff-pomo").checked, breakMin: +($("#ff-break").value) || 5 };
  const r = await api("/api/focus/start", { method: "POST", body: JSON.stringify({
    minutes, task, why, first_step: first, distraction_plan: plan, energy, mood, voice: true }) });
  if (r.ok) {
    log.length = 0;
    say(o.justStart ? "5 minutes. That's it. Future-you says thanks — I'm right here." : rnd(LINES.start), "start");
    state.lastMilestone = 0; state.onBreak = false; state.breakDone = false; state.round = o.round || 1;
    state.comebacks = 0; state.celebrating = false;
    chime("start");
    refresh();
  } else toast(r.error || "couldn't start", "bad");
}

async function finishSession() {
  const moodAfter = +($("#ff-mood-after").dataset.val || 0);
  const notes = ($("#ff-notes").value || "").trim();
  const r = await api("/api/focus/finish", { method: "POST", body: JSON.stringify({ mood_after: moodAfter, notes }) });
  if (r.ok) {
    state.onBreak = false; state.lastSummary = r; state.celebrating = true;
    chime("win"); confetti();
    render();
  } else toast(r.error || "couldn't finish", "bad");
}
async function saveThought() {
  const box = $("#ff-thought");
  if (!box) return;
  const t = box.value.trim();
  if (!t) return;
  const r = await api("/api/focus/thought", { method: "POST", body: JSON.stringify({ text: t }) });
  if (r.ok) { say(rnd(LINES.thought), "checkin"); box.value = ""; }
}
async function recordComeback() {
  const r = await api("/api/focus/comeback", { method: "POST" });
  if (r.ok) { state.comebacks = r.comebacks; say(rnd(LINES.back), "checkin"); chime("start"); render(); }
}
async function startBreak() {
  state.onBreak = true; state.breakDone = false; state.breakEnd = Date.now() / 1000 + (state.pomo.breakMin || 5) * 60;
  await api("/api/focus/mode", { method: "POST", body: JSON.stringify({ mode: "break", break_min: state.pomo.breakMin || 5 }) }).catch(() => {});
  say(rnd(LINES.break), "break"); chime("workEnd"); render();
}
async function endBreak(next) {
  state.onBreak = false;
  await api("/api/focus/mode", { method: "POST", body: JSON.stringify({ mode: "work" }) }).catch(() => {});
  if (next) {
    const s = state.session;
    const n = (state.round || 1) + 1;
    await api("/api/focus/stop", { method: "POST" }).catch(() => {});
    await startSession({ minutes: state.total || 25, task: s.task, round: n });
    say(rnd(LINES.breakOver).replace("{n}", n), "checkin");
  } else { chime("breakEnd"); render(); }
}

function render() {
  if (state.celebrating && state.lastSummary) return renderCelebration();
  if (state.session) return renderActive();
  return renderIdle();
}
function renderIdle() {
  const host = $("#focus-host");
  if (!host) return;
  const st = state.stats || {}; const f = st.focus || {};
  host.innerHTML = `
    <div class="idle-wrap">
      <div class="companion">
        <div class="aura"></div>
        <img src="/static/companion.png" class="avatar" alt="Friday"/>
        <div class="c-name">Friday</div>
        <div class="c-status">${greeting()}</div>
      </div>
      <div class="card plan">
        <div class="plan-title">set the scene</div>
        <input id="ff-task" type="text" placeholder="One task. Just one." maxlength="120"/>
        <div class="hint dim">✳️ first tiny step</div>
        <input id="ff-first" type="text" placeholder="e.g. open the editor and write one function" maxlength="120"/>
        <div class="hint dim">🎁 reward after</div>
        <input id="ff-why" type="text" placeholder="e.g. then I watch one episode" maxlength="120"/>
        <div class="row2">
          <div><div class="hint dim">⚡ energy</div><div class="moods" id="ff-energy">${[["😴",1],["😪",2],["🙂",3],["💪",4],["🚀",5]].map(([e,v])=>`<button class="mood" data-val="${v}">${e}</button>`).join("")}</div></div>
          <div><div class="hint dim">😊 mood</div><div class="moods" id="ff-mood">${[["😖",1],["😕",2],["😐",3],["🙂",4],["😄",5]].map(([e,v])=>`<button class="mood" data-val="${v}">${e}</button>`).join("")}</div></div>
        </div>
        <div class="hint dim">🧯 distraction pre-commit</div>
        <input id="ff-plan" type="text" placeholder="when I want to check ___, I'll ___ instead" maxlength="140"/>
        <div class="presets">
          ${[15,25,45,60].map(m=>`<button class="preset ${m===25?"active":""}" data-min="${m}">${m}m</button>`).join("")}
          <input id="ff-minutes" type="number" value="25" min="1" max="180"/>
        </div>
        <div class="opts">
          <label><input id="ff-pomo" type="checkbox" ${state.pomo.enabled?"checked":""}/> pomodoro</label>
          <input id="ff-break" type="number" value="${state.pomo.breakMin||5}" min="2" max="20" style="width:56px"/> <span class="dim">min</span>
          <span class="spacer"></span>
          <button id="ff-snd-off" class="snd ${!state.sound.on?"active":""}">off</button>
          <button id="ff-snd-noise" class="snd ${state.sound.on&&state.sound.kind==="noise"?"active":""}">noise</button>
          <button id="ff-snd-rain" class="snd ${state.sound.on&&state.sound.kind==="rain"?"active":""}">rain</button>
        </div>
        <button id="ff-go" class="btn primary big">Begin — I'm here with you</button>
        <button id="ff-5" class="btn ghost big">⚡ Just 5 minutes</button>
      </div>
      <div class="mini-stats dim">
        ${f.avg_score?`avg score <b>${f.avg_score}</b> · `:""}${st.streak_days?`streak <b>${st.streak_days}d</b> 🔥 · `:""}${f.best_hour!=null?`best hour <b>${f.best_hour}:00</b> · `:""}${st.week?`${st.week.minutes}m this week`:""}
        ${!f.avg_score&&!st.streak_days?`your sanctuary is waiting for its first seed 🌱`:""}
      </div>
    </div>`;
  ["ff-energy","ff-mood"].forEach(id => {
    const w = $("#"+id); if (!w) return;
    $$(".mood", w).forEach(b => b.addEventListener("click", () => { $$(".mood", w).forEach(x=>x.classList.toggle("active",x===b)); w.dataset.val = b.dataset.val; }));
  });
  $$(".preset").forEach(b => b.addEventListener("click", () => { $$(".preset").forEach(x=>x.classList.toggle("active",x===b)); $("#ff-minutes").value = b.dataset.min; }));
  $("#ff-go").addEventListener("click", () => startSession());
  $("#ff-5").addEventListener("click", () => startSession({ minutes: 5, justStart: true }));
  $("#ff-snd-off").addEventListener("click", () => { toggleSound(null); renderIdle(); });
  $("#ff-snd-noise").addEventListener("click", () => { toggleSound("noise"); renderIdle(); });
  $("#ff-snd-rain").addEventListener("click", () => { toggleSound("rain"); renderIdle(); });
}
function renderActive() {
  const host = $("#focus-host");
  if (!host) return;
  const s = state.session;
  const onBreak = state.onBreak && s.mode === "break";
  const left = onBreak ? Math.max(0, (s.break_end_ts || state.breakEnd || 0) - Date.now()/1000)
                       : Math.max(0, (s.start_ts + s.target_min*60) - Date.now()/1000);
  const el = Math.floor((Date.now()/1000 - s.start_ts)/60);
  const ph = onBreak ? "break" : phase();
  host.innerHTML = `
    <div class="active-wrap">
      <div class="companion small">
        <div class="aura ${onBreak?"teal":""}"></div>
        <img src="/static/companion.png" class="avatar ${onBreak?"":"pulse"}" alt="Friday"/>
        <div class="c-status">${onBreak?"on break with you ☕":"working alongside you · in flow"}</div>
      </div>
      <div class="timer-card phase-${ph}">
        ${onBreak?`<div class="breathe"><div class="breathe-ring"><div class="breathe-dot"></div></div><div class="breathe-hint">breathe in 4 · hold 7 · out 8</div></div>`:""}
        <div class="phase-label">${PHASE_LABEL[ph]}</div>
        <div class="timer" id="ff-timer">${bdFmt(left)}</div>
        <div class="task-line">${onBreak?"rest · water · breathe":(s.task?`🎯 ${esc(s.task)}`:"deep work")}</div>
        ${!onBreak&&s.first_step?`<div class="dim">first step: ${esc(s.first_step)}</div>`:""}
        ${!onBreak&&s.why?`<div class="reward">🎁 after: ${esc(s.why)}</div>`:""}
        ${!onBreak&&s.distraction_plan?`<div class="dim">🧯 ${esc(s.distraction_plan)}</div>`:""}
        <div class="stats dim">${el}m elapsed · drifts ${s.drift_count||0} · comebacks ${state.comebacks}${state.round>1?` · round ${state.round}`:""}</div>
        <div class="actions">
          ${onBreak
            ? `<button id="ff-next" class="btn primary">▶ Next round</button><button id="ff-breakdone" class="btn ghost">I'm done</button>`
            : `<button id="ff-finish" class="btn primary">🏁 Finish</button><button id="ff-plus" class="btn ghost">+5</button>`}
          <button id="ff-snd" class="btn ghost">${state.sound.on?(state.sound.kind==="rain"?"🌧 rain":"🔊 noise"):"🔈 sound"}</button>
        </div>
        ${!onBreak?`
        <div class="brain">
          <input id="ff-thought" type="text" placeholder="🧠 brain dump — I'll hold it for you" maxlength="400"/>
          <button id="ff-thought-go" class="btn ghost">Save</button>
        </div>`:""}
        ${!onBreak&&s.drift_count>0?`<button id="ff-back" class="btn ghost back">🧡 I'm back — count it</button>`:""}
      </div>
      <div class="coach-card card">
        <div class="coach-tabs">
          <button class="ctab ${state.coachTab==="coach"?"active":""}" data-t="coach">💬 Coach</button>
          <button class="ctab ${state.coachTab==="log"?"active":""}" data-t="log">📜 Log</button>
        </div>
        <div id="pane-coach" class="pane ${state.coachTab==="coach"?"":"hidden"}">
          <div id="coach-msgs" class="coach-msgs"></div>
          <div class="coach-chips">
            ${["I'm stuck","2-minute version?","Why am I doing this?","I want to quit","What's next?"].map(c=>`<button class="chip-btn" data-q="${esc(c)}">${esc(c)}</button>`).join("")}
          </div>
          <div class="coach-row">
            <input id="ff-coach" type="text" placeholder="talk to your coach…" maxlength="300"/>
            <button id="ff-coach-go" class="btn ghost">Send</button>
          </div>
        </div>
        <div id="pane-log" class="pane ${state.coachTab==="log"?"":"hidden"}"><div id="log" class="log"></div></div>
      </div>
    </div>`;
  $("#ff-finish")?.addEventListener("click", () => finishSession());
  $("#ff-plus")?.addEventListener("click", async () => {
    await api("/api/focus/stop", { method: "POST" }).catch(()=>{});
    const mins = Math.max(1, Math.round(left/60) + 5);
    await startSession({ minutes: mins, task: s.task });
    say("I added 5 minutes — we're in this together.");
  });
  $("#ff-next")?.addEventListener("click", () => endBreak(true));
  $("#ff-breakdone")?.addEventListener("click", () => endBreak(false));
  $("#ff-back")?.addEventListener("click", () => recordComeback());
  $("#ff-thought-go")?.addEventListener("click", () => saveThought());
  const ti = $("#ff-thought"); if (ti) ti.addEventListener("keydown", e => { if (e.key === "Enter") saveThought(); });
  const ci = $("#ff-coach"); if (ci) ci.addEventListener("keydown", e => { if (e.key === "Enter") sendCoach(ci.value); });
  $("#ff-coach-go")?.addEventListener("click", () => sendCoach($("#ff-coach").value));
  $$(".chip-btn").forEach(b => b.addEventListener("click", () => sendCoach(b.dataset.q)));
  $$(".ctab").forEach(t => t.addEventListener("click", () => {
    $$(".ctab").forEach(x => x.classList.toggle("active", x === t));
    state.coachTab = t.dataset.t;
    renderActive();
  }));
  $("#ff-snd")?.addEventListener("click", () => {
    const next = !state.sound.on ? "noise" : state.sound.kind === "noise" ? "rain" : null;
    toggleSound(next); renderActive();
  });
  renderLog(); renderCoach();
}
function renderCelebration() {
  const host = $("#focus-host");
  if (!host) return;
  const r = state.lastSummary;
  const grade = r.focus_score >= 85 ? "FOCUS LEGEND" : r.focus_score >= 70 ? "FOCUS MASTER" : r.focus_score >= 50 ? "SOLID FOCUS" : "YOU SHOWED UP";
  const refl = r.drifts === 0 ? "Zero drifts. A still, focused mind — rare and precious."
    : r.comebacks >= r.drifts ? `${r.drifts} drift${r.drifts===1?"":"s"}, ${r.comebacks} comeback${r.comebacks===1?"":"s"} — you return to the work. That's the muscle.`
    : r.drifts > 3 ? `${r.drifts} drifts is just today's weather. You stayed in the room — that's the practice.`
    : "You showed up, did the thing, and came back to yourself.";
  host.innerHTML = `
    <div class="celebration card">
      <div class="celebrate-head">a quiet moment, well spent</div>
      <div class="score-ring" style="--score:${Math.max(2, r.focus_score)}"><div class="score-inner"><div class="score-num">${r.focus_score}</div><div class="score-label">focus score</div></div></div>
      <div class="grade">${grade}</div>
      <div class="reflect">${refl}</div>
      ${r.task?`<div class="task-line">🎯 ${esc(r.task)}</div>`:""}
      <div class="plant">🌱 planted in your garden${r.why?` · ${esc(r.why)} 🎁`:""}</div>
      <div class="after">
        <div class="hint dim">How do you feel now?</div>
        <div class="moods center" id="ff-mood-after">${[["😖",1],["😕",2],["😐",3],["🙂",4],["😄",5]].map(([e,v])=>`<button class="mood" data-val="${v}">${e}</button>`).join("")}</div>
        <input id="ff-notes" type="text" placeholder="one line for later-you (optional)…" maxlength="300"/>
      </div>
      <button id="ff-done" class="btn primary big">Save & see my record</button>
    </div>`;
  const w = $("#ff-mood-after");
  $$(".mood", w).forEach(b => b.addEventListener("click", () => { $$(".mood", w).forEach(x=>x.classList.toggle("active",x===b)); w.dataset.val = b.dataset.val; }));
  $("#ff-done").addEventListener("click", () => {
    const sel = w.querySelector(".mood.active");
    if (sel) w.dataset.val = sel.dataset.val;
    state.celebrating = false;
    refresh();
  });
}
function renderLog() {
  const el = $("#log"); if (!el) return;
  el.innerHTML = log.map(l => `<div class="line ${l.k}"><img src="/static/companion.png" alt=""/><span>${esc(l.m)}</span><span class="dim ts">${new Date(l.ts).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})}</span></div>`).join("") || `<div class="dim">I'm here.</div>`;
  el.scrollTop = el.scrollHeight;
}
function renderCoach() {
  const box = $("#coach-msgs"); if (!box) return;
  box.innerHTML = state.coach.map(m => `<div class="cmsg ${m.role==="user"?"you":"friday"}"><span class="cb">${esc(m.text)}</span></div>`).join("")
    || `<div class="dim" style="font-size:12px">I'm right here with you — stuck, tired, distracted, or just checking in, tell me.</div>`;
  box.scrollTop = box.scrollHeight;
}
async function sendCoach(text) {
  text = (text||"").trim(); if (!text) return;
  state.coach.push({ role: "user", text });
  state.coachTab = "coach"; renderActive();
  const box = $("#coach-msgs");
  const ty = document.createElement("div"); ty.className = "cmsg friday"; ty.innerHTML = `<span class="cb"><span class="td"></span><span class="td"></span><span class="td"></span></span>`;
  box.appendChild(ty); box.scrollTop = box.scrollHeight;
  let reply = "";
  try {
    const res = await fetch("/api/focus/coach", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: text }) });
    if (!res.ok || !res.body) throw new Error("coach " + res.status);
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value || new Uint8Array(), { stream: !done });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        for (const line of chunk.split("\n")) {
          if (!line.startsWith("data:")) continue;
          let ev; try { ev = JSON.parse(line.slice(5)); } catch (e) { continue; }
          if (ev.type === "delta") { reply += ev.text; ty.querySelector(".cb").textContent = reply; box.scrollTop = box.scrollHeight; }
        }
      }
    }
  } catch (e) {
    reply = "I'm right here. Smallest next step: do the first two minutes of your task, then come back.";
    ty.querySelector(".cb").textContent = reply;
  }
  state.coach.push({ role: "friday", text: reply });
  ty.remove(); renderCoach();
}

/* ---------- history view ---------- */
function garden(st) {
  const days = [];
  for (let i = 6; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86400000);
    days.push({ key: `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`, label: ["S","M","T","W","T","F","S"][d.getDay()], n: 0 });
  }
  (st.sessions||[]).forEach(s => {
    if (s.status === "completed" || s.status === "abandoned") {
      const d = new Date(s.start_ts*1000);
      const k = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
      const day = days.find(x => x.key === k); if (day) day.n++;
    }
  });
  return days.map(d => `<div class="gday"><span class="gplant">${d.n>=3?"🌳":d.n===2?"🌿":d.n===1?"🌱":"·"}</span><span>${d.label}</span></div>`).join("");
}
function renderHistory() {
  const host = $("#history-host");
  if (!host) return;
  const st = state.stats || {}; const f = st.focus || {};
  const scored = (st.sessions||[]).filter(s => s.focus_score != null).slice(0, 20).reverse();
  const max = Math.max(1, ...scored.map(s => s.focus_score || 0));
  host.innerHTML = `
    <div class="h-head"><h2>your sanctuary</h2></div>
    <div class="card garden">${garden(st)}</div>
    <div class="tiles">
      <div class="tile"><b>${st.total_completed||0}</b><span>sessions</span></div>
      <div class="tile"><b>${f.avg_score||0}</b><span>avg score</span></div>
      <div class="tile"><b>${f.best_score||0}</b><span>best</span></div>
      <div class="tile"><b>${st.streak_days||0}</b><span>streak 🔥</span></div>
      <div class="tile"><b>${st.week?st.week.minutes:0}m</b><span>this week</span></div>
      <div class="tile"><b>${f.avg_energy||0}/5</b><span>avg energy</span></div>
    </div>
    <div class="dim h-insight">${f.best_hour!=null?`you focus best around <b>${f.best_hour}:00</b>`:""}${f.energy_delta?` · energy ${f.energy_delta>0?"+":""}${f.energy_delta} after sessions`:""}</div>
    ${scored.length?`<div class="card"><h3>focus scores</h3><div class="chart">${scored.map(s=>`<div class="col" title="${s.focus_score}"><div class="bar" style="height:${Math.max(4,(s.focus_score/max)*100)}%"></div><span>${s.focus_score}</span></div>`).join("")}</div></div>`:""}
    ${(st.sessions||[]).filter(s=>s.thoughts).length?`<div class="card"><h3>🧠 thought bank</h3>${(st.sessions||[]).filter(s=>s.thoughts).slice(0,8).map(s=>`<div class="thoughts"><div class="dim">${new Date(s.start_ts*1000).toLocaleDateString()}${s.task?` · ${esc(s.task)}`:""}</div>${(s.thoughts||"").split("\n").map(t=>`<div>${esc(t)}</div>`).join("")}</div>`).join("")}</div>`:""}
    ${(st.sessions||[]).length?`<div class="card"><h3>journal</h3>${(st.sessions||[]).slice(0,10).map(s=>`<div class="jrow"><span class="chip ${s.status}">${esc(s.status)}</span><span>${new Date(s.start_ts*1000).toLocaleDateString()}</span><span><b>${Math.round(((s.end_ts||Date.now()/1000)-s.start_ts)/60)}</b>/${s.target_min}m</span>${s.focus_score!=null?`<span class="chip good">${s.focus_score}</span>`:""}${s.task?`<span class="dim">· ${esc(s.task)}</span>`:""}</div>`).join("")}</div>`:""}`;
}

/* ---------- settings view ---------- */
async function renderSettings() {
  const host = $("#settings-host");
  if (!host) return;
  const s = await api("/api/settings");
  host.innerHTML = `
    <div class="h-head"><h2>settings</h2></div>
    <div class="card">
      <h3>brain (LLM)</h3>
      <div class="row2">
        <div><div class="hint dim">provider</div>
          <select id="ff-provider"><option value="deepseek" ${s.llm_provider==="deepseek"?"selected":""}>DeepSeek</option><option value="gemini" ${s.llm_provider==="gemini"?"selected":""}>Gemini</option></select>
        </div>
        <div><div class="hint dim">model</div>
          <input id="ff-model" type="text" list="models" value="${esc(s.llm_model||"")}" placeholder="${s.llm_provider==="gemini"?"gemini-3.1-flash-lite":"deepseek-chat"}"/>
          <datalist id="models"><option value="gemini-3.1-flash-lite"></option><option value="gemini-3.5-flash"></option><option value="gemini-3-flash-preview"></option><option value="gemini-3.6-flash"></option><option value="deepseek-chat"></option><option value="deepseek-reasoner"></option></datalist>
        </div>
      </div>
      <button id="ff-model-save" class="btn primary">Apply model</button>
      <div class="hint dim" style="margin-top:10px">API key (${s.llm_provider})</div>
      <div class="row2">
        <input id="ff-key" type="password" placeholder="${s.llm_provider==="gemini"?"AIza… or AQ.Ab…":"sk-…"}"/>
        <button id="ff-key-save" class="btn primary">Save key</button>
      </div>
      <div id="ff-keystatus" class="dim" style="margin-top:6px">
        ${s.keys.map(k=>`<span class="chip">${esc(k.provider)} ${esc(k.masked)}</span>`).join(" ")||"no keys yet — add one above"}
      </div>
    </div>
    <div class="card">
      <h3>appearance</h3>
      <button id="ff-theme" class="btn ghost">Switch to ${s.theme==="dark"?"light":"dark"} mode</button>
    </div>
    <div class="card">
      <h3>about</h3>
      <div class="dim">Friday Focus v1.0.0 · built on Friday–Δ core · ${esc(s.llm_provider)}</div>
    </div>`;
  $("#ff-model-save").addEventListener("click", async () => {
    try {
      const r = await api("/api/settings/model", { method: "POST", body: JSON.stringify({ provider: $("#ff-provider").value, model: $("#ff-model").value }) });
      toast(`Model → ${r.provider} ${r.model||""}`, "good"); renderSettings();
    } catch (e) { toast(e.message, "bad"); }
  });
  $("#ff-key-save").addEventListener("click", async () => {
    const key = $("#ff-key").value.trim();
    if (!key) return toast("enter a key", "bad");
    try {
      const r = await api("/api/settings/key", { method: "POST", body: JSON.stringify({ provider: $("#ff-provider").value, api_key: key }) });
      toast(`${r.provider} key saved ${r.masked}`, "good");
      $("#ff-key").value = ""; renderSettings();
    } catch (e) { toast(e.message, "bad"); }
  });
  $("#ff-theme").addEventListener("click", async () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    await api("/api/settings/theme", { method: "POST", body: JSON.stringify({ theme: next }) }).catch(() => {});
    renderSettings();
  });
}

/* ---------- refresh + loops ---------- */
async function refresh() {
  const [active, stats] = await Promise.all([api("/api/focus/active"), api("/api/focus/stats")]);
  const was = state.session;
  const now = active.session;
  state.session = now; state.stats = stats;
  if (was && !now && !state.celebrating) {
    const mins = Math.max(0, Math.round((Date.now()/1000 - was.start_ts)/60));
    say(rnd(LINES.end).replace("{m}", mins).replace("{t}", was.task?` on “${was.task}”`:"").replace("{d}", was.drift_count||0).replace("{s}", (was.drift_count||0)===1?"":"s"), "end");
    toast("session ended", "chrome");
  }
  if (now && now.session_id !== state.lastSessionId) {
    state.lastSessionId = now.session_id;
    state.lastMilestone = 0; state.onBreak = false; state.comebacks = now.comebacks || 0; state.phase = "warmup";
    if (now.mode === "break") { state.onBreak = true; state.breakEnd = now.break_end_ts || 0; }
    if (!log.length) say(`I'm here with you — ${now.target_min} min${now.task?` on “${now.task}”`:""}. Same room, same timer. Let's work.`, "start");
  }
  if (now && now.mode === "break") { state.onBreak = true; state.breakEnd = now.break_end_ts || 0; }
  else if (now) state.onBreak = false;
  if (state.view === "focus") { checkMilestones(); render(); }
  if (state.view === "history") renderHistory();
}
setInterval(refresh, 3000);
setInterval(() => {
  if (state.view !== "focus" || !state.session || state.celebrating) return;
  const s = state.session;
  const onBreak = state.onBreak && s.mode === "break";
  const left = onBreak ? Math.max(0, (s.break_end_ts || state.breakEnd || 0) - Date.now()/1000)
                       : Math.max(0, (s.start_ts + s.target_min*60) - Date.now()/1000);
  const t = $("#ff-timer"); if (t) t.textContent = bdFmt(left);
  if (left <= 0 && !onBreak) {
    if (state.pomo.enabled) { state.breakDone = false; startBreak(); }
    else finishSession();
  }
  if (left <= 0 && onBreak && !state.breakDone) {
    state.breakDone = true;
    chime("breakEnd");
    say(rnd(LINES.breakOver).replace("{n}", (state.round||1)+1), "checkin");
  }
  checkMilestones();
}, 1000);

/* ---------- nav + boot ---------- */
$$(".nav-item").forEach(b => b.addEventListener("click", () => {
  $$(".nav-item").forEach(x => x.classList.toggle("active", x === b));
  state.view = b.dataset.view;
  $$(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + state.view));
  if (state.view === "focus") render();
  if (state.view === "history") renderHistory();
  if (state.view === "settings") renderSettings();
}));
$("#theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  api("/api/settings/theme", { method: "POST", body: JSON.stringify({ theme: next }) }).catch(() => {});
});
(async function boot() {
  const snd = localStorage.getItem("ff.sound");
  if (snd === "1" || snd === "noise") toggleSound("noise");
  else if (snd === "rain") toggleSound("rain");
  try {
    const s = await api("/api/settings");
    if (s.theme) document.documentElement.dataset.theme = s.theme;
  } catch (e) {}
  refresh();
})();
