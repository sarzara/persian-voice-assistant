# Deploying to a Linux server (cloud.ir)

Step by step, every command included. You run everything; nothing here needs the author.
Commands marked **[laptop]** run on your computer, **[server]** on the server over SSH.

Placeholders: `SERVER_IP` (the server's public IPv4), `usage.example.com` (the dashboard subdomain),
`YOUR_GITHUB_USER/persian-voice-assistant` (your private repository).

```
Internet ──443/80──► Caddy ──► dashboard:8080          (HTTPS, Let's Encrypt)
                      ✗ api:8000, mcp:8001              (server-local only: 127.0.0.1)
bot ──long polling──► api.telegram.org                  (outbound only, no inbound port)
bot/api ──► DeepSeek, Google Calendar, Resend           (outbound HTTPS, optional proxy)
```

> **Read step 5 before anything else if the server is in Iran.** Telegram is blocked there, Docker Hub
> refuses Iranian IP addresses, and Google and Resend may refuse them too. Step 5 shows how to find out in one
> command and what to do about it.

---

## 0. What you need before you start

| Item | Where it comes from |
|---|---|
| Ubuntu 22.04 or 24.04 server, 2 vCPU, 2 GB RAM, 20 GB disk, public IPv4 | cloud.ir panel |
| Root (or sudo) SSH access to it | cloud.ir panel |
| A domain where you can add DNS records | your registrar / DNS host |
| A private GitHub repository with this code | step 1 |
| Every secret in `.env` (bot token, DeepSeek key, Resend key + verified domain, Google OAuth client + refresh token, …) | README "Quick start"; Google token from `scripts/google_auth.py` on your laptop |
| An off-server place for backups (S3-compatible bucket, or your laptop) | step 12 |
| If step 5 finds blocks: a proxy or VPS outside the restricted network | step 5 |

## 1. Put the code in a private GitHub repository  [laptop]

```bash
cd "C:\Users\Sahar\Desktop\Personal Assistant"        # PowerShell; or the repo folder in WSL/Git Bash
git status                                             # must not list .env, secrets/ or credentials*.json
gh repo create persian-voice-assistant --private --source . --push   # or create it on github.com, then:
# git remote add origin git@github.com:YOUR_GITHUB_USER/persian-voice-assistant.git
# git push -u origin main --tags
```

## 2. Create the deploy user, SSH keys only  [server]

```bash
ssh root@SERVER_IP
adduser --disabled-password --gecos "" deploy
usermod -aG sudo deploy
echo "deploy ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/deploy && chmod 440 /etc/sudoers.d/deploy
mkdir -p /home/deploy/.ssh && chmod 700 /home/deploy/.ssh
```

**[laptop]** create a key if you have none, and print the public key:

```bash
ssh-keygen -t ed25519 -C "deploy@pva"        # accept the default path; set a passphrase
type $env:USERPROFILE\.ssh\id_ed25519.pub     # PowerShell  (macOS/Linux: cat ~/.ssh/id_ed25519.pub)
```

**[server]** paste it:

```bash
nano /home/deploy/.ssh/authorized_keys        # paste the one line, save
chmod 600 /home/deploy/.ssh/authorized_keys && chown -R deploy:deploy /home/deploy/.ssh
```

**[laptop]** in a **new** terminal, check you can log in as deploy **before** closing the root session:

```bash
ssh deploy@SERVER_IP
```

Turn off password and root login **[server, as deploy]**:

```bash
sudo tee /etc/ssh/sshd_config.d/10-hardening.conf >/dev/null <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
EOF
sudo sshd -t && sudo systemctl reload ssh     # 'sshd -t' must print nothing
```

Open a second `ssh deploy@SERVER_IP` to confirm you still get in. `ssh root@SERVER_IP` must now be refused.

## 3. Firewall: SSH, 80 and 443 only  [server]

```bash
sudo apt-get update && sudo apt-get -y upgrade
sudo apt-get install -y ufw unattended-upgrades git curl
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 443/udp        # HTTP/3
sudo ufw --force enable
sudo ufw status verbose
```

The bot uses long polling, so it needs **no** inbound port. Note: ports that Docker publishes bypass ufw.
That is why the compose file publishes the API (8000), dashboard (8080) and MCP (8001) on `127.0.0.1` only.
Caddy's 80/443 are the only public ports.

## 4. Install Docker Engine and the Compose plugin  [server]

Official repository (Ubuntu):

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker deploy
exit                                  # log out and back in so the docker group applies
```

```bash
ssh deploy@SERVER_IP
docker version && docker compose version     # both must print a version
```

If `download.docker.com` is unreachable, use Ubuntu's own packages instead:
`sudo apt-get install -y docker.io docker-compose-v2 docker-buildx`, then `sudo usermod -aG docker deploy`.

## 5. Outbound check, and what to do if something is blocked  [server]

Get the code first. GitHub is normally reachable; for a private repository, add a read-only deploy key:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/github_deploy -C "pva-server"
cat ~/.ssh/github_deploy.pub     # GitHub → repo → Settings → Deploy keys → Add (read-only)
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF
git clone git@github.com:YOUR_GITHUB_USER/persian-voice-assistant.git ~/persian-voice-assistant
cd ~/persian-voice-assistant
deploy/check-host.sh
```

Read the `VERDICT` column. `reachable` means any HTTP answer, even 401/404. `BLOCKED` means a timeout, DNS
failure or reset. `REFUSED 403/451` usually means a geo or sanctions block on the provider's side.

| Blocked | Effect | Fix (pick one per row) |
|---|---|---|
| Telegram Bot API | the bot cannot receive or send anything | **A** proxy · **B** Bot API gateway |
| DeepSeek | no agent | **A** proxy, or `DEEPSEEK_BASE_URL` → an OpenAI-compatible gateway you control |
| Google OAuth / Calendar | events cannot be created | **A** proxy |
| Resend | emails cannot be sent | **A** proxy, or `RESEND_API_URL` → a gateway you control |
| Docker Hub, PyPI, Debian, alphacephei.com | the image cannot be built here | **C** build on your laptop and ship, or **D** mirrors |
| Let's Encrypt | no HTTPS certificate | ask the provider; Caddy retries automatically |

**A. One HTTPS or SOCKS5 proxy for all API calls.** Every outbound API call in the app (Telegram, DeepSeek,
Google, Resend) honours the standard variables in `.env`:

```bash
HTTPS_PROXY=socks5h://host.docker.internal:1080     # or http://user:pass@proxy.example.net:3128
HTTP_PROXY=socks5h://host.docker.internal:1080
NO_PROXY=localhost,127.0.0.1,db,api,dashboard,mcp   # keep internal traffic direct
```

`socks5h` also resolves DNS through the proxy, which helps when DNS answers are tampered with. A simple
way to get such a proxy is an SSH tunnel to a small VPS you own outside the restricted network:

```bash
# [server] a key for the tunnel; add the .pub to ~/.ssh/authorized_keys of a user on the outside VPS
ssh-keygen -t ed25519 -N "" -f ~/.ssh/tunnel -C pva-tunnel
sudo apt-get install -y autossh
sudo tee /etc/systemd/system/pva-tunnel.service >/dev/null <<'EOF'
[Unit]
Description=SOCKS5 tunnel for the assistant's outbound API calls
After=network-online.target
Wants=network-online.target

[Service]
User=deploy
Environment=AUTOSSH_GATETIME=0
# Listens on the Docker bridge address so containers reach it as host.docker.internal:1080
ExecStart=/usr/bin/autossh -M 0 -N -D 172.17.0.1:1080 -i /home/deploy/.ssh/tunnel \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
  -o StrictHostKeyChecking=accept-new tunnel@OUTSIDE_VPS_IP
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now pva-tunnel
sudo ufw allow from 172.16.0.0/12 to any port 1080 proto tcp    # containers → tunnel (bridge networks only)
```

**B. Telegram Bot API gateway.** Instead of (or in addition to) a proxy, point the bot at a reverse proxy
for `api.telegram.org` that you control, e.g. a small HTTPS server outside the restricted network that
forwards `/bot*` and `/file/bot*` to `https://api.telegram.org`. Then set
`TELEGRAM_API_BASE_URL=https://tg-gateway.your-domain.example`. The gateway sees your bot token, so only use
one you run yourself.

**C. Build the image on your laptop and ship it** (no registry or PyPI access needed on the server):

```bash
# [laptop] WSL or Git Bash, in the repo, same commit as the server
deploy/ship-images.sh deploy@SERVER_IP
# [server]
cd ~/persian-voice-assistant && git pull && SKIP_BUILD=1 deploy/deploy.sh
```

From PowerShell without WSL: `docker compose -f docker-compose.prod.yml build`, then
`docker save -o pva.tar persian-voice-assistant:<short-commit> postgres:16-bookworm caddy:2.10.2`, then
`scp pva.tar deploy@SERVER_IP:`, then **[server]** `docker load -i pva.tar`. Get `<short-commit>` from
`git rev-parse --short HEAD`.

**D. Mirrors on the server.** Docker Hub: add a registry mirror your provider offers to
`/etc/docker/daemon.json`, then `sudo systemctl restart docker`. Check the provider's documentation for the
current address, for example ArvanCloud's `https://docker.arvancloud.ir`:

```json
{ "registry-mirrors": ["https://YOUR-MIRROR"], "log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "5"} }
```

PyPI, Debian and the Vosk model take build arguments, set in the shell or in `.env`:
`PIP_INDEX_URL=https://your-pypi-mirror/simple`, `APT_MIRROR=http://your-debian-mirror/debian`,
`VOSK_MODEL_URL=https://where-you-host/vosk-model-small-fa-0.42.zip`.

> **Terms of service.** Google, Resend and Docker restrict access from sanctioned regions in their terms.
> Routing around a provider's geo restriction may break those terms. That is your decision. The simplest
> fully compliant setup is a server outside the restricted region; then none of step 5 is needed.

Once the stack is running, re-check from inside the containers, with the app's own proxy settings:
`docker compose -f docker-compose.prod.yml run --rm --no-deps api python -m app.cli check-outbound`.

## 6. Create `.env` on the server (secrets never go through git)  [server]

```bash
cd ~/persian-voice-assistant
cp .env.example .env && chmod 600 .env
for i in 1 2 3 4 5; do openssl rand -base64 33 | tr -d '/+=' ; done   # five random strings for the next step
nano .env
```

Fill in every value. `deploy.sh` refuses to start while any required value is empty or still says
`replace-me`/`change-me`. Production specifics:

| Variable | Value |
|---|---|
| `POSTGRES_PASSWORD`, `ADMIN_API_KEY`, `MCP_API_KEY`, `DASHBOARD_SECRET_KEY`, `DASHBOARD_DB_PASSWORD` | the random strings |
| `POSTGRES_HOST` | `db` |
| `DASHBOARD_HOST` | `usage.example.com` |
| `ACME_EMAIL` | your email (certificate expiry notices) |
| `DEMO_DATA` | `false` (or `true` for a first look, then purge) |
| `LOG_FORMAT` | `json` |
| `DEBUG` | `false` |
| `GOOGLE_*` | from your laptop: `python scripts/google_auth.py` (README "Google Calendar setup") |
| `HTTPS_PROXY` etc. | only if step 5 needed them |

Prefer copying a prepared file from your laptop over typing secrets in a terminal:
`scp .env.production deploy@SERVER_IP:persian-voice-assistant/.env`, then `chmod 600 .env`.

Dashboard password hash (prompts twice, prints the line for `.env`):

```bash
docker compose -f docker-compose.prod.yml build api      # skip if you shipped the images (step 5C)
docker compose -f docker-compose.prod.yml run --rm --no-deps api python -m app.dashboard.auth hash-password
nano .env                                                # paste DASHBOARD_PASSWORD_HASH=...
```

## 7. The Vosk Persian model

Nothing to do by hand. The image build downloads `vosk-model-small-fa-0.42` (53 MB) from alphacephei.com
and bakes it into the image at `/opt/vosk-model`. The build log prints its SHA-256. To make later builds
tamper-evident, put that value in `.env` as `VOSK_MODEL_SHA256=…`. If alphacephei.com is blocked, use 5C or set
`VOSK_MODEL_URL` to a copy you host.

## 8. DNS for the dashboard  [your DNS provider]

| Type | Name | Value | TTL |
|---|---|---|---|
| A | `usage` (i.e. `usage.example.com`) | `SERVER_IP` | 300 |
| AAAA | `usage` | the server's IPv6, **only if** it has one and port 443 is open on it | 300 |

Check from your laptop: `nslookup usage.example.com` must return `SERVER_IP`. Do this **before** step 9:
Caddy requests the certificate on first start, and Let's Encrypt must reach the server on port 80/443 under
that name. If you use Cloudflare, set the record to "DNS only" (grey cloud) for the first certificate.

## 9. First start  [server]

```bash
cd ~/persian-voice-assistant
deploy/deploy.sh                 # or: SKIP_BUILD=1 deploy/deploy.sh   if you shipped images (5C)
```

`deploy.sh` checks `.env`, builds the image, starts `db`, runs the `migrate` one-shot (`alembic upgrade head`,
the dashboard's read-only role, optional demo data), starts `bot`, `api`, `dashboard` and `caddy`, waits until
every healthcheck passes, then calls `/health` and the dashboard URL. The manual equivalent:

```bash
export APP_VERSION=$(git rev-parse --short HEAD)
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps                       # all "healthy"; migrate "exited (0)"
docker compose -f docker-compose.prod.yml logs migrate             # "Running upgrade ... -> 0006"
docker compose -f docker-compose.prod.yml run --rm --no-deps migrate alembic upgrade head   # re-run migrations by hand
```

## 10. Check it works

```bash
docker compose -f docker-compose.prod.yml logs --tail 50 bot       # "Starting bot (long polling)", no errors
curl -s http://127.0.0.1:8000/health | python3 -m json.tool        # database, telegram and llm all "ok": true
curl -sI https://usage.example.com/login | head -1                 # HTTP/2 200
curl -s -o /dev/null -w '%{http_code}\n' https://usage.example.com/ # 303 (redirect to login: no public access)
```

`/health` → `llm` fails with "model … not offered" if `DEEPSEEK_MODEL` is retired; the message lists the
names available. Then, from Telegram:

1. `/start`: Persian greeting. `/dashboard`: a one-time sign-in link for `https://usage.example.com`.
2. Seed yourself and the contacts once: `docker compose -f docker-compose.prod.yml run --rm --no-deps api python -m app.db.seed`.
   Change the sample contacts to real addresses with `POST /contacts` (README "Admin API").
3. Voice: "فردا ساعت ده صبح با علی جلسه بگذار" → preview → ✅ → the event appears in Google Calendar and the guest is invited.
4. Voice: "به سارا ایمیل بزن و بگو گزارش را فردا می‌فرستم" → preview → ✅ → the email arrives.
5. `docker compose -f docker-compose.prod.yml run --rm api python -m app.cli eval-agent` → the 30-item accuracy report.

Reach the API, dashboard or MCP from your laptop without opening ports, through an SSH tunnel:
`ssh -L 8000:127.0.0.1:8000 -L 8080:127.0.0.1:8080 deploy@SERVER_IP`, then `http://127.0.0.1:8000/health`.

## 11. Logs and day-to-day

```bash
docker compose -f docker-compose.prod.yml logs -f bot                       # JSON lines; rotated at 10 MB × 5
docker compose -f docker-compose.prod.yml logs bot | grep '"run_id": 42'    # one request end to end
docker compose -f docker-compose.prod.yml restart bot
docker stats --no-stream                                                    # memory vs. the limits
```

## 12. Nightly backups, kept off the server, and a tested restore  [server]

`deploy/backup.sh` writes `pg_dump` (custom format), checks the archive, optionally encrypts it with
[age](https://github.com/FiloSottile/age), copies it off the server with
[rclone](https://rclone.org), and prunes old copies.

```bash
sudo apt-get install -y rclone age
sudo mkdir -p /var/backups/pva && sudo chown deploy:deploy /var/backups/pva
```

Encryption, so a stolen backup is useless. **[laptop]** `age-keygen -o pva-backup-key.txt`; keep the file
safe. Its `# public key: age1…` line goes to the server:

```bash
cat > ~/persian-voice-assistant/deploy/backup.env <<'EOF'
BACKUP_DIR=/var/backups/pva
KEEP_DAYS=14
BACKUP_AGE_RECIPIENT=age1REPLACE_WITH_YOUR_PUBLIC_KEY
BACKUP_RCLONE_REMOTE=offsite:pva-backups
REMOTE_KEEP_DAYS=30
EOF
chmod 600 ~/persian-voice-assistant/deploy/backup.env
rclone config        # create remote "offsite": any S3-compatible bucket (e.g. your provider's object storage)
```

Run it once by hand, then schedule it for every night at 03:15:

```bash
~/persian-voice-assistant/deploy/backup.sh
rclone ls offsite:pva-backups                     # the file is there
( crontab -l 2>/dev/null; echo '15 3 * * * cd /home/deploy/persian-voice-assistant && deploy/backup.sh >> /var/backups/pva/backup.log 2>&1' ) | crontab -
```

No bucket? Leave `BACKUP_RCLONE_REMOTE` empty and pull the backups to your laptop regularly:
`scp "deploy@SERVER_IP:/var/backups/pva/pva-*.age" D:\backups\pva\` (PowerShell).

**Test a restore** right after the first backup, and then monthly. It runs without downtime and touches
nothing live:

```bash
f=$(ls -t /var/backups/pva/pva-*.dump* | head -1)
# encrypted? decrypt on the laptop, or copy the key temporarily: age -d -i pva-backup-key.txt -o /tmp/check.dump "$f"
deploy/restore.sh --check /tmp/check.dump      # restores into a scratch database, prints row counts, drops it
rm -f /tmp/check.dump
```

A real restore (replaces the live database; asks you to type `restore`; takes a safety dump first):
`deploy/restore.sh /path/to/backup.dump`.

## 13. Updates and rollback

**[laptop]** commit and `git push`. Then **[server]**:

```bash
cd ~/persian-voice-assistant
deploy/deploy.sh            # pull, safety backup, build, migrate, restart, health checks
deploy/deploy.sh v1.0.1     # or a specific tag/commit
```

Running it twice is harmless: an unchanged commit pulls nothing, builds from cache and leaves running
containers alone. Before every update that changes the code it saves a local backup and remembers the
previous commit and schema revision in `.deploy/`.

Roll back to the previous commit:

```bash
deploy/rollback.sh                                   # downgrades the schema if needed, redeploys the old commit
deploy/rollback.sh --restore /var/backups/pva/pva-<timestamp>.dump   # or: restore the pre-update backup instead
```

Prefer `--restore` with the pre-update dump when a migration's downgrade would drop data. For example,
0003's downgrade deletes typed (text) messages. Afterwards, `git checkout main` returns to the branch for
the next update.

## 14. Troubleshooting

| Symptom | Check |
|---|---|
| Bot silent | `logs bot`; `check-outbound` (Telegram blocked → step 5); token correct; only one bot instance per token |
| `/health` llm not ok | key and balance at DeepSeek; model name (`/health` lists the available ones); proxy |
| "دسترسی به تقویم گوگل منقضی شده" | refresh token revoked or the OAuth app still "Testing" (7-day expiry): re-run `scripts/google_auth.py` |
| Email rejected | `EMAIL_FROM` domain verified in Resend; `docker compose … run --rm api python -m app.cli send-test-email` |
| Dashboard: no certificate | DNS A record, ports 80/443 open, `logs caddy` |
| Dashboard login loop | `DASHBOARD_HOST` must match the URL you open (cookies are `Secure` on https) |
| `migrate` exited 1 | `logs migrate`; fix, then `deploy/deploy.sh` again (or `deploy/rollback.sh`) |
| Disk full | `docker system df`; `docker image prune`; old backups in `/var/backups/pva` |
