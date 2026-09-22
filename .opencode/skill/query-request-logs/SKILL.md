---
name: query-request-logs
description: Queries the synology-photos-proxy's request log in Loki on the live NAS deployment to inspect proxied requests (including failed Photo Request uploads). Use when asked to check failed uploads, error rates, recent traffic, or anything about this proxy's request logs/history.
---

# Querying the proxy's request log (Loki)

`app.py` logs every proxied request as one plain line via `logging.info`:

```
proxied method=POST path=/mo/request/REQ123/webapi/entry.cgi/SYNO.Foto.Upload.PhotoRequestItem status=200 client_ip=1.2.3.4 body={"error":{"code":101},"success":false}
```

The container's Docker logging driver (`driver: loki`, see
`docker-compose.deploy.yml`) ships that line to a Loki instance running on
the NAS (deployed from `loki/` in this repo). Loki indexes and stores it
with a 14-day retention (`limits_config.retention_period` in
`loki/loki-config.yaml`) - there is no database or logging code in the app
itself beyond that one log line. This is the only record of failed Photo
Request uploads - Synology Photos itself keeps none.

**`status` alone is not enough to detect a failed upload, and neither is
any fixed field list.** Confirmed against the live backend (real Playwright
session against a real Photo Request link, captured via the browser's
network tab):

- Successful upload: `{"data":{"action":"new","id":44468,"unit_id":44468},"success":true}`
- Rejected upload: `{"error":{"code":101},"success":false}`

Both over HTTP `200`. DSM's failure shapes are undocumented and
inconsistent, so `json_body_for_log()` in `app.py` logs the **entire**
JSON response body verbatim (`body=`, compact-reserialized, capped at
`MAX_LOGGED_BODY`) rather than extracting specific fields - an earlier
version of this only checked a `success` key and would have missed any
failure shape that didn't use it. `body=-` means the response wasn't
JSON. Note: some failures (e.g. an unsupported file extension) are
rejected by the browser's own client-side JS and never even reach the
server - no logging on the backend can catch those.

## Labels

The Docker Loki driver auto-attaches these labels (no app-side config
needed): `compose_service="synology-photos-proxy"`, `compose_project`,
`container_name`, `host`. Everything else (method/path/status/client_ip/
body) is plain text inside the log line, not a label - extract the fixed
fields with a LogQL `regexp` stage; search inside `body` with a plain
substring filter (`|=`), since its shape is unpredictable by design.

## Location

- NAS SSH alias: `synology-nas` (in `~/.ssh/config` - use as-is, never
  inline user/key/host/port).
- Loki's query API: `http://127.0.0.1:3100` - loopback-only on the NAS, so
  queries must run from the NAS itself (via `ssh synology-nas "curl ..."`)
  or through an SSH tunnel, not directly from this machine.

## Running a query

Fixed-field extraction (method/path/status/client_ip) plus a filter:

```bash
ssh synology-nas 'curl -s -G "http://127.0.0.1:3100/loki/api/v1/query_range" \
  --data-urlencode '"'"'query={compose_service="synology-photos-proxy"} | regexp `method=(?P<method>\S+) path=(?P<path>\S+) status=(?P<status>\d+) client_ip=(?P<client_ip>\S+)` <FILTER>'"'"' \
  --data-urlencode "limit=50" --data-urlencode "direction=backward"' | python3 -m json.tool
```

Common `<FILTER>` clauses (append after the `regexp` stage):

```logql
# HTTP-level failures, most recent first
| status >= 400

# Failed uploads only
| method = "POST" and status >= 400

# Everything from one client IP
| client_ip = "1.2.3.4"
```

**For DSM's HTTP-200-but-logically-failed responses, don't use `regexp` on
`body` - its shape is unpredictable.** Use a plain substring filter
instead, which works against the raw log line directly (no `regexp` stage
needed):

```bash
ssh synology-nas 'curl -s -G "http://127.0.0.1:3100/loki/api/v1/query_range" \
  --data-urlencode '"'"'query={compose_service="synology-photos-proxy"} |= "\"success\":false"'"'"' \
  --data-urlencode "limit=50"' | python3 -m json.tool
```

Both kinds of failure at once (HTTP-level or DSM-logical-level):

```bash
ssh synology-nas 'curl -s -G "http://127.0.0.1:3100/loki/api/v1/query_range" \
  --data-urlencode '"'"'query={compose_service="synology-photos-proxy"} |= "status=4" or "status=5" or "\"success\":false"'"'"' \
  --data-urlencode "limit=50"' | python3 -m json.tool
```

If you don't know what you're looking for yet, just grep the whole body
for a keyword (error message text, a filename, etc.) the same way:

```bash
ssh synology-nas 'curl -s -G "http://127.0.0.1:3100/loki/api/v1/query_range" \
  --data-urlencode '"'"'query={compose_service="synology-photos-proxy"} |= "<keyword>"'"'"' \
  --data-urlencode "limit=50"' | python3 -m json.tool
```

Add `--data-urlencode "start=<unix_nanoseconds>"` /
`--data-urlencode "end=<unix_nanoseconds>"` to narrow the time range;
without them Loki defaults to the last 1 hour, which is often too narrow
for "did this fail yesterday" questions.

## Sanity-checking the pipeline

```bash
# Is Loki up?
ssh synology-nas "curl -s http://127.0.0.1:3100/ready"

# Is the proxy container actually configured to ship logs to it?
ssh synology-nas "sudo /usr/local/bin/docker inspect synology-photos-proxy-synology-photos-proxy-1 --format '{{.HostConfig.LogConfig}}'"
```

If the container's `LogConfig.Type` isn't `loki`, it was started before the
logging driver was added to `docker-compose.yml` - it needs
`docker compose up -d` to recreate it (a plain restart does not change the
logging driver).
