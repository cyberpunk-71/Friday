# verb: files

- `files.read(path)` / `files.write(path, content)` / `files.list(dir)`
- `files.delete(path)` — reversible: moves to `.friday-trash/` TTL 30d
- `files.organize(dir, rules)` — applies rules; auto-escalates at >20 files

Rules: never delete irreversibly. Every mutation logs a tool_result event with an undo handle. [↩ Undo 89s] chip is rendered for the last mutation.
