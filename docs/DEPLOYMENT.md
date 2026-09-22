# Deploying the API to aaPanel

GitHub Actions builds the image and publishes it to GitHub Container Registry,
then SSHes into the server to pull and restart. The server never sees the
source and needs no build tools.

```
push to main
   |
   ruff + mypy + pytest        <- a failure stops everything here
   |
   docker build -> ghcr.io/shabir0786/leadpilot_backend:<sha>
   |
   ssh -> pull, alembic upgrade head, restart, health check
```

Set up once, then every push to `main` deploys.

---

## 1. On the server (once)

SSH in as root, or use aaPanel's Terminal.

### Docker

aaPanel ships a Docker manager in **App Store → Docker**; installing it there
is the simplest route. To check what you have:

```bash
docker --version && docker compose version
```

If `docker compose` is missing (you have the older `docker-compose`), install
the plugin — the workflow uses the v2 syntax.

### A deploy folder

```bash
mkdir -p /www/leadpilot-api && cd /www/leadpilot-api
```

Remember this path: it becomes the `DEPLOY_PATH` secret.

### The compose file

Copy `docker-compose.prod.yml` from the repo to `/www/leadpilot-api/docker-compose.yml`.
Either paste it in with `nano`, or:

```bash
curl -fsSL -o docker-compose.yml \
  https://raw.githubusercontent.com/SHABIR0786/LeadPilot_Backend/main/docker-compose.prod.yml
```

### The environment file

`.env` holds the JWT secret and every provider key, so it never goes in git —
create it directly on the server:

```bash
nano /www/leadpilot-api/.env
```

Start from `.env.example` and set at least:

```ini
ENVIRONMENT=production
DEBUG=false

# python -c "import secrets; print(secrets.token_urlsafe(32))"
SECRET_KEY=<a new 32-byte secret, NOT the development one>

# aaPanel's MySQL. MYSQL_HOST is set by compose; do not set it here.
MYSQL_USER=leadpilot
MYSQL_PASSWORD=<the password you set in aaPanel>
MYSQL_DB=LeadPilot

# --- URLs (no domain yet: the VM's IP, over http) ---------------------------
# Where the API is reachable from outside. n8n posts its callbacks here, so it
# must be the public address, never localhost.
PUBLIC_API_URL=http://43.157.81.74:8000

# Where the frontend is served. Used to build emailed links (password reset,
# email verification), so a wrong value sends users somewhere that does not
# exist.
FRONTEND_URL=http://43.157.81.74:8090

# The frontend runs on a different port, which makes it a different origin, so
# it has to be listed here or every API call from the browser is blocked.
# Scheme + host + port, no trailing path.
BACKEND_CORS_ORIGINS=http://43.157.81.74:8090

N8N_WEBHOOK_URL=<your n8n production webhook>
N8N_WEBHOOK_SECRET=<shared secret>
DEEPSEEK_API_KEY=<key>
EMAIL_ENABLED=true
SMTP_USER=<...>
SMTP_PASSWORD=<...>
```

> **`http`, not `https`.** A certificate cannot be issued for a bare IP
> address — Let's Encrypt only signs domain names. A self-signed certificate
> would make every browser show a full-page warning and the desktop shell
> refuse to load the app. Plain `http` is the workable option until there is a
> domain; see *Moving to a domain later* at the end.

Then lock it down — it is the most sensitive file on the box:

```bash
chmod 600 /www/leadpilot-api/.env
```

### MySQL

In aaPanel → **Databases**, create the database and user, then let the
container reach it. aaPanel's MySQL usually binds to `127.0.0.1` only, so
allow the Docker bridge:

```bash
# Find the bridge subnet (typically 172.17.0.0/16)
ip addr show docker0 | grep inet
```

Grant the user access from it (adjust the subnet if yours differs):

```sql
CREATE USER 'leadpilot'@'172.%' IDENTIFIED BY '<password>';
GRANT ALL PRIVILEGES ON LeadPilot.* TO 'leadpilot'@'172.%';
FLUSH PRIVILEGES;
```

And make sure MySQL listens on the bridge, not just loopback — in aaPanel's
MySQL config, `bind-address = 0.0.0.0`. **Then confirm your firewall does not
expose 3306 publicly**; aaPanel's own firewall panel is the place to check.

### An SSH key for Actions

Generate a key *for the pipeline only*, so it can be revoked without touching
your own access:

```bash
ssh-keygen -t ed25519 -C "github-actions-leadpilot" -f ~/.ssh/gh_deploy -N ""
cat ~/.ssh/gh_deploy.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
cat ~/.ssh/gh_deploy          # the PRIVATE key -> GitHub secret, then delete it here
```

---

## 2. In GitHub (once)

**Settings → Secrets and variables → Actions**

| Secret | Value |
|---|---|
| `SSH_HOST` | Server IP or hostname |
| `SSH_USER` | `root`, or a user in the `docker` group |
| `SSH_KEY` | The **private** key printed above, whole file including the BEGIN/END lines |
| `SSH_PORT` | Only if not 22 |
| `DEPLOY_PATH` | `/www/leadpilot-api` |

Add two **variables** (not secrets) on the same page:

| Variable | Value |
|---|---|
| `PUBLIC_API_URL` | `http://43.157.81.74:8000` — shown as the deploy link |
| `API_PORT` | `8000` unless you changed it |

No registry secret is needed: Actions publishes to ghcr.io with its own
`GITHUB_TOKEN`.

### Let the server pull the image

A private repo's images are private too, so the VM needs credentials. Use a
token scoped to nothing but reading packages — the repo stays private and the
token cannot touch the source.

1. **https://github.com/settings/tokens** → **Tokens (classic)** →
   **Generate new token (classic)**
2. Note: `LeadPilot VM pull`. Expiration: your call — a year is reasonable.
3. Scopes: tick **`read:packages`** and nothing else.
4. Copy the token (`ghp_...`; GitHub shows it once) and add it as the repo
   secret **`GHCR_TOKEN`**.

That single scope is the point: if the VM is ever compromised, the token pulls
images and does nothing else — no source, no pushes, no other repositories.

The workflow uses `GHCR_TOKEN` when present and falls back to the built-in
`GITHUB_TOKEN` otherwise, so nothing else needs changing.

> Note the expiry. A token that lapses makes deploys fail with `denied`
> months later, which is a confusing symptom if you have forgotten it exists.

---

## 3. First deploy

The first one is manual, because nothing is running yet to restart:

```bash
cd /www/leadpilot-api
echo "LEADPILOT_IMAGE=ghcr.io/shabir0786/leadpilot_backend:latest" > .env.deploy

# Once only: Docker saves this in ~/.docker/config.json, and the automated
# deploys log in again themselves.
echo "<GHCR_TOKEN>" | docker login ghcr.io -u SHABIR0786 --password-stdin
docker compose --env-file .env.deploy pull api

# Creates the schema. Safe to re-run; it is a no-op when already current.
docker compose --env-file .env.deploy run --rm api alembic upgrade head

docker compose --env-file .env.deploy up -d api
curl http://127.0.0.1:8000/api/v1/health/ready
```

Expect `{"status":"ready","database":"ok"}`. Anything else, see Troubleshooting.

### Expose the API on port 8000

The container binds to `127.0.0.1:8000`, so nothing outside the VM can reach
it yet. With no domain there is no nginx site to attach it to, so put a plain
reverse proxy in front.

In aaPanel → **Website → Add site**, use the IP `43.157.81.74` as the domain
and set the port to **8000**. Then in the site's **Configuration file**,
replace the `location /` block:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # The progress stream is Server-Sent Events. Buffering makes the search
    # tracker appear frozen until the whole run finishes.
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
}
```

If aaPanel will not accept a bare IP as a site, skip nginx and publish the
container port directly — edit `docker-compose.yml` on the server:

```yaml
ports:
  - "8000:8000"      # instead of "127.0.0.1:8000:8000"
```

That is fine for now, though it means no nginx in front for logs or rate
limiting later.

### Open the ports

Two places, and **both** have to allow it:

1. **Your cloud provider's security group / firewall** — this is the one that
   is currently blocking 8090. Open **8000** (API) and **8090** (frontend).
2. **aaPanel → Security** — add the same two ports.

Check from your own machine:

```bash
curl http://43.157.81.74:8000/api/v1/health
```

Expect `{"status":"ok","environment":"production"}`. A hang rather than a
refusal usually means the cloud firewall, not aaPanel.

### Serving the frontend on 8090

Build it locally and copy the output up:

```bash
# on your machine, in LeadPilot/
#   .env first: VITE_API_BASE_URL=http://43.157.81.74:8000/api/v1
pnpm build
scp -r dist/* root@43.157.81.74:/www/leadpilot-web/
```

Then add a second aaPanel site on port **8090** with its root at
`/www/leadpilot-web`, and make it fall back to `index.html` — the app is a
single-page router, so a refresh on `/leads` is a 404 without this:

```nginx
location / {
    try_files $uri $uri/ /index.html;
}
```

> `VITE_API_BASE_URL` is baked in at build time, not read at runtime. Point it
> at the VM's IP **before** `pnpm build`, or the deployed frontend will call
> `localhost` from your users' browsers.

---

## 4. Everyday use

**Deploy:** push to `main`. Watch it in the repo's **Actions** tab.

**Redeploy without a commit:** Actions → Deploy API → **Run workflow**.

**Roll back:** Run workflow, and put an older tag (the 12-character commit SHA)
in the *image tag* box. Tests are skipped — that image already passed them.

```bash
# See what is available
docker images ghcr.io/shabir0786/leadpilot_backend
```

**Logs:**

```bash
cd /www/leadpilot-api
docker compose --env-file .env.deploy logs -f --tail 100 api
```

**What is running right now:**

```bash
grep LEADPILOT_IMAGE /www/leadpilot-api/.env.deploy
```

---

## Troubleshooting

**`Cannot reach the database` from `/health/ready`**

The container cannot see aaPanel's MySQL. Check in order:

```bash
# Does the bridge alias resolve?
docker compose --env-file .env.deploy run --rm api \
  python -c "import socket; print(socket.gethostbyname('host.docker.internal'))"

# Is MySQL listening beyond loopback?
ss -lntp | grep 3306
```

If it shows only `127.0.0.1:3306`, set `bind-address = 0.0.0.0` in MySQL and
restart it from aaPanel. Then confirm the grant uses `'leadpilot'@'172.%'`,
not `@'localhost'`.

**`denied` when pulling the image**

The server cannot authenticate to the registry. Either `GHCR_TOKEN` is not
set as a repo secret, or the token has expired — classic tokens do, silently,
and the symptom appears months after it was created. Generate a new one with
`read:packages`, update the secret, and on the VM:

```bash
echo "<new token>" | docker login ghcr.io -u SHABIR0786 --password-stdin
```

**The deploy succeeds but the health check fails**

The container started and then died. The workflow prints the last 60 log lines
on failure — look there first. A missing required setting in `.env` is the
usual cause, and pydantic names the field.

**The search tracker never updates in production**

nginx is buffering the SSE stream. `proxy_buffering off;` above is what fixes
it.

**Every API call from the browser fails with a CORS error**

The frontend's origin is not in `BACKEND_CORS_ORIGINS`. It must match the
browser's address bar exactly — scheme, host **and port**:

```ini
BACKEND_CORS_ORIGINS=http://43.157.81.74:8090
```

`https://` when the site is served over `http`, a missing `:8090`, or a
trailing path all fail to match. Restart the container after changing it:

```bash
cd /www/leadpilot-api && docker compose --env-file .env.deploy up -d api
```

**Sign-in appears to work, then every page bounces back to login**

The session cookie is not being kept. Over plain `http` this is usually the
frontend and API disagreeing about the host — using `localhost` in one place
and the IP in the other counts as two different sites. Make sure the built
frontend calls `http://43.157.81.74:8000/api/v1` and that you are browsing to
the IP, not to a tunnel or `localhost`.

**The browser refuses to connect / shows a certificate warning**

Something is set to `https://` on the IP. There is no certificate for a bare
IP address, so every URL here has to be `http://` — check
`VITE_API_BASE_URL`, `PUBLIC_API_URL` and `FRONTEND_URL`.

---

## Notes

**Migrations run in the pipeline, not on startup.** The app does not migrate
when it boots, so the workflow runs `alembic upgrade head` before the new
container takes over. A migration that cannot run stops the deploy, leaving the
previous version serving.

**One deploy at a time.** The workflow uses a concurrency group, so a second
push waits rather than racing the first.

**Back up before a destructive migration.** The pipeline does not snapshot the
database. For anything that drops or rewrites data, take a backup from
aaPanel's Databases tab first — it takes a few seconds and it is the only way
back.

---

## Running on an IP, without a domain

This works, and is the right call for getting something in front of the client
now. Two things to know while it lasts:

**Everything is unencrypted.** Passwords, session tokens and lead data all
cross the network in the clear. Fine for a demo or internal testing; not
something to put real client data through for long.

**Google sign-in will not work.** Google rejects bare IPs as an authorised
origin, so that button fails until there is a domain. Email and password
sign-in is unaffected.

### Moving to a domain later

A domain costs a few pounds a year and removes both problems. The change is
small — no rebuild of the API, no migration:

1. Point an A record at `43.157.81.74`.
2. In aaPanel, add the domain to both sites and issue a Let's Encrypt
   certificate (its **SSL** tab does this in a click), then turn on forced
   HTTPS.
3. Update `/www/leadpilot-api/.env`:
   ```ini
   PUBLIC_API_URL=https://api.yourdomain.com
   FRONTEND_URL=https://app.yourdomain.com
   BACKEND_CORS_ORIGINS=https://app.yourdomain.com
   ```
   then restart: `docker compose --env-file .env.deploy up -d api`
4. Rebuild the frontend with the new `VITE_API_BASE_URL` and copy it up again
   — the old build still has the IP compiled into it.
5. Update the n8n webhook callbacks to the new API URL.
