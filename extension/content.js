// Friday Sensor — content script. Extracts page text for learning
// (device-side, 0 LLM on the VM) and reports it to the background worker.
"use strict";
(() => {
  if (window.__fridaySensor) return;
  window.__fridaySensor = true;

  const sample = () => {
    try {
      const body = document.body;
      if (!body) return "";
      const text = (body.innerText || "").trim();
      // first 400 chars of the main content, title-weighted
      return text.slice(0, 400);
    } catch (e) { return ""; }
  };

  // report on load + visibility changes (SPA navigation)
  const report = () => {
    try {
      chrome.storage.session.set({ pageText: sample() });
    } catch (e) {}
  };
  setTimeout(report, 1200);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) setTimeout(report, 600);
  });
})();
