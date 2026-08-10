# verb: mail

- `mail.draft({to, subject, body, register})` — never sends; produces a draft artifact + inline approve card
- `mail.send({to, subject, body})` — BLOCKING GATE: explicit approval required (the other one of exactly 2)

Rules: >5 external recipients auto-escalates. Draft in the user's register (style profile). Attach artifacts by id.
