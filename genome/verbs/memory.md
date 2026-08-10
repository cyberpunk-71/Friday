# verb: memory

- `memory.recall(query, k=12)` → the 12 slots (identity/corrections/open loops/topical/tension/procedure) — already in your context as SLOTS; call only for a narrower question
- `memory.write({kind, text, importance, entities})` — APPEND evidence to the river. Never "update" — supersede instead.
- `memory.claims()` → beliefs with α/β/stability
- `memory.tensions()` → open contradictions
- `memory.agent_sql(sql)` → read-only SQL over the DELTA views when recall confidence is low (GROUP BY/HAVING questions)

Rules: user corrections (weight 10) outrank any inference (0.3). Never write a fact a correction contradicts.
