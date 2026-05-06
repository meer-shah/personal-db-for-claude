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
# Add your SSH key to /home/marcvista/.ssh/authorized_keys
# Then disable password auth in /etc/ssh/sshd_config and: systemctl restart ssh
```

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
# Verify
curl http://localhost:6333/collections
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

Then enable and start everything:

```bash
sudo systemctl enable --now caddy
sudo systemctl enable --now pkp-mcp.service
sudo systemctl enable --now pkp-indexer.timer
```

Verify:

```bash
sudo systemctl status pkp-mcp.service caddy
curl https://<your-domain>/health
# Expected: {"status":"ok"}
```

Caddy will request a Let's Encrypt cert on first run — give it ~30 seconds.

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
# Health
curl https://<your-domain>/health

# Index status (auth required)
curl https://<your-domain>/tools/index_status \
    -H "Authorization: Bearer $MCP_BEARER_TOKEN"
# Expected: JSON with point count > 0
```

---

## 12. Connect Claude Desktop

See [CLIENT_SETUP.md](CLIENT_SETUP.md). You give the end user:
- Your `PUBLIC_BASE_URL`
- The `MCP_BEARER_TOKEN`
- The `pkp_bridge.py` file

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
