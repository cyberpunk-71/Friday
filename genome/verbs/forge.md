# verb: forge (code execution)

- `forge.run(code, timeout_s=60)` — execute Python in the E2B/Firecracker sandbox
- The sandbox has `import friday` SDK: `friday.web`, `friday.fs`, `friday.gmail`, `friday.drive`, `friday.stripe`, `friday.github`, `friday.browser`, `friday.docling`, `friday.mcp`, `friday.sheets`, `friday.calendar`, `friday.whatsapp`, `friday.telegram`, `friday.slack`, `friday.notion`, `friday.search`
- `friday.mcp.call(app, action, params)` → 8000+ Zapier apps; the catalog is vector-searched INSIDE the sandbox (0 context cost)

Rules: write real Python, not pseudo-code. Assert postconditions yourself and return structured JSON. Speculative results may be discarded free.
