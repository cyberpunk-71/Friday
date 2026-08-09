# verb: act (MCP / 8000 apps)

- `mcp.call(app, action, params)` — Zapier/MCP app actions (e.g. mcp.call("calendly","create_event",{...}))
- `act.github({repo, branch, files})` — branch + PR, never force-push
- `act.drive({op:"read|write|list", path})` — revisions kept
- `act.gmail({op})`, `act.sheets({op})`, `act.calendar({op})`, `act.slack({op})`, `act.notion({op})`

Rules: external writes are reversible-class unless the escalation rules say blocking. Always log the API call as a tool_result event with the request id.
