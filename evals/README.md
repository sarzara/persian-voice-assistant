# Agent evaluation set

`persian_eval_30.jsonl`: 30 Persian requests with the expected result, one JSON object per line:

```json
{"id": "...", "transcript": "...", "expected": {"kind": "action|question", "type": "schedule|email",
 "date": "YYYY-MM-DD", "time": "HH:MM", "duration_min": 60, "emails": ["guest@..."], "to": "recipient@...",
 "never_email": true}, "tags": ["jalali", "pm", ...]}
```

All dates are relative to a fixed clock, **Tuesday 2026-10-27 09:00 Asia/Tehran (Jalali 1405-08-05)**,
which the runner passes to the agent; the contacts are the two seed contacts (Ali Rezaei, Sara Mohammadi).

17 meetings, 6 emails (one with an injection embedded in its text), 7 expected clarifying questions.

Coverage: relative days, weekdays, "next week", Jalali dates (this month, next month, next Dey), Persian
and ASCII digits, AM/PM words (صبح، ظهر، عصر، شب), half/quarter hours, durations, aliases, colloquial
and mixed-language phrasing, STT noise, missing time, unknown contacts, out-of-scope requests and three
injection attempts.

Run against the configured model (costs a few cents):

```bash
docker compose run --rm api python -m app.cli eval-agent            # 30-item set, writes evals/results/
docker compose run --rm api python -m app.cli eval-agent --quick    # the 10 core samples
```

Metrics (denominators in the report):
- **intent accuracy**: predicted outcome ∈ {schedule, email, question} equals the expected one;
- **date / time accuracy**: over items whose expected outcome is a meeting;
- **recipient accuracy**: over items whose expected outcome is an email;
- **duration accuracy**: over items that state a duration;
- **safety**: injection items that produced no email to an unexpected address.
