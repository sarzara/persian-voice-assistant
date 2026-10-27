# Changelog

## 1.0.0 (2026-10-27)

- **01 Foundation:** Docker Compose (db, bot, api), Telegram bot with allowlist and Persian replies, PostgreSQL schema
  with Alembic, Resend client and test-email CLI.
- **02 Agent:** Vosk Persian STT with ffmpeg (audio deleted after transcription), DeepSeek behind a provider
  interface, tool registry with Pydantic schemas, Google Calendar (OAuth) and Resend tools, versioned prompt,
  bounded agent loop with code guardrails, Persian preview with Confirm/Edit/Cancel, idempotent execution, memory.
- **03 Backend:** health checks, admin API (runs, events, emails, contacts, retry), MCP server sharing the agent's
  tools, JSON logs with run_id correlation, rate limit, 60 s voice cap, Persian errors with reference codes, audio
  cleanup job.
- **04 Dashboard:** `llm_calls` logging from the provider with costs, quality signals (`run_quality`), 👍/👎,
  token budgets with Telegram alerts and hard stop, demo data, owner-only read-only dashboard on its own subdomain
  behind Caddy.
- **05 Release:** 30-item Persian evaluation set with accuracy report, network proxy/gateway settings and outbound
  check, hash-pinned lockfile, production compose, deployment guide and scripts.
- Default model changed to `deepseek-v4-flash` (`deepseek-chat` was retired by DeepSeek on 2026-07-24).
