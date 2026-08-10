"use strict";
const DEFAULTS = {
  server: "http://localhost:8000",
  observeAll: true, focusNudges: true, chromeNudges: true, voiceNudges: true,
};
chrome.storage.local.get(DEFAULTS, (c) => {
  document.getElementById("server").value = c.server;
  document.getElementById("observeAll").checked = c.observeAll;
  document.getElementById("focusNudges").checked = c.focusNudges;
  document.getElementById("chromeNudges").checked = c.chromeNudges;
  document.getElementById("voiceNudges").checked = c.voiceNudges;
});
document.getElementById("save").addEventListener("click", () => {
  chrome.storage.local.set({
    server: document.getElementById("server").value.trim() || DEFAULTS.server,
    observeAll: document.getElementById("observeAll").checked,
    focusNudges: document.getElementById("focusNudges").checked,
    chromeNudges: document.getElementById("chromeNudges").checked,
    voiceNudges: document.getElementById("voiceNudges").checked,
  }, () => {
    const s = document.getElementById("status");
    s.textContent = "saved ✓";
    setTimeout(() => { s.textContent = ""; }, 1500);
  });
});
