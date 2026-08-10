# verb: tasks

- `tasks.create({title, description, autonomy, priority})` → task_id
- `tasks.plan(task_id, dag)` → DAG steps with postcondition assertions: each node declares `assert` (python expr) evaluated on its output; repair-call only on failed assert
- `tasks.approve(task_id)` / `tasks.reject(task_id)`
- `tasks.artifacts(task_id)` → produced files (downloadable from the panel)

Rules: exactly 2 blocking gates — payment, gmail_send. Everything else executes now but reversible (trash/revisions/branch+PR/session replay) with an inline [↩ Undo 89s] chip. Escalate on blast radius (>20 files, >5 ext recipients, any money, credential read, DOM with card/password).
