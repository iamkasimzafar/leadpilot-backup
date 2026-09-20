# Running the API in Docker

Build the image on the VM and run it. No registry, no pipeline.

## 1. Get the code onto the VM

```bash
cd /www
git clone https://github.com/SHABIR0786/LeadPilot_Backend.git leadpilot-api
cd leadpilot-api
```

## 2. Create the environment file

`.env` holds the JWT secret and every provider key, so it is not in git.

```bash
cp .env.example .env
nano .env
```

The settings that matter for a server deployment:

```ini
ENVIRONMENT=production
DEBUG=false

# python -c "import secrets; print(secrets.token_urlsafe(32))"
SECRET_KEY=<a new 32-byte secret, not the development one>

# aaPanel's MySQL, reached from inside the container. Not 127.0.0.1: inside a
# container that means the container itself.
MYSQL_HOST=host.docker.internal
MYSQL_USER=leadpilot
MYSQL_PASSWORD=<the password from aaPanel>
MYSQL_DB=LeadPilot

# The VM's address, over http. A certificate cannot be issued for a bare IP,
# so https would only produce browser warnings.
PUBLIC_API_URL=http://43.157.81.74:8000
FRONTEND_URL=http://43.157.81.74:8090
BACKEND_CORS_ORIGINS=http://43.157.81.74:8090

N8N_WEBHOOK_URL=<the n8n production webhook>
DEEPSEEK_API_KEY=<key>
```

Then lock it down — it is the most sensitive file on the box:

```bash
chmod 600 .env
```

## 3. Let the container reach MySQL

aaPanel's MySQL usually listens on loopback only, which the container cannot
reach. Two changes:

```bash
# Find the Docker bridge subnet, usually 172.17.0.0/16
ip addr show docker0 | grep inet
```

In aaPanel → **Databases → MySQL config**, set `bind-address = 0.0.0.0` and
restart MySQL. Then grant the user access from the bridge:

```sql
CREATE USER 'leadpilot'@'172.%' IDENTIFIED BY '<password>';
GRANT ALL PRIVILEGES ON LeadPilot.* TO 'leadpilot'@'172.%';
FLUSH PRIVILEGES;
```

> Check that your cloud firewall does **not** expose 3306 publicly. Opening
> MySQL to the bridge is fine; opening it to the internet is not.

## 4. Build and run

```bash
docker build -f docker/Dockerfile -t leadpilot-api .

docker run -d \
  --name leadpilot-api \
  --env-file .env \
  --add-host host.docker.internal:host-gateway \
  -p 8000:8000 \
  --restart unless-stopped \
  --log-opt max-size=10m --log-opt max-file=5 \
  leadpilot-api
```

`--add-host` is what makes `host.docker.internal` resolve on Linux; Docker
Desktop defines it automatically but a server does not.

## 5. Check it

```bash
docker logs -f leadpilot-api
```

Expect the database check, then the migrations, then uvicorn. Then:

```bash
curl http://127.0.0.1:8000/api/v1/health/ready
```

`{"status":"ready","database":"ok"}` means it is working. From your own
machine, once the cloud firewall allows 8000:

```bash
curl http://43.157.81.74:8000/api/v1/health
```

## Updating after a code change

```bash
cd /www/leadpilot-api
git pull
docker build -f docker/Dockerfile -t leadpilot-api .
docker stop leadpilot-api && docker rm leadpilot-api
# then the same docker run as above
```

Only the layers that changed rebuild, so a code-only change takes seconds —
the dependency layer is cached as long as `requirements.txt` is untouched.

Worth wrapping in a script once you have done it twice:

```bash
cat > deploy.sh <<'EOF'
#!/bin/sh
set -e
cd /www/leadpilot-api
git pull
docker build -f docker/Dockerfile -t leadpilot-api .
docker stop leadpilot-api 2>/dev/null || true
docker rm leadpilot-api 2>/dev/null || true
docker run -d --name leadpilot-api --env-file .env \
  --add-host host.docker.internal:host-gateway \
  -p 8000:8000 --restart unless-stopped \
  --log-opt max-size=10m --log-opt max-file=5 \
  leadpilot-api
docker image prune -f
echo "Deployed. Logs: docker logs -f leadpilot-api"
EOF
chmod +x deploy.sh
```

## What the container does on start

1. **Waits for MySQL** — up to a minute, retrying. On a VM reboot the
   container usually starts before MySQL is accepting connections, and without
   this it would crash-loop.
2. **Runs `alembic upgrade head`** — the app does not migrate on startup, so
   it happens here. Idempotent: a no-op when the schema is current.
3. **Starts uvicorn.**

A migration that fails stops the container rather than serving against a
schema the code does not expect.

## Troubleshooting

**`exec /usr/local/bin/entrypoint.sh: no such file or directory`**

The script has Windows line endings. `.gitattributes` prevents this, but if
you edited it on Windows:

```bash
sed -i 's/\r$//' docker/entrypoint.sh && docker build -f docker/Dockerfile -t leadpilot-api .
```

**It sits on "Waiting for the database..." then exits**

The container cannot reach MySQL. In order:

```bash
# Does the alias resolve? (needs --add-host)
docker exec leadpilot-api getent hosts host.docker.internal

# Is MySQL listening beyond loopback?
ss -lntp | grep 3306
```

`127.0.0.1:3306` only means `bind-address` is still loopback. If it resolves
and listens correctly, the grant is probably `@'localhost'` rather than
`@'172.%'`.

**Migrations fail on start**

The container exits and the error is in `docker logs leadpilot-api`. The
previous image is untouched, so nothing is half-migrated — fix the cause and
rebuild.

**Every browser call fails with CORS**

`BACKEND_CORS_ORIGINS` must match the frontend's address exactly, including
the port: `http://43.157.81.74:8090`. Restart after editing `.env` — it is
read once at startup.
