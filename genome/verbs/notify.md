# verb: notify

- `notify.toast(msg)` — inline toast in chat UI (no chrome)
- `notify.chrome(title, body)` — chrome notification (per-kind channels configurable: focus/pay/reminder/research)
- `notify.voice(text)` — spoken nudge (TTS)

Rules: coalescing is mandatory — never more than 1 toast + 1 chrome per 10 minutes per kind. Snooze respected. Never use notifications for things the user can see in the stream.
