---
name: query-request-logs
description: Queries the synology-photos-proxy's request log in Loki on the live NAS deployment to inspect proxied requests (including failed Photo Request uploads). Use when asked to check failed uploads, error rates, recent traffic, or anything about this proxy's request logs/history.
---

# Querying the proxy's request log (Loki)

`app.py` logs every proxied request as one plain line via `logging.info`:

```
proxied method=POST path=/mo/request/REQ123/upload status=413 client_ip=1.2.3.4 api_success=- api_error_code=-
```

The container's Docker logging driver (`driver: loki`, see
`docker-compose.deploy.yml`) ships that line to a Loki instance running on
the NAS (deployed from `loki/` in this repo). Loki indexes and stores it
with a 14-day retention (`limits_config.retention_period` in
`loki/loki-config.yaml`) - there is no database or logging code in the app
itself beyond that one log line. This is the only record of failed Photo
Request uploads - Synology Photos itself keeps none.

**`status` alone is not enough to detect a failed upload.** Confirmed
against the live backend: DSM often returns HTTP `200` even for a logical
failure, wrapping it in a JSON body like
`{"success": false, "error": {"code": 101}}`. `api_outcome()` in `app.py`
reads that envelope on any `application/json` response and logs it as
`api_success`/`api_error_code`; both are `-` when the response isn't that
envelope (HTML, images, non-DSM JSON, etc.). A failed upload is therefore
either `status >= 400`, **or** `api_success=False` regardless of status.

## Labels

The Docker Loki driver auto-attaches these labels (no app-side config
needed): `compose_service="synology-photos-proxy"`, `compose_project`,
`container_name`, `host`. Everything else (method/path/status/client_ip)
is plain text inside the log line, not a label - extract it with a LogQL
`regexp` stage.

## Location

- NAS SSH alias: `synology-nas` (in `~/.ssh/config` - use as-is, never
  inline user/key/host/port).
- Loki's query API: `http://127.0.0.1:3100` - loopback-only on the NAS, so
  queries must run from the NAS itself (via `ssh synology-nas "curl ..."`)
  or through an SSH tunnel, not directly from this machine.

## Running a query

```bash
ssh synology-nas 'curl -s -G "http://127.0.0.1:3100/loki/api/v1/query_range" \
  --data-urlencode '"'"'query={compose_service="synology-photos-proxy"} | regexp `method=(?P<method>\S+) path=(?P<path>\S+) status=(?P<status>\d+) client_ip=(?P<client_ip>\S+) api_success=(?P<api_success>\S+) api_error_code=(?P<api_error_code>\S+)` <FILTER>'"'"' \
  --data-urlencode "limit=50" --data-urlencode "direction=backward"' | python3 -m json.tool
```

Common `<FILTER>` clauses (append after the `regexp` stage):

```logql
# All failed requests, HTTP-level and DSM-API-level, most recent first
| status >= 400 or api_success = "False"

# Failed uploads only
| method = "POST" and (status >= 400 or api_success = "False")

# HTTP-level failures only (network/reverse-proxy/backend rejects)
| status >= 400

# DSM logical failures that still returned HTTP 200 (see caveat above)
| api_success = "False"

# Everything from one client IP
| client_ip = "1.2.3.4"
```

Simpler substring-only queries (no field filtering, just grep-style) work
without the `regexp` stage, e.g. either kind of failure:

```bash
ssh synology-nas 'curl -s -G "http://127.0.0.1:3100/loki/api/v1/query_range" \
  --data-urlencode '"'"'query={compose_service="synology-photos-proxy"} |= "status=4" or "status=5" or "api_success=False"'"'"' \
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
