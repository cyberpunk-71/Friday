# verb: browse

- `browser.open(url)` — headless chromium; returns DOM snapshot
- `browser.screenshot(url)` → PNG artifact (e.g. product card for a saree)
- `browser.extract(selector)` — structured extraction
- `browser.fill(selector, value)` + `browser.click(selector)` — form automation (login flows only with explicit user consent)

Rules: any DOM containing card/password/cvv/otp fields → auto-escalate to blocking. Every session is recorded for replay (session replay).
