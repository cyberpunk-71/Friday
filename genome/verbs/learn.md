# verb: learn

- `learn.skill({name, procedure, verify, cassette})` — writes genome/skills/<name>/SKILL.md + verify() + VCR cassette; NO cassette, NO merge
- `learn.self_review()` — git log of the genome, rendered as cards
- `learn.score_explanation()` — "I remembered that because: entity-exact +1.4, correction-flag +2.1, recency −0.3"

Rules: a skill ships only if it beats the no-skill baseline on replayed real turns (Gym). Failed cassette = quarantine, not silent rot.
