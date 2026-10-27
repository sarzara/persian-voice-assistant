# Persian Voice Assistant

A personal Telegram assistant. Send a voice message in Persian ("فردا ساعت ده با علی جلسه بگذار",
"به سارا ایمیل بزن که گزارش را فردا می‌فرستم"); it transcribes it, turns it into **one** calendar
event or email, shows you a Persian preview, and acts **only after you tap ✅ تأیید**.
The full spec is in [docs/SPEC.md](docs/SPEC.md).

**Status:** batch 01 (foundation) and batch 02 (speech-to-text, agent, tools, confirmation, memory).

## How a message flows

```
voice ──ffmpeg──► 16 kHz WAV ──Vosk (fa)──► transcript + confidence ──► audio deleted
                                                  │ empty / low confidence → "please repeat"
text ─────────────────────────────────────────────┤
                                                  ▼
        context: now (Tehran, Jalali) · preferences · mentioned contacts · last 5 messages
                                                  ▼
  DeepSeek agent (≤ 6 steps, read-only tools) ──► ScheduleAction | EmailAction | question
                                                  ▼   Pydantic + code guardrails
                          Persian preview  [✅ تأیید] [✏️ ویرایش] [❌ لغو]
                                                  ▼   Confirm only
           executor (idempotent) ──► Google Calendar / Resend ──► memory facts via tools
```

## What runs

| Service   | What it does                                                      | Exposed            |
|-----------|-------------------------------------------------------------------|--------------------|
| `db`      | PostgreSQL 16, data in the `pgdata` named volume                  | not published      |
| `migrate` | One-shot `alembic upgrade head`, then exits                       | —                  |
| `bot`     | Telegram worker (long polling): STT, agent, confirmation          | outbound only      |
| `api`     | FastAPI; `GET /health` checks the database                        | `127.0.0.1:8000`   |

`bot` and `api` start only after `migrate` succeeds, and `migrate` only after `db` is healthy.

## Quick start

Requirements: Docker Desktop (or Docker Engine + Compose v2). The first build downloads the
Vosk Persian model (`vosk-model-small-fa-0.42`, 53 MB) from alphacephei.com.

```bash
cp .env.example .env        # then fill it in (sections below)
docker compose up --build
docker compose run --rm api python -m app.db.seed       # owner + two sample contacts
```

Minimum `.env` for batch 02: `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`, `OWNER_*`, `POSTGRES_PASSWORD`,
`RESEND_API_KEY` + `EMAIL_FROM`, `DEEPSEEK_API_KEY`, and the four `GOOGLE_*` values.

### DeepSeek

Create a key at platform.deepseek.com and set `DEEPSEEK_API_KEY`. `DEEPSEEK_BASE_URL`
(`https://api.deepseek.com`) and `DEEPSEEK_MODEL` (`deepseek-chat`) can point at any
OpenAI-compatible endpoint; the agent only depends on the `LLMProvider` interface.

### Google Calendar setup (one time)

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project and **enable the
   Google Calendar API** (APIs & Services → Library).
2. **OAuth consent screen**: User type *External*; add your Google account under *Test users*.
3. **Credentials → Create credentials → OAuth client ID → Application type: Desktop app.**
   Put the client ID and secret in `.env` as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.
4. On your computer (not in Docker), with Python 3.9+ and no extra packages:
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

### Resend

`EMAIL_FROM` must be on a domain verified in Resend (until then, `onboarding@resend.dev` can only
send to your own Resend account address). Replies to assistant emails go to `OWNER_EMAIL` (reply-to).

## Using it

- Voice or text: "پنجشنبه ساعت ۳ بعدازظهر یک جلسه یک ساعته با سارا درباره بودجه بگذار".
- If something is missing (time, an unknown person's email), the bot asks one Persian question;
  just answer, and it combines your answer with the previous message.
- The preview shows date (Jalali + Gregorian), time, duration, guests, or the full email.
  **✅ تأیید** runs it, **✏️ ویرایش** lets you say what to change, **❌ لغو** drops it.
  A preview expires after `CONFIRMATION_TTL_MIN` (60) minutes.
- Speech recognition rarely gets email addresses right. For a new person, **type** the address
  ("ایمیل رضا reza@example.org است"); it is saved as a contact after the action runs.

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

## Everyday commands

```bash
docker compose run --rm api python -m app.db.seed               # idempotent
docker compose run --rm api python -m app.cli send-test-email   # proves RESEND_API_KEY
docker compose run --rm api python -m app.cli eval-agent        # 10 Persian samples vs. the real LLM
curl http://127.0.0.1:8000/health
docker compose logs -f bot
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Useful queries:

```sql
select id, status, intent, steps, prompt_tokens, completion_tokens, latency_ms, error
  from agent_runs order by id desc limit 10;
select run_id, tool_name, status, latency_ms from tool_calls order by id desc limit 20;
```

`eval-agent` runs as a separate user (`telegram_user_id = 0`) with its own two contacts and a fixed
clock (Tuesday 2026-10-27 09:00 Tehran), so it never touches your data and gives the same expected
dates on any day. It exits non-zero if any sample fails.

## Acceptance check (batch 02)

1. `docker compose up --build`; check the bot log shows no errors.
2. Voice: "فردا ساعت ده صبح با علی رضایی جلسه بگذار" → transcript → preview → ✅ → event in Google
   Calendar at 10:00 Tehran time, Ali invited (`ali.rezaei@example.com` is a fake seed address;
   edit the contact to a real one to see the invite arrive).
3. Voice: "به سارا ایمیل بزن و بگو گزارش را تا فردا می‌فرستم" → preview → ✅ → email delivered
   (point Sara's contact at an address you can read).
4. Tap ✅ twice quickly → the second tap answers "قبلاً انجام شده".
5. `docker compose run --rm api python -m app.cli eval-agent` → 10/10.

## Tests

```bash
python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt
python -m pytest                                    # unit tests, no services needed
RUN_DB_TESTS=1 python -m pytest                     # + PostgreSQL tests (TRUNCATE all tables!)
RUN_DB_TESTS=1 RUN_LIVE_TESTS=1 python -m pytest tests/test_agent_live.py -v   # real DeepSeek
```

Offline agent tests replay the 10 samples (`app/agent/evaluation.py`) through the real loop, tools,
validation and database with a scripted model; they test the plumbing and the guarantees (injection,
invented addresses, past dates, step cap, double tap), not the model's judgement. The live tests and
`eval-agent` measure the model.

## Layout

```
app/
  config.py            settings from environment (.env)
  cli.py               send-test-email, eval-agent
  bot/                 handlers, Persian messages, preview + buttons
  stt/                 ffmpeg conversion, Vosk recognizer, STT service
  agent/               provider, prompts/, actions, agent loop, executor, memory, pipeline, evaluation
  tools/               registry (schemas + logging), Google Calendar, Resend, Persian/Jalali helpers
  db/                  models, session, repo helpers, seed
  api/                 FastAPI app
scripts/google_auth.py one-time OAuth consent (stdlib only)
migrations/            0001 schema · 0002 tool-call latency · 0003 agent metadata, text messages
tests/
```

## Data model notes

Typed messages are stored in `voice_messages` with `source='text'` (no `telegram_file_id`), so both
inputs share one pipeline and one history. `agent_runs.status` moves
`running → needs_clarification | pending_confirmation → confirmed → executed | failed`, or `rejected`
(cancel, edit, expired). Deliberate deviations from the spec's column list: `voice_messages.audio_path`,
`agent_runs.prompt_tokens/completion_tokens` (instead of `tokens`), `emails.to_address` (`TO` is reserved).

## Known limitations

- `vosk-model-small-fa-0.42` reports 14–23 % word error rate; names and numbers are the usual
  failures. Low-confidence results ask you to repeat (`STT_MIN_CONFIDENCE`, default 0.6, is a starting
  point: tune it on your own voice messages).
- One owner action per message; no recurring events, no multi-recipient email (by design).
- A crash between "confirmed" and the provider call leaves the run `confirmed` with a pending
  tool call; it is never retried automatically (safe, but you need to resend the request).

## Security notes

- Secrets live only in `.env` (git-ignored, excluded from the Docker build context), held as `SecretStr`.
- `httpx` request logging is silenced because Telegram URLs contain the bot token.
- Containers run as UID 10001; the database has no published port; the API listens on loopback only.
- The allowlist check runs before every handler, including button callbacks; runs are also checked
  against the tapping user's ID.
- Bot replies never use `parse_mode`, so transcripts and model output cannot inject formatting or links.
