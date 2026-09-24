#!/bin/sh
# Bring the schema up to date, then hand over to the command (uvicorn).
#
# The app does not migrate when it boots, so this is where it happens. Running
# it here rather than in a separate step means `docker run` is all you need --
# there is no way to start the API against a stale schema by forgetting a step.
set -e

# MySQL is usually still accepting connections a moment after the host reports
# it as up, and on a VM reboot the container often wins that race. Retry for a
# minute rather than crash-looping.
echo "Waiting for the database..."
attempt=1
until python -c "
import asyncio, sys
from sqlalchemy import text
from app.db.session import SessionLocal

async def check():
    async with SessionLocal() as db:
        await db.execute(text('SELECT 1'))

try:
    asyncio.run(check())
except Exception as exc:
    print(f'  not ready: {type(exc).__name__}', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null; do
  if [ "$attempt" -ge 30 ]; then
    echo "Database unreachable after 30 attempts. Check MYSQL_HOST, the" >&2
    echo "credentials in .env, and that MySQL allows the Docker bridge." >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 2
done
echo "Database is up."

# Idempotent: a no-op when the schema is already current.
#
# Only the API applies migrations. The Celery worker and beat run from the
# same image and set RUN_MIGRATIONS=false, so three containers starting at
# once do not race each other over the same ALTER TABLE.
if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  echo "Applying migrations..."
  alembic upgrade head
else
  echo "Skipping migrations (RUN_MIGRATIONS=${RUN_MIGRATIONS})."
fi

echo "Starting: $*"
exec "$@"
