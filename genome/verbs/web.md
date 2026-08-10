# verb: web

- `web.search(query, max=6)` → [ {title,url,snippet} ] — live search (Tavily/Brave/Exa on VM; sim fixtures offline)
- `web.read(url, max_chars=12000)` → markdown text — Jina reader on VM, direct fetch+extract fallback
- `web.speculative` — a search may already be in flight for your query (Hermes pre-fire); consume it with `web.latest_evidence()`

Rules: verify current facts with tools. Cite with [n] and the source URL. Never invent a price or date.
