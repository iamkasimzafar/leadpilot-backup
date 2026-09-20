# LeadPilot API.
#
# Multi-stage: dependencies are compiled in a builder image and only the
# finished virtualenv is copied into the runtime, so the compilers (needed to
# build asyncmy and bcrypt) never ship to the server.
#
# Build and run on the VM:
#   docker build -f docker/Dockerfile -t leadpilot-api .
#   docker run -d --name leadpilot-api --env-file .env -p 8000:8000 leadpilot-api

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# asyncmy and bcrypt have C extensions with no prebuilt wheel for slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copied on its own so this layer is cached: an app-code change rebuilds in
# seconds instead of reinstalling every dependency.
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# curl for the healthcheck below; tini so Ctrl-C and `docker stop` actually
# reach uvicorn instead of being swallowed by PID 1.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/*

# Never run the app as root.
RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=appuser:appuser . .

# Applies pending migrations, then starts the server. The app does not migrate
# on startup, so without this a schema change would meet code expecting it.
COPY --chown=appuser:appuser docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER appuser
EXPOSE 8000

# Readiness, not liveness: it checks the database too, so "healthy" means the
# container can actually serve rather than merely that the process is alive.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/health/ready || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
