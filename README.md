# Personal Knowledge Platform (PKP)

Connects a OneDrive document library to Claude.ai via MCP. Users query their
documents through Claude.ai chat. Documents are chunked, embedded, and stored
in Qdrant; Claude calls a small set of MCP tools to search and read them.

This README is the full bootstrap guide for a fresh server.

---

## 1. Prerequisites

### Server sizing (Hetzner Cloud recommended)

Pick based on your OneDrive library size:

| Library size | Recommended | Specs | Price (Hetzner) |
|---|---|---|---|
| Up to ~10k files / 50 GB | CX32 | 4 vCPU, 8 GB RAM, 80 GB disk | ~€9/mo |
| Up to ~50k files / 200 GB | **CX42** (sweet spot) | 8 vCPU, 16 GB RAM, 160 GB disk | ~€18/mo |
| Up to ~200k files / 1 TB | **CX52** (recommended) | 16 vCPU, 32 GB RAM, 360 GB disk | ~€36/mo |
| 1 TB+ | CX62 | 32 vCPU, 64 GB RAM, 600 GB disk | ~€68/mo |

> The bottleneck is RAM (the embedding model needs ~2 GB) and disk (Qdrant
> stores vectors — roughly 1 GB per 50k chunks). CPU matters during the
> initial index; after that it's mostly idle.

**OS:** Ubuntu 24.04 LTS on all of the above.

To provision on Hetzner:
1. Go to https://console.hetzner.cloud → your project → **Add Server**
2. Location: any (Nuremberg or Helsinki are fine)
3. Image: **Ubuntu 24.04**
4. Type: pick from table above
5. SSH Keys: add your public key (paste output of `cat ~/.ssh/id_ed25519.pub` from your laptop)
6. Name it something meaningful (e.g. `pkp-prod`)
7. Click **Create & Buy now**

The server's public IPv4 appears on the dashboard within 30 seconds.

### Other prerequisites

- Root access (or a sudo user) on the server.
- Either a real domain pointed at the server's IP, **or** use
  `<your-ip-with-dashes>.nip.io` for free (e.g. IP `1.2.3.4` → domain `1-2-3-4.nip.io`).
- A Microsoft account with the OneDrive library you want to index.
- Python 3.12, Docker, Caddy — installed in step 4 below.

### Connecting from Windows

Open PowerShell **as Administrator** (right-click PowerShell → "Run as
administrator"). If `ssh` is not recognised, run this once to enable it:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
```

Then connect:

```powershell
ssh root@<SERVER_IP>
```

The first time you connect, it will ask:

```
Are you sure you want to continue connecting (yes/no/[fingerprint])?
```

Type `yes` and press Enter. This is normal — it's saving the server's
fingerprint so future connections are verified automatically.

The repo can be cloned anywhere — paths inside the Python code resolve
relative to the repo root automatically. The systemd `install.sh` script
also figures out the path and user for you.

---

## 2. Credentials to gather first

Before touching the server, prepare these. You'll paste them into `.env` in
step 6.

### 2a. Azure app (for OneDrive access)

1. Go to <https://portal.azure.com> → **Microsoft Entra ID** → **App
   registrations** → **New registration**.
2. Name: anything (e.g. "PKP"). Supported account types:
   **Accounts in any organizational directory and personal Microsoft accounts**.
3. After creation, open the app:
   - **Overview** → copy the **Application (client) ID** → this is
     `ONEDRIVE_CLIENT_ID`.
   - **Authentication** → **Add a platform** → **Mobile and desktop
     applications** → tick the `https://login.microsoftonline.com/common/oauth2/nativeclient`
     redirect URI and save.
   - **Authentication** → scroll to **Advanced settings** → **Allow public
     client flows** → set to **Yes** → save.
   - **API permissions** → **Add a permission** → **Microsoft Graph** →
     **Delegated permissions** → add `Files.ReadWrite.All` and
     `offline_access`. Grant admin consent if prompted.
4. `ONEDRIVE_TENANT_ID` is `common` (works for both personal and work
   accounts).

No client secret is needed — this is a public client (device-code flow).

### 2b. Bearer token and upload signing key

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"   # MCP_BEARER_TOKEN
python3 -c "import secrets; print(secrets.token_hex(32))"   # UPLOAD_SIGNING_KEY
```

Keep both safe. The bearer token is what Claude Desktop sends on every request.

---

## 3. Pick your public URL

Either:
- A **real domain** with an A record pointing at the server's IP, **or**
- A **nip.io** subdomain like `1-2-3-4.nip.io` where `1-2-3-4` is your IP
  with dots replaced by dashes (no DNS setup needed).

You'll need this in step 6 (`PUBLIC_BASE_URL`) and step 9 (Caddy).

---

## 4. System setup (run as root or with sudo)

```bash
# Update and install base packages
apt update && apt upgrade -y
apt install -y python3.12 python3.12-venv python3-pip git curl tmux ufw

# Docker
curl -fsSL https://get.docker.com | sh

# Caddy (official repo)
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy

# Firewall: SSH + HTTPS only
ufw allow 22/tcp
ufw allow 443/tcp
ufw --force enable
```

Create a non-root user (skip if your sudo user already exists):

```bash
adduser marcvista
usermod -aG sudo,docker marcvista
```

Add your SSH key so you can log in as the new user:

```bash
mkdir -p /home/marcvista/.ssh
cp /root/.ssh/authorized_keys /home/marcvista/.ssh/authorized_keys
chown -R marcvista:marcvista /home/marcvista/.ssh
chmod 700 /home/marcvista/.ssh
chmod 600 /home/marcvista/.ssh/authorized_keys
```

Disable password auth:

```bash
sed -i 's/^#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh
```

> **Important:** The `sudo` and `docker` group memberships only take effect after a **fresh login**. After running `usermod`, always log out of root and reconnect as your new user from your laptop (`ssh marcvista@<SERVER_IP>`). Do not use `su - marcvista` from within the root session — group memberships will be missing and `sudo` / `docker` commands will fail.

---

## 5. Clone the repo

As the non-root user (e.g. `marcvista`):

```bash
cd ~
git clone https://github.com/<your-account>/kb-app.git
cd kb-app
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

The `requirements.txt` is heavy (includes torch + CUDA libs for embedding).
Allow ~5 minutes and ~3 GB of disk.

---

## 6. Configure `.env`

```bash
cp .env.example .env
nano .env
```

Fill in everything from step 2. `ONEDRIVE_REFRESH_TOKEN` stays blank — it's
written automatically in step 8.

---

## 7. Start Qdrant

```bash
docker compose up -d
# Wait for Qdrant to initialize, then verify
sleep 10 && curl http://localhost:6333/collections
# Expected: {"result":{"collections":[]},"status":"ok",...}
```

---

## 8. First-run OneDrive auth (one-time, manual)

```bash
source venv/bin/activate
python onedrive.py
```

It will print a URL and a code. Open the URL in any browser, sign in with
the OneDrive account, paste the code. On success, the refresh token is
written to `.env` automatically and a list of files appears.

> ⚠ This step **must** complete before starting the systemd services. The
> services have no way to do an interactive login.

---

## 9. Install systemd units and Caddy config

From the repo root, as root:

```bash
sudo ./deploy/install.sh
# It will prompt for your domain (e.g. example.com or 1-2-3-4.nip.io)
```

Then enable and start everything — **run each line separately**:

```bash
sudo systemctl enable --now caddy
```
```bash
sudo systemctl enable --now pkp-mcp.service
```
```bash
sudo systemctl enable --now pkp-indexer.timer
```

> **Note:** Run these one at a time, not as a block. Pasting all three at once can cause the shell to swallow the second and third commands silently.

Verify:

```bash
sudo systemctl status pkp-mcp.service caddy pkp-indexer.timer
```
```bash
curl https://<your-domain>/health
# Expected: {"status":"ok"}
```

Caddy will request a Let's Encrypt cert on first run — give it ~30 seconds.

If `pkp-mcp.service` shows `inactive (dead)` after enabling, start it manually and check the log:

```bash
sudo systemctl start pkp-mcp.service
sleep 5
sudo tail -n 30 /var/log/pkp/mcp.log
```

---

## 10. First full index

The hourly delta-indexer timer is now running, but the very first index has
to be a full one. Run it in `tmux` because it can take hours on a large
library.

```bash
tmux new -s indexer
source venv/bin/activate
python -m ingestion.runner --full
# Detach with Ctrl-b d
```

Monitor:

```bash
watch -n 10 'cat /var/pkp/status.json'
```

Expect ~30 chunks per file on average. A 1000-file library typically takes
1–3 hours depending on file types and sizes.

---

## 11. Verify end-to-end

```bash
# Service health (all three should be active)
sudo systemctl status pkp-mcp.service caddy pkp-indexer.timer
```
```bash
# HTTPS endpoint
curl https://<your-domain>/health
# Expected: {"status":"ok"}
```
```bash
# Qdrant collection (should show pkp_chunks with vectors)
curl http://localhost:6333/collections/pkp_chunks
```
```bash
# Authenticated tool call (replace <TOKEN> with your MCP_BEARER_TOKEN)
curl https://<your-domain>/tools/index_status \
    -H "Authorization: Bearer <TOKEN>"
# Expected: JSON with total_chunks > 0
```
```bash
# Live MCP log (Ctrl-C to stop)
tail -f /var/log/pkp/mcp.log
```

---

## 12. Connect Claude Desktop

See [CLIENT_SETUP.md](CLIENT_SETUP.md). You give the end user:
- Your `PUBLIC_BASE_URL`
- The `MCP_BEARER_TOKEN`
- The `pkp_bridge.py` file

---

## Tuning for large libraries (100k+ files)

For libraries with hundreds of thousands of files, two things tend to bite:
Microsoft Graph 429 throttling, and Claude Desktop's ~1 MB MCP response cap.
The code defaults handle both, but if you see persistent 429 errors or
"response too large" complaints from Claude, tune as follows.

### Reduce indexer concurrency

The default is 6 workers downloading in parallel. On a library where Graph
is sustained-throttling you, halve this:

```bash
sudo systemctl edit pkp-full-indexer.service
```

Paste this and save:

```ini
[Service]
Environment="PKP_INGEST_WORKERS=3"
# Optional: raise the crash-loop circuit breaker so brief restart bursts
# don't halt the service. Default is 5 restarts / 30 min.
StartLimitBurst=10
StartLimitIntervalSec=3600
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart pkp-full-indexer.service
```

Fewer parallel downloads → proportionally fewer 429s. Trade-off is slower
wall-clock indexing, but you lose fewer files to throttle-exhausted retries.

### How throttling is handled

`_download_to` honors Microsoft's `Retry-After` header on 429/5xx — if Graph
asks for 300s, we wait exactly 300s (clamped to 5 min ceiling), falling back
to exponential backoff if the header is missing. Max attempts is 10. You
should see far fewer "DOWNLOAD ERROR ... 429" lines in the log after this
patch landed.

### Search response size

`search_documents` truncates each result's text to ~800 chars by default so
broad queries don't blow past Claude Desktop's MCP cap. If a caller wants
the full chunk text back in the search response (rather than a follow-up
`get_document` call), pass `full_text=true` in the request body. The
default-off behavior is the right one for Claude Desktop — full text is
always reachable via `get_document`.

---

## Updating later

Pull updates from `main`:

```bash
cd ~/kb-app
git pull origin main
source venv/bin/activate
pip install -r requirements.txt   # in case deps changed
sudo systemctl restart pkp-mcp.service
sudo systemctl status pkp-mcp.service
curl https://<your-domain>/health
```

If a release requires re-indexing or a migration, the release notes will say
so explicitly. Otherwise, the steps above are sufficient.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `pkp-mcp.service` keeps restarting | `.env` missing a key; check `tail -f /var/log/pkp/mcp.log` |
| Caddy fails to get cert | Port 443 blocked, or domain doesn't resolve to this server |
| `python onedrive.py` says "AADSTS7000218" | "Allow public client flows" not enabled on the Azure app |
| Indexer finds 0 files | Refresh token not yet written; re-run `python onedrive.py` |
| Claude Desktop says "401" | Bearer token in client config doesn't match server `.env` |
| `index_status` returns 0 points | First full index never ran — see step 10 |
| `docker compose up -d` says "permission denied" on docker socket | `marcvista` user is not in the `docker` group for this session. Fix: log out completely and log back in, or run `exit` to root then `usermod -aG docker marcvista`, then `su - marcvista`. Verify with `groups` — `docker` must appear. |
| `Failed to enable unit: Unit file caddy.service does not exist` | Caddy was not installed yet when `install.sh` ran. Install it first (step 4c), then re-run `sudo systemctl enable --now caddy`. If `apt install caddy` asks about the Caddyfile conflict, choose **N** to keep the version that `install.sh` already wrote. |
| `pkp-mcp.service` fails with `ModuleNotFoundError: No module named 'slowapi'` | `slowapi` was missing from the venv. Run `pip install slowapi==0.1.9` then `sudo systemctl restart pkp-mcp.service`. |
| `pkp-mcp.service` fails with `ModuleNotFoundError: No module named 'mcp'` | MCP SDK missing. Run `pip install mcp==1.9.0` then `sudo systemctl restart pkp-mcp.service`. |
| `pkp-mcp.service` fails with any other `ModuleNotFoundError` | A dependency is missing. Re-run `pip install -r requirements.txt` inside the venv, then restart the service. |
| Indexer crashes with `504 Server Error: Gateway Timeout` from `graph.microsoft.com` | OneDrive's Graph API timed out while listing a large folder. The current code retries automatically with exponential backoff (up to 6 attempts). If it still fails, just re-run `python -m ingestion.runner --full` — the content-hash deduplication means already-indexed files are skipped. |
| Many "DOWNLOAD ERROR ... 429 Too Many Requests" in `full-indexer.log` | Microsoft Graph is throttling sustained download traffic. `_download_to` honors `Retry-After` on 429 and retries up to 10 times, but if you see hundreds of these, lower `PKP_INGEST_WORKERS` (see *Tuning for large libraries*). Halving workers roughly halves the 429 rate. |
| Claude Desktop reports "response too large" or memory issues on search | The search response exceeded Claude Desktop's ~1 MB MCP cap. Lower the `top_k` you're requesting, or rely on the default 800-char preview truncation (full text is always reachable via `get_document`). |
| `sudo: marcvista is not in the sudoers file` | The `usermod -aG sudo` was applied but the session predates it. Log out fully (`exit` from the SSH session, then reconnect via `ssh marcvista@<SERVER_IP>`) and try again. |

Logs:

```bash
tail -f /var/log/pkp/mcp.log
tail -f /var/log/pkp/indexer.log
sudo journalctl -u caddy -f
```

---

## Architecture summary

See [CLAUDE.md](CLAUDE.md) for the full architecture, data model, and
non-negotiable implementation rules.
