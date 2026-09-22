# synology-photos-proxy

A reverse proxy that sits in front of Synology Photos and fixes broken Open
Graph metadata, so link previews actually work in WhatsApp, iMessage, Slack,
Telegram, etc.

## What's broken in Synology Photos, and what this fixes

Synology Photos generates share links (`/mo/sharing/<id>`) and photo request
links (`/mo/request/<id>`), but the HTML it serves has several bugs that
break link previews:

1. **`og:image` is a relative URL** (e.g. `content="ABC123/cover.jpg"`)
   instead of an absolute one. This violates the Open Graph spec, and most
   preview crawlers refuse to resolve it, so no thumbnail ever shows.
2. **No `og:url` tag** is present at all.
3. **Photo Request pages never show the real subject.** Every request link
   renders a generic `<title>Synology Photos</title>` and the generic app
   icon, regardless of what the request is actually for - the real subject
   only appears client-side, after JavaScript calls an API the static page
   never uses to update its own title/meta tags.

This proxy transparently forwards every request to the real Synology Photos
backend, and only when a response is HTML does it:

- Rewrite `og:image` to an absolute URL, if it's relative.
- Add `og:url` pointing at the canonical link, if missing.
- For Photo Request pages, look up the real subject via the same
  (undocumented, unauthenticated) API the page's own JavaScript uses, and
  replace the generic title/`og:title` with it.

Every fix follows the same discipline: **search for the tag, assert it looks
exactly like the known-broken case, validate the result is still
well-formed, and apply it - if any of that fails for any reason, the
original response is returned untouched and the error is logged.** This
proxy should never be the reason a page breaks; at worst it fails to improve
a preview.

## Recreating this deployment

This assumes a Synology NAS with Container Manager (Docker) installed, and
DSM's Login Portal / Reverse Proxy features.

### 1. Point a hostname at Synology Photos, with a real cert

Synology Photos needs its own domain, bound via **Control Panel > Login
Portal > Applications > Synology Photos > Edit > Customized domain**. Get a
Let's Encrypt certificate for that domain through DSM's own certificate
manager (**Control Panel > Security > Certificate**) - this only works if
the domain is publicly resolvable and port 80 reaches the NAS for the
HTTP-01 challenge, and DSM will auto-renew it forever.

### 2. Remove the domain binding, use a Reverse Proxy rule instead

The Application Portal binding from step 1 routes the domain straight to
Synology Photos - there's no way to interpose a proxy in front of it while
it's active. Remove the "Customized domain" from that Photos settings page,
which frees the domain up.

Then create a manual rule under **Control Panel > Login Portal > Advanced >
Reverse Proxy**:

| | |
|---|---|
| Source | `https://<your-domain>:443` |
| Destination | `http://localhost:8181` |

Re-assign the certificate from step 1 to this new Reverse Proxy service
entry (**Control Panel > Security > Certificate > Settings**) - DSM treats
each reverse-proxied hostname as its own cert-assignable service.

### 3. Deploy Loki (for request logging)

Install the Loki Docker logging driver plugin on the NAS host once (a
Docker daemon-level change, not a container):

```bash
docker plugin install grafana/loki-docker-driver:3.7.8-amd64 --alias loki --grant-all-permissions
```

Then deploy Loki itself, e.g. under `/volume1/docker/loki/` using the
`loki/` directory in this repo (`docker-compose.yml` + `loki-config.yaml`,
retention set to 14 days):

```bash
docker compose up -d
```

It listens on `127.0.0.1:3100` only - not exposed beyond the NAS itself.

### 4. Deploy the proxy container

On the NAS, e.g. under `/volume1/docker/synology-photos-proxy/`:

```yaml
# docker-compose.yml
services:
  synology-photos-proxy:
    image: ghcr.io/rhys-saldanha/synology-photos-proxy:latest
    # host networking: this proxy talks to DSM's own Photos backend at
    # 127.0.0.1:5000 on the NAS itself, which only works if the container
    # shares the host's network namespace rather than a bridge network.
    network_mode: host
    restart: unless-stopped
    logging:
      driver: loki
      options:
        loki-url: "http://127.0.0.1:3100/loki/api/v1/push"
```

```bash
docker compose up -d
```

The container listens on `127.0.0.1:8181`, matching the Reverse Proxy rule
from step 2.

### 5. Verify

```bash
curl -s https://<your-domain>/mo/sharing/<some-share-id> | grep og:image
```

`og:image` should be a full `https://...` URL, not a relative path.

## Request logging

The app logs every proxied request as a plain `logging.info` line: method,
path, status code, client IP, and (for JSON API responses) DSM's own
`success`/`error` envelope as `api_success`/`api_error_code`. The Docker
Loki logging driver attached to the container (see step 3/4 above) ships
that line to Loki, which indexes and stores it with a 14-day retention -
no logging code lives in the app beyond that one line.

Synology Photos itself doesn't log Photo Request upload failures anywhere,
so this is the only place to see them - and the `api_success`/
`api_error_code` fields matter here: confirmed against the live backend,
DSM often returns HTTP **200** even for a logical failure (body
`{"success": false, "error": {"code": ...}}`), so the HTTP status alone
misses these. A failed upload is either a non-2xx `status`, or a 2xx
`status` with `api_success=False`.

```bash
curl -s -G "http://127.0.0.1:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={compose_service="synology-photos-proxy"} |= "status=4" or "status=5" or "api_success=False"' \
  --data-urlencode 'limit=50' | python3 -m json.tool
```

See `.opencode/skill/query-request-logs/` for the full set of example
LogQL queries.

## Redeploying after a change

Auto-updates: the Watchtower instance already running as part of the
reciplease deployment (`/volume1/docker/reciplease`) polls every 5 minutes
and updates any container labelled `com.centurylinklabs.watchtower.enable=true`
- both this proxy and Loki carry that label, so a new image pushed by CI is
picked up within 5 minutes with no manual step.

To force it immediately instead of waiting:

```bash
docker compose pull && docker compose up -d
```

## Development

```bash
pip install -r requirements.txt
python test_app.py
```

No test framework - just a script of plain asserts, run directly.
