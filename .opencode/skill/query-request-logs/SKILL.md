---
name: query-request-logs
description: Queries the synology-photos-proxy's SQLite request log on the live NAS deployment to inspect proxied requests (including failed Photo Request uploads). Use when asked to check failed uploads, error rates, recent traffic, or anything about this proxy's request logs/history.
---

# Querying the proxy's request log

`app.py` logs every proxied request (method, path, status code, client IP,
timestamp) to a SQLite database, pruning rows older than 14 days on every
write (see `RETENTION_DAYS` in `app.py`). This is the only place failed
Photo Request uploads are ever recorded - Synology Photos itself keeps no
log of upload failures.

## Schema

```sql
CREATE TABLE requests (
    ts TEXT NOT NULL,        -- ISO 8601 UTC, e.g. 2026-09-22T20:37:41.487240+00:00
    method TEXT NOT NULL,    -- GET / POST / HEAD
    path TEXT NOT NULL,
    status INTEGER NOT NULL, -- HTTP status the backend returned
    client_ip TEXT
);
```

No request/response bodies are stored - only the outcome. A failed upload is
a `POST` row with `status >= 400`.

## Location

- NAS SSH alias: `synology-nas` (in `~/.ssh/config` - use it as-is, never
  inline user/key/host/port).
- Container name: `synology-photos-proxy-synology-photos-proxy-1`
- DB path inside the container: `/data/requests.db`

## Running a query

The container image (`python:3.13-slim`) has no `sqlite3` CLI binary -
only Python's stdlib `sqlite3` module. Query through `python3 -c`:

```bash
ssh synology-nas "sudo /usr/local/bin/docker exec synology-photos-proxy-synology-photos-proxy-1 python3 -c \"
import sqlite3
for row in sqlite3.connect('/data/requests.db').execute('''<SQL HERE>'''):
    print(row)
\""
```

Common queries (substitute into `<SQL HERE>` above):

```sql
-- Failed requests (any method), most recent first
SELECT ts, method, path, status, client_ip FROM requests WHERE status >= 400 ORDER BY ts DESC

-- Failed uploads only
SELECT ts, path, status, client_ip FROM requests WHERE method = 'POST' AND status >= 400 ORDER BY ts DESC

-- Everything from one client IP
SELECT ts, method, path, status FROM requests WHERE client_ip = '1.2.3.4' ORDER BY ts

-- Status code breakdown for the last 14 days (all that's retained)
SELECT status, COUNT(*) FROM requests GROUP BY status ORDER BY COUNT(*) DESC

-- Traffic in the last 24 hours
SELECT ts, method, path, status, client_ip FROM requests
WHERE ts >= strftime('%Y-%m-%dT%H:%M:%f', 'now', '-1 day') ORDER BY ts DESC
```

If the exec fails, check `ssh synology-nas "sudo /usr/local/bin/docker ps"`
for the current container name first - it may have changed after a
redeploy.
