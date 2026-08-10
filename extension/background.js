// Friday Sensor — MV3 background service worker.
// - learns your browsing (page visits → /api/ext/observe, 0 LLM)
// - during a focus session, drift → /api/focus/drift → coalesced nudges
// - chrome notifications + voice nudge are delivered here (UI toasts happen
//   in the PWA itself via /api/nudges/stream)
"use strict";

const DEFAULT_CFG = {
  server: "http://localhost:8000",   // set to your nip.io URL in options
  focusNudges: true,
  voiceNudges: true,
  chromeNudges: true,
  observeAll: true,                  // page learning always on (you asked for it)
  minVisitMs: 1500,
};

let cfg = { ...DEFAULT_CFG };
let focusActive = false;
let lastObserve = 0;

chrome.storage.local.get(DEFAULT_CFG, (c) => { cfg = { ...DEFAULT_CFG, ...c }; });
chrome.storage.onChanged.addListener((ch, area) => {
  if (area === "local") chrome.storage.local.get(DEFAULT_CFG, (c) => { cfg = { ...DEFAULT_CFG, ...c }; });
});

async function post(path, body) {
  try {
    const r = await fetch(cfg.server + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error("http " + r.status);
    return await r.json();
  } catch (e) {
    console.warn("friday-sensor:", path, e.message);
    return null;
  }
}

async function getFocusState() {
  try {
    const r = await fetch(cfg.server + "/api/focus/active");
    const d = await r.json();
    return d.session;
  } catch (e) { return null; }
}

async function observePage(tab) {
  if (!cfg.observeAll || !tab || !tab.url || tab.url.startsWith("chrome")) return;
  const now = Date.now();
  if (now - lastObserve < 3000) return;         // coalesce rapid tab switches
  lastObserve = now;
  const body = { url: tab.url.slice(0, 500), title: (tab.title || "").slice(0, 200), text: "" };
  // content script supplies extracted text via storage when available
  chrome.storage.session.get({ pageText: "" }, (s) => {
    body.text = (s.pageText || "").slice(0, 400);
    chrome.storage.session.remove("pageText");
    post("/api/ext/observe", body);
  });
}

// ---- page learning (always on) ----
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === "complete") observePage(tab);
});
chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  observePage(tab);
  // focus-mode drift check
  if (cfg.focusNudges) checkDrift(tab);
});

// ---- focus drift ----
async function checkDrift(tab) {
  if (!tab || !tab.url || tab.url.startsWith("chrome")) return;
  const s = await getFocusState();
  if (!s) return;
  const allow = JSON.parse(s.allow_domains || "[]");
  const domain = (() => { try { return new URL(tab.url).hostname; } catch (e) { return ""; } })();
  if (allow.some((d) => domain === d || domain.endsWith("." + d) || d.endsWith(domain))) return;
  const res = await post("/api/focus/drift", { url: tab.url.slice(0, 500), title: tab.title || "" });
  if (!res) return;
  for (const n of res.nudges || []) {
    if (n.channel === "chrome" && cfg.chromeNudges) {
      chrome.notifications.create("friday-drift", {
        type: "basic",
        iconUrl: "icon128.png",
        title: "🎯 Friday — focus",
        message: n.message,
        priority: 2,
      });
    }
    if (n.channel === "voice" && cfg.voiceNudges) {
      // Voice is delivered by the PWA (speechSynthesis); here we surface a
      // chrome notification so it is never missed.
      chrome.notifications.create("friday-drift-voice", {
        type: "basic", iconUrl: "icon128.png",
        title: "🎯 Friday — voice nudge", message: n.message, priority: 2,
      });
    }
  }
}

chrome.notifications.onClicked.addListener((id) => {
  chrome.notifications.clear(id);
  chrome.tabs.create({ url: cfg.server });
});

// keep the worker alive + periodic focus state refresh
chrome.alarms.create("friday-heartbeat", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(() => { /* heartbeat */ });
