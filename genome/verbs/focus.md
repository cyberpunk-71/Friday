# verb: focus

- `focus.start({minutes, allow:["github.com"], voice:true})`
- `focus.stop()`
- `focus.stats()` — sessions, drift timeline, learned patterns
- `focus.allow_domain(d)` / `focus.block_domain(d)`

Rules: during a session, the extension logs every page; drifts nudge coalesced (1 toast + 1 chrome per 10 min per kind), voice nudge after 3rd drift. Phone notifications suppressed per-kind if the user asked.
