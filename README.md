# Persian Voice Assistant

A personal Telegram assistant: send a voice message in Persian and it will
(in later batches) transcribe it, turn it into a calendar event or an email,
and run it only after you confirm. The full spec is in [docs/SPEC.md](docs/SPEC.md).

**Status: batch 01 — foundation.** Telegram bot, PostgreSQL schema, Resend email
client and Docker setup. Speech-to-text and the agent come in batch 02.

## What runs

| Service   | What it does                                                      | Exposed            |
|-----------|-------------------------------------------------------------------|--------------------|
| `db`      | PostgreSQL 16, data in the `pgdata` named volume                  | not published      |
| `migrate` | One-shot `alembic upgrade head`, then exits                       | —                  |
| `bot`     | Telegram worker (long polling); voice files in the `voice` volume | outbound only      |
| `api`     | FastAPI; `GET /health` checks the database                        | `127.0.0.1:8000`   |

`bot` and `api` start only after `migrate` succeeds, and `migrate` only after `db`
is healthy, so the schema is always current and Alembic never runs twice at once.

## Quick start

Requirements: Docker Desktop (or Docker Engine + Compose v2).

```bash
cp .env.example .env        # then edit .env (see below)
docker compose up --build   # starts db -> migrate -> bot + api
```

Fill in `.env`:

- `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather).
- `ALLOWED_USER_IDS` — your numeric Telegram ID (ask [@userinfobot](https://t.me/userinfobot)); comma-separate several.
- `OWNER_TELEGRAM_ID`, `OWNER_NAME`, `OWNER_EMAIL` — you.
- `RESEND_API_KEY`, `EMAIL_FROM` — from [resend.com](https://resend.com). Until you verify a domain,
  `onboarding@resend.dev` can only send to the email address of your own Resend account,
  so set `OWNER_EMAIL` to that address for the test.
- `POSTGRES_PASSWORD` — any long random string.

## Everyday commands

```bash
# Seed the owner and two sample contacts (idempotent)
docker compose run --rm api python -m app.db.seed

# Prove the Resend key works: sends one test email to OWNER_EMAIL
docker compose run --rm api python -m app.cli send-test-email
docker compose run --rm api python -m app.cli send-test-email --to someone@example.com

# Health check
curl http://127.0.0.1:8000/health      # {"status":"ok","database":"ok"}

# Logs, a database shell, stop
docker compose logs -f bot
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose down                    # keeps data; add -v to wipe volumes
```

## Acceptance check (batch 01)

1. `docker compose up --build` → `migrate` logs `Running upgrade -> 0001`, then `bot` logs `Starting bot`.
2. Send `/start` to the bot → Persian greeting. `/help` → Persian help.
3. From an account **not** in `ALLOWED_USER_IDS` → a short polite Persian refusal.
4. Send a voice message → "✅ پیام صوتی شما … ذخیره شد", and
   `docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select id, telegram_file_id, duration_s, audio_path from voice_messages"'` shows the row.
5. `docker compose run --rm api python -m app.cli send-test-email` prints `OK … id=…` and the email arrives.

## Development without Docker

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
# in .env set POSTGRES_HOST=localhost and publish/point to a Postgres you can reach
alembic upgrade head
python -m app.bot.main
python -m pytest                     # unit tests
RUN_DB_TESTS=1 python -m pytest      # + database tests (they TRUNCATE all tables: use a throwaway DB)
```

New migration after changing `app/db/models.py`:

```bash
alembic revision --autogenerate -m "describe the change"   # then review the file
```

`tests/test_db.py::test_models_match_migration` fails if a model changes without a migration.

## Layout

```
app/
  config.py          settings from environment (.env)
  cli.py             operator commands (send-test-email)
  bot/               Telegram handlers, Persian messages, entry point
  db/                models, session, repo helpers, seed
  tools/             external integrations (Resend client)
  api/               FastAPI app
migrations/          Alembic environment and revisions
tests/
docs/SPEC.md
```

## Data model

`users`, `contacts` (Persian + English `aliases`, GIN-indexed), `voice_messages`,
`agent_runs`, `tool_calls` (unique `idempotency_key`), `calendar_events`, `emails`,
`memory_facts` (unique per user + key), `audit_events`. Every table has `created_at`
and `updated_at` (kept current by a database trigger). Status and intent columns are
`VARCHAR` with `CHECK` constraints, so adding a state later is a small migration.

Deviations from the spec's column list, on purpose:

- `voice_messages.audio_path` — where the audio sits until STT deletes it (batch 02).
- `agent_runs.prompt_tokens` / `completion_tokens` instead of one `tokens` column, so the
  dashboard can price input and output separately.
- `emails.to_address` instead of `to` (`TO` is reserved in SQL).

## Security notes

- Secrets live only in `.env`, which is git-ignored and excluded from the Docker build context.
- Settings hold secrets as `SecretStr`; `httpx` request logging is silenced because Telegram
  URLs contain the bot token.
- Containers run as a non-root user (UID 10001). The database has no published port; the API
  listens on loopback only.
- The allowlist check runs before every other handler; strangers in groups get no reply at all.
