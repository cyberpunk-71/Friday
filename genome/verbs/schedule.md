# verb: schedule

- `schedule.remind({text, at, channels:["chrome","chat","phone","voice"]})`
- `schedule.event({title, date, notes})` — plans table (conflict-checked against existing plans)
- `schedule.tracker({kind:"availability|price|content", query, frequency_mins})`

Rules: reminders are dual-channel by default (chrome+chat). Conflict checks run at write time and are surfaced as cards, not buried.
