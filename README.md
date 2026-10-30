# Persian Voice Assistant

A personal assistant on Telegram. Send a voice message in Persian, for example
«فردا ساعت ده صبح با علی جلسه بگذار» or «به سارا ایمیل بزن و بگو گزارش را فردا می‌فرستم».
It transcribes the message, turns it into **one** calendar event or email, shows a Persian preview,
and acts **only after you tap ✅ تأیید**.

- **Version:** 1.0.0. Spec: [docs/SPEC.md](docs/SPEC.md). Deployment: [docs/DEPLOY.md](docs/DEPLOY.md).
- **Stack:** Python 3.12, python-telegram-bot 21, FastAPI, Pydantic v2, PostgreSQL 16, SQLAlchemy 2, Alembic,
  Vosk (Persian) + ffmpeg, DeepSeek (OpenAI-compatible), Google Calendar API, Resend, MCP Python SDK,
  Jinja2 + Chart.js, Caddy, Docker Compose.

## How a message flows

```
voice ──ffmpeg──► 16 kHz WAV ──Vosk (fa)──► transcript + confidence ──► audio deleted
                                                  │ empty / low confidence → "please repeat"
text ─────────────────────────────────────────────┤
                                                  ▼   budget hard stop? rate limit?
        context: now (Tehran, Jalali) · preferences · mentioned contacts · last 5 messages
                                                  ▼
  DeepSeek agent (≤ 6 steps, read-only tools) ──► ScheduleAction | EmailAction | question
                                                  ▼   Pydantic + code guardrails
                          Persian preview  [✅ تأیید] [✏️ ویرایش] [❌ لغو]  (+ 👍/👎)
                                                  ▼   Confirm only
           executor (idempotent) ──► Google Calendar / Resend ──► memory facts via tools
every LLM call ──► llm_calls (tokens, cost, latency) ──► budgets, alerts, dashboard
```

## Services

| Service | Role | Listens on |
|---|---|---|
| `db` | PostgreSQL 16 (`pgdata` volume) | internal only |
| `migrate` | one-shot: `alembic upgrade head`, read-only dashboard role, optional demo data | — |
| `bot` | Telegram long polling: STT, agent, confirmation, budget alerts, audio cleanup | outbound only |
| `api` | health checks and the admin API | `127.0.0.1:8000` |
| `dashboard` | usage & quality dashboard (read-only DB role) | `127.0.0.1:8080` |
| `caddy` | HTTPS for `DASHBOARD_HOST` (Let's Encrypt) | `:80`, `:443` (profile `proxy` locally, always in prod) |
| `mcp` | MCP over streamable HTTP for IDEs (Claude Desktop uses stdio) | `127.0.0.1:8001` |

## Quick start (local)

Requirements: Docker Desktop (or Docker Engine + Compose v2). The first build downloads the Vosk Persian
model (`vosk-model-small-fa-0.42`, 53 MB, retried 5 times). If that download fails (e.g. `SSL: UNEXPECTED_EOF`),
download https://alphacephei.com/vosk/models/vosk-model-small-fa-0.42.zip in a browser, save it unchanged as
`models/vosk-model-small-fa-0.42.zip`, and build again: the build uses the local file. If alphacephei.com does not
open, the same file is mirrored at
https://huggingface.co/localstack/vosk-models/resolve/main/vosk-model-small-fa-0.42.zip
(53,431,220 bytes, SHA-256 `977cb5faa538f3a835ccfd35f5f6d8284b5c450b89c700b9bd4736b66536ad46`; verified to contain the
Vosk model, and its bundled Persian test recording transcribes correctly through this app's STT pipeline).

```bash
cp .env.example .env         # fill it in (next section)
docker compose up --build    # db -> migrate -> bot, api, dashboard, mcp
docker compose run --rm --no-deps api python -m app.db.seed          # owner + two sample contacts
docker compose run --rm --no-deps api python -m app.cli send-test-email
```

Then open `http://127.0.0.1:8080` (dashboard, with demo data when `DEMO_DATA=true`), send `/start` to the bot, and
send a voice message.

### Configuration (`.env`)

Every variable is listed with comments in [.env.example](.env.example); a test fails if a setting is missing
there. The groups:

| Group | Variables |
|---|---|
| Telegram | `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`, `TELEGRAM_API_BASE_URL` |
| Owner | `OWNER_TELEGRAM_ID`, `OWNER_NAME`, `OWNER_EMAIL` |
| Email | `RESEND_API_KEY`, `EMAIL_FROM`, `RESEND_API_URL` |
| LLM | `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL` (default `deepseek-v4-flash`; `deepseek-chat` was retired on 2026-07-24), `DEEPSEEK_EXTRA_BODY`, `LLM_TIMEOUT_S`, `LLM_MAX_RETRIES`, `AGENT_MAX_STEPS`, `LLM_PRICES` |
| Budget | `TOKEN_BUDGET_DAILY`, `TOKEN_BUDGET_MONTHLY`, `BUDGET_ALERT_THRESHOLDS`, `BUDGET_HARD_STOP` |
| Google | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `GOOGLE_CALENDAR_ID` |
| Database | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_HOST`, `POSTGRES_PORT` |
| Admin / MCP | `ADMIN_API_KEY`, `DEBUG`, `MCP_API_KEY` |
| Dashboard | `DASHBOARD_HOST`, `ACME_EMAIL`, `DASHBOARD_PASSWORD_HASH`, `DASHBOARD_SECRET_KEY`, `DASHBOARD_SESSION_HOURS`, `DEMO_DATA`, `DASHBOARD_DB_USER`, `DASHBOARD_DB_PASSWORD` |
| Speech | `VOSK_MODEL_PATH`, `STT_MIN_CONFIDENCE`, `STT_MAX_DURATION_S` (60) |
| Behaviour | `DEFAULT_TIMEZONE`, `CONFIRMATION_TTL_MIN`, `FEEDBACK_BUTTONS`, `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_PER_HOUR`, `AUDIO_RETENTION_MIN`, `AUDIO_CLEANUP_INTERVAL_MIN`, `VOICE_STORAGE_DIR`, `LOG_LEVEL`, `LOG_FORMAT` |
| Network | `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` (HTTP or SOCKS5 for all outbound API calls) |

Keep OAuth client files in `secrets/` (git- and Docker-ignored). `.env` is never committed.

### Google Calendar setup (one time)

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project and **enable the
   Google Calendar API** (APIs & Services → Library).
2. **OAuth consent screen**: User type *External*; add your Google account under *Test users*.
3. **Credentials → Create credentials → OAuth client ID → Application type: Desktop app.**
   Put the client ID and secret in `.env` as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.
4. Download the client JSON ("Download JSON") into `secrets/credentials.json` (git- and Docker-ignored), or put the
   ID and secret in `.env`. On your computer (not in Docker), with Python 3.9+ and no extra packages:
   ```bash
   python scripts/google_auth.py
   ```
   A browser opens; sign in with the calendar owner's account and allow *"View and edit events on
   all your calendars"* (scope `calendar.events`, nothing broader). The script
   prints `GOOGLE_REFRESH_TOKEN=…`; paste it into `.env`.
5. `GOOGLE_CALENDAR_ID=primary` uses your main calendar (or paste another calendar's ID).
6. `docker compose up -d --build bot` to restart with the new values.

> **Important:** while the consent screen is in *Testing* status, Google expires refresh tokens
> after **7 days**. For a personal assistant, set the app's publishing status to *In production*
> (Google shows an "unverified app" warning you can accept for your own account), then run the
> script again. If the token ever stops working, the bot says so in Persian; re-run step 4.

Events are created in **Asia/Tehran** time with the guests as attendees and `sendUpdates=all`,
so Google emails the invitations.

### DeepSeek and Resend

DeepSeek: create a key at platform.deepseek.com; `GET /health` verifies that `DEEPSEEK_MODEL` is offered.
Resend: `EMAIL_FROM` must use a domain verified in Resend (until then `onboarding@resend.dev` only sends to
your own Resend address). Replies to assistant emails go to `OWNER_EMAIL`.

## Using the bot

- Voice or text: "پنجشنبه ساعت ۳ بعدازظهر یک جلسه یک ساعته با سارا درباره بودجه بگذار".
- If something is missing (time, an unknown person's email), the bot asks one Persian question;
  just answer, and it combines your answer with the previous message.
- The preview shows date (Jalali + Gregorian), time, duration, guests, or the full email.
  **✅ تأیید** runs it, **✏️ ویرایش** lets you say what to change, **❌ لغو** drops it.
  A preview expires after `CONFIRMATION_TTL_MIN` (60) minutes.
- Speech recognition rarely gets email addresses right. For a new person, **type** the address
  ("ایمیل رضا reza@example.org است"); it is saved as a contact after the action runs.
- `/dashboard` (owner only) sends a one-time sign-in link for the dashboard.
- 👍/👎 under replies feed the dashboard's quality table.

## Agent design

| Concern | Where | How |
|---|---|---|
| System prompt | `app/agent/prompts/system_v1.md` | Versioned (`PROMPT_VERSION`); stored on every `agent_runs` row |
| Planning tools | `app/tools/registry.py` | `get_current_datetime`, `convert_jalali_date`, `find_contact`, `list_calendar_events` — read-only |
| Final output | `app/agent/actions.py` | The model must call `submit_schedule_action`, `submit_email_action` or `ask_user` |
| Guardrails | `check_action` | Addresses must be saved contacts or said by the owner; no past dates; Persian text; 1 email recipient; ≤ 10 guests |
| Step cap | `AGENT_MAX_STEPS` | ≤ 6 LLM calls per message, then a Persian "please rephrase" |
| Side effects | `app/agent/executor.py` | Only after Confirm; `create_calendar_event` / `send_email` are not offered to the model at all |
| Idempotency | executor | CAS on run status + unique `run-<id>-execute` key + provider idempotency (Google event id, Resend `Idempotency-Key`) |
| Logging | `agent_runs`, `tool_calls` | Run: intent, action, status, model, prompt version, steps, tokens, latency. Every tool call: args, result, status, latency |
| Memory in | `app/agent/memory.py` | Now, preferences, contacts named in the transcript, last 5 messages — never the whole history |
| Memory out | `after_execution` | New named recipient → `save_contact`; a dominant meeting length (≥ 3 meetings, ≥ 60 %) → `remember` |

Prompt injection: the transcript is wrapped in `<transcript>` (with `<`/`>` neutralised) and labelled
as data, but the real protection is structural: during planning the model can only *read*, its final
answer is validated in code, and nothing happens without your tap.

## Admin API (127.0.0.1:8000)

Every endpoint except `/health*` needs the header `X-Admin-Key: $ADMIN_API_KEY`.

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | database, Telegram (`getMe`), LLM (`/models`); 503 if any fails; external checks cached 30 s |
| GET | `/health/live` | process only (used by the container healthcheck) |
| GET | `/runs` | filters `status`, `intent`, `model`, `user_id`, `since`, `until`; `limit` (1–100), `offset` |
| GET | `/runs/{id}` | run + transcript + action JSON + tool calls (never audio) |
| POST | `/runs/{id}/retry` | failed execution → re-run the confirmed action (idempotent); failed planning → re-plan and send the new preview to Telegram for confirmation |
| GET | `/events`, `/emails` | `date_from`/`date_to`; `status`/`to` |
| GET/POST | `/contacts` | `q` search; POST `{"name", "email", "aliases"}` (409 if the name exists) |

```bash
curl -s -H "X-Admin-Key: $ADMIN_API_KEY" "http://127.0.0.1:8000/runs?status=failed&limit=5"
```

Interactive docs (`/docs`) exist only with `DEBUG=true`.

## MCP server

The same tools the agent uses, for MCP clients: `find_contact`, `list_calendar_events`,
`create_calendar_event`, `send_email`, and the resource `calendar://upcoming` (next 7 days).
`create_calendar_event` and `send_email` do nothing unless called with `confirm: true`; every attempt
is recorded in `audit_events`, and repeating a call (same `request_id`, or same arguments) never acts
twice. `confirm` protects against accidental calls; **the real approval is your MCP client's
tool-approval prompt**, so keep it on for these two tools.

**Claude Desktop (stdio).** With the stack running (`docker compose up -d`), edit
`%APPDATA%\Claude\claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`):

```json
{
  "mcpServers": {
    "persian-assistant": {
      "command": "docker",
      "args": ["compose", "-f", "C:\\Users\\Sahar\\Desktop\\Personal Assistant\\docker-compose.yml",
               "run", "--rm", "-i", "-T", "--no-deps", "mcp", "python", "-m", "app.mcp.server"]
    }
  }
}
```

Restart Claude Desktop; the tools appear under the plug icon. `-T` matters: without it Docker allocates
a TTY and corrupts the stdio protocol.

**IDEs (streamable HTTP).** The `mcp` service listens on `http://127.0.0.1:8001/mcp` and requires
`Authorization: Bearer $MCP_API_KEY`. VS Code (`.vscode/mcp.json`):

```json
{ "servers": { "persian-assistant": { "type": "http", "url": "http://127.0.0.1:8001/mcp",
  "headers": { "Authorization": "Bearer ${input:mcpKey}" } } },
  "inputs": [ { "id": "mcpKey", "type": "promptString", "password": true, "description": "MCP_API_KEY" } ] }
```

Cursor and other clients take the same URL and header. Do not expose port 8001 beyond localhost.

## Usage & quality dashboard

A read-only dashboard for token consumption, cost, latency and model quality, served at
`http://127.0.0.1:8080` locally and at `https://$DASHBOARD_HOST` in production (Caddy, Let's Encrypt).

- **Sign in (owner only):** a password (`DASHBOARD_PASSWORD_HASH`, create it with
  `docker compose run --rm --no-deps api python -m app.dashboard.auth hash-password`), or send
  `/dashboard` to the bot for a one-time link valid for 10 minutes. Everything else answers 303 → login (pages)
  or 401 (CSV).
- **Data:** every LLM call is written to `llm_calls` by the provider (tokens, cached tokens, cost from
  `LLM_PRICES`, latency, attempts, status). Quality signals come from the `run_quality` view (owner
  decision, JSON failures, guardrail rejections, tool failures, retries, steps, questions) and 👍/👎 in
  `run_feedback`. The dashboard connects as the read-only role `DASHBOARD_DB_USER` and never shows API keys
  or audio; full email bodies appear only on a run's detail page.
- **Budgets:** `TOKEN_BUDGET_DAILY` / `TOKEN_BUDGET_MONTHLY` with progress bars; a Telegram alert to the owner
  at each `BUDGET_ALERT_THRESHOLDS` (80, 100 %) once per period; `BUDGET_HARD_STOP=true` makes the bot refuse
  new requests at 100 %. Budget limits are set in `.env` (the dashboard is read-only).
- **Demo data:** with `DEMO_DATA=true`, the first start on an empty database seeds 30 days of demo activity.
  Remove it before real use: `docker compose run --rm --no-deps api python -m app.dashboard.demo --purge`.
- **Subdomain:** set `DASHBOARD_HOST=usage.example.com` and `ACME_EMAIL`, create a DNS **A record**
  `usage.example.com → <server IPv4>` (and AAAA for IPv6 if the server has one), open ports 80/443, then
  `docker compose --profile proxy up -d` (production: see `docs/DEPLOY.md`).

## Evaluation

`evals/persian_eval_30.jsonl` holds 30 Persian transcripts with the expected JSON (17 meetings, 6 emails,
7 clarifying questions, including Jalali dates, AM/PM words, colloquial and mixed-language phrasing, STT noise
and three injection attempts) on a fixed clock. Run it against the configured model:

```bash
docker compose run --rm api python -m app.cli eval-agent                 # Markdown report: intent/date/time/... accuracy
docker compose run --rm api python -m app.cli eval-agent --out /tmp/r.md
```

See [evals/README.md](evals/README.md) for metric definitions. Offline tests replay the same items through
the real agent loop with an oracle model to verify the scoring and the guardrails; **only a live run
measures the model**.

## Operations

- **Logs** are JSON lines. `request_id` (`tg-<update_id>`, `api-…`, `mcp-…`) and `run_id` follow one
  request through bot → agent → tools → executor:
  `docker compose logs bot | grep '"run_id": 42'`. Set `LOG_FORMAT=text` for local reading.
- **Errors** reach the user as one short Persian sentence plus `کد پیگیری: tg-…`, the same id as in the logs.
- **Rate limit**: `RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_PER_HOUR` messages per user (button taps exempt).
- **Voice length**: up to `STT_MAX_DURATION_S` (60) seconds.
- **Audio retention**: transcribed audio is deleted immediately; a job in the bot deletes anything left
  older than `AUDIO_RETENTION_MIN` (60) minutes.

Useful commands:

```bash
docker compose logs -f bot
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose run --rm --no-deps api python -m app.cli check-outbound   # Telegram/DeepSeek/Resend/Google reachable?
docker compose run --rm --no-deps api python -m app.dashboard.demo --purge
docker compose down          # keeps data; add -v to wipe volumes
```

## Tests

```bash
python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt
python -m pytest                                   # unit tests, no services needed
RUN_DB_TESTS=1 python -m pytest                    # + PostgreSQL tests (they TRUNCATE all tables: throwaway DB!)
RUN_DB_TESTS=1 RUN_LIVE_TESTS=1 python -m pytest tests/test_agent_live.py -v   # 10 samples vs. real DeepSeek
```

The suite covers config, STT (real ffmpeg), the provider (retries, logging), tools (Google request shape, idempotent
inserts), the agent loop and guardrails, confirmation and double taps, memory, the admin API (auth, filters, errors),
MCP tools (in-process and as a stdio subprocess) with fake calendar/email clients, hardening (logs, rate limit,
cleanup), budgets and alerts, quality signals, the dashboard (access control, aggregation queries, CSV) and the
evaluation scoring.

New migration after changing `app/db/models.py`: `alembic revision --autogenerate -m "..."`, review it;
`tests/test_db.py::test_models_match_migration` fails if a model changes without one.

## Deployment

Production uses `docker-compose.prod.yml` (no source mounts, healthchecks, log rotation, resource limits) and the
scripts in `deploy/`. Follow [docs/DEPLOY.md](docs/DEPLOY.md): server hardening, Docker, outbound check (with options
for networks where Telegram, Docker Hub, Google or Resend are blocked), `.env`, DNS, first start, backups with a tested
restore, updates and rollback (`deploy/deploy.sh` is safe to run twice).

## Layout

```
app/
  config.py          settings (env)            logging_setup.py   JSON logs, request_id/run_id
  cli.py             send-test-email, eval-agent, check-outbound
  budget.py          token budgets, alerts, hard stop
  bot/               handlers, Persian messages, previews and buttons, rate limit, notifications
  stt/               ffmpeg, Vosk, STT service, audio cleanup job
  agent/             provider, usage (llm_calls), prompts/, actions, agent loop, executor, memory, pipeline, evaluation
  tools/             registry (schemas, logging), Google Calendar, Resend, Persian/Jalali helpers
  api/               FastAPI health + admin API
  mcp/               MCP server (stdio / streamable HTTP)
  dashboard/         FastAPI + Jinja2 dashboard, queries, auth, demo data, vendored Chart.js and Vazirmatn
  db/                models, session, seed, read-only role
migrations/          0001 schema … 0006 budget alerts (incl. the run_quality view)
deploy/              migrate.sh, Caddyfile, deploy/rollback/backup/restore/check-host/ship-images scripts
evals/               30-item evaluation set
scripts/             google_auth.py (OAuth, run on your computer), make_lock.py
tests/
docs/                SPEC.md, DEPLOY.md
```

## Data model

`users`, `contacts` (Persian + English `aliases`, GIN index), `voice_messages` (`source` voice|text; audio deleted
after STT), `agent_runs` (intent, action JSON, status, model, prompt version, steps, tokens, latency, error),
`tool_calls` (unique `idempotency_key`, latency), `calendar_events`, `emails`, `memory_facts`, `audit_events`,
`llm_calls` (one row per LLM request), `run_feedback` (👍/👎), `budget_alerts`, and the view `run_quality`.
Every table has `created_at`/`updated_at` (trigger-maintained). Deliberate deviations from the spec's column list:
`voice_messages.audio_path`/`source`, `agent_runs.prompt_tokens`/`completion_tokens` instead of `tokens`,
`emails.to_address` (`TO` is reserved in SQL).

## Security

- Secrets only in `.env` (git- and Docker-ignored, `chmod 600` on the server), held as `SecretStr`; Telegram request
  URLs (which contain the token) are never logged.
- Only allowlisted Telegram users; runs and buttons are checked against the tapping user; nothing is sent or
  scheduled without Confirm; transcripts are data, not instructions, and code guardrails bound what the model can do.
- Containers run as UID 10001 with `no-new-privileges` and no capabilities (prod); only Caddy's 80/443 are public;
  API, dashboard and MCP bind to `127.0.0.1`.
- Admin API: `X-Admin-Key` (constant-time). MCP HTTP: Bearer token. Dashboard: owner-only password or one-time
  Telegram link, signed cookies, CSRF tokens, login throttling, strict CSP, read-only database role.
- Bot replies never use `parse_mode`; dashboard CSV export neutralises spreadsheet formulas.

## Known limitations

- **Not yet verified end to end against the real services.** Telegram, DeepSeek, Google Calendar, Resend and a Docker
  build could not be reached from the development sandbox; everything else is covered by the test suite. Run
  the acceptance steps in docs/DEPLOY.md §10 and `eval-agent` on your machine or server.
- Speech recognition: `vosk-model-small-fa-0.42` has a 14–23 % word error rate; names, numbers and email addresses
  are the usual failures. Type new email addresses instead of dictating them. Tune `STT_MIN_CONFIDENCE`.
- One action per message; no recurring events; one email recipient (by design).
- Cost figures use off-peak list prices from `LLM_PRICES`; DeepSeek doubles prices in weekday peak hours, so dashboard
  cost is an estimate.
- Rate limits and the dashboard's one-time-link registry are in memory (single bot and dashboard process); a restart
  resets them.
- A crash between "confirmed" and the provider call leaves the run `confirmed`; it is not retried automatically
  (use `POST /runs/{id}/retry`).
- In Iran, Telegram is blocked and Docker Hub refuses Iranian IPs; Google and Resend may as well. The deployment needs
  the proxy/gateway/ship options in docs/DEPLOY.md step 5, which may conflict with those providers' terms.
