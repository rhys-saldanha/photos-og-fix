"""
Reverse proxy in front of Synology Photos that fixes broken Open Graph tags
(known Synology bug: og:image is emitted as a relative path, which most link
preview crawlers - WhatsApp, iMessage, Slack - refuse to resolve).

Design: every fix is "search -> assert -> validate -> apply, else skip and
log". If anything about the page doesn't match what we expect, the original,
untouched response is returned. This proxy should never be the reason a page
breaks - at worst it just fails to improve the preview.
"""
import logging
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, request
from werkzeug.middleware.proxy_fix import ProxyFix

BACKEND = "http://127.0.0.1:5000"
REQUEST_TIMEOUT = 15
MIN_SIZE_RATIO = 0.5  # modified output must be at least this fraction of the original size

# Photo Request pages (as opposed to album sharing pages) always render a
# generic title/icon regardless of the request's actual subject - but the
# real subject is available via an unauthenticated API call keyed by the
# share ID in the URL, since these pages are meant to be used by people
# with no DSM account at all.
REQUEST_PATH_RE = re.compile(r"^/mo/request/([^/]+)/?$")
GENERIC_REQUEST_TITLE = "Synology Photos"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

app = Flask(__name__)
# DSM's reverse proxy terminates TLS and forwards to us over plain HTTP,
# setting X-Forwarded-Proto/X-Forwarded-Host (confirmed against its actual
# nginx config). Without this, request.url would report our internal
# http://localhost:8181 address instead of the real https:// URL a visitor
# used - which is exactly the bug we're here to fix, just relocated.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

_HOP_BY_HOP = {"content-length", "transfer-encoding", "content-encoding", "connection"}


def absolutize_og_image(soup: BeautifulSoup, page_url: str) -> bool:
    """Search for og:image; if its content is a relative URL, rewrite it
    in place to an absolute one. Returns True if a change was made."""
    head = soup.head
    if head is None:
        raise ValueError("no <head> element")

    og_image = head.find("meta", attrs={"property": "og:image"})
    if og_image is None or not og_image.get("content"):
        return False

    content = og_image["content"]
    if re.match(r"^https?://", content):
        return False  # already absolute, nothing to fix

    og_image["content"] = urljoin(page_url, content)
    return True


def add_og_url_if_missing(soup: BeautifulSoup, page_url: str) -> bool:
    """Add an og:url tag pointing at the canonical share URL, but only if
    one doesn't already exist (assert-before-add, never duplicate) and only
    if this page actually carries real og:title data (i.e. it's a page
    worth annotating, not the generic app shell)."""
    head = soup.head
    if head is None:
        raise ValueError("no <head> element")

    if head.find("meta", attrs={"property": "og:url"}) is not None:
        return False  # already present, do not duplicate

    og_title = head.find("meta", attrs={"property": "og:title"})
    if og_title is None:
        return False  # nothing to anchor the insertion to

    new_tag = soup.new_tag("meta", attrs={"property": "og:url", "content": page_url})
    og_title.insert_after(new_tag)
    return True


def fetch_request_subject(
    request_id: str, cookies: dict | None, extra_headers: dict | None = None
) -> str | None:
    """Look up the real subject of a Photo Request by its share ID.

    Reverse-engineered against the live DSM backend (not documented
    anywhere): this call only succeeds with all of:
      - the /mo/request/webapi/... path prefix (bare /webapi/... fails)
      - an X-Syno-Sharing header naming the share ID
      - the sharing_sid cookie set by the initial page load
      - the SAME client-IP headers (X-Real-IP/X-Forwarded-For) that the
        initial page load carried - DSM binds the session to that IP and
        rejects a mismatch with error 150, confirmed by reproducing that
        exact failure and fix against the live backend
    Missing any one of these makes DSM return success=false. Returns None
    on any failure - callers must treat None as "leave it alone"."""
    try:
        headers = {"x-syno-sharing": request_id, **(extra_headers or {})}
        resp = requests.post(
            f"{BACKEND}/mo/request/webapi/entry.cgi/SYNO.Foto.Sharing.Passphrase",
            headers=headers,
            cookies=cookies,
            data={
                "api": "SYNO.Foto.Sharing.Passphrase",
                "method": "get_photo_request_info",
                "version": "1",
                "passphrase": f'"{request_id}"',
            },
            timeout=REQUEST_TIMEOUT,
        )
        payload = resp.json()
        if not payload.get("success"):
            logging.info(
                "subject lookup for %s returned: %s (headers sent=%s, cookies sent=%s)",
                request_id,
                payload,
                headers,
                cookies,
            )
            return None
        return payload.get("data", {}).get("subject") or None
    except Exception:
        logging.exception("failed to fetch photo request subject for %s", request_id)
        return None


def replace_generic_request_title(soup: BeautifulSoup, subject: str) -> bool:
    """Replace the generic title/og:title with the request's real subject,
    but only if both are EXACTLY the known generic value first (assert
    before overwrite) - never touch a title we don't recognize."""
    head = soup.head
    if head is None:
        raise ValueError("no <head> element")

    title_tag = head.find("title")
    og_title = head.find("meta", attrs={"property": "og:title"})

    if title_tag is None or title_tag.string != GENERIC_REQUEST_TITLE:
        return False
    if og_title is None or og_title.get("content") != GENERIC_REQUEST_TITLE:
        return False

    new_title = f"{subject} | Synology Photos"
    title_tag.string = new_title
    og_title["content"] = new_title
    return True


def validate_html(
    soup: BeautifulSoup, expect_og_url: bool, original_len: int, expect_title: str | None = None
) -> None:
    """Sanity-check the modified document before it's allowed out the door.
    Raises on anything that looks like corruption; callers must treat any
    exception as "discard the modification"."""
    if soup.html is None or soup.head is None or soup.body is None:
        raise ValueError("missing html/head/body after modification")
    if expect_og_url and soup.head.find("meta", attrs={"property": "og:url"}) is None:
        raise ValueError("og:url missing after insertion")
    if expect_title is not None:
        if soup.title is None or soup.title.string != expect_title:
            raise ValueError("title was not updated as expected")
        og_title = soup.head.find("meta", attrs={"property": "og:title"})
        if og_title is None or og_title.get("content") != expect_title:
            raise ValueError("og:title was not updated as expected")
    rendered_len = len(str(soup))
    if rendered_len < original_len * MIN_SIZE_RATIO:
        raise ValueError(
            f"output suspiciously small ({rendered_len} bytes vs {original_len} original)"
        )


def fix_og_tags(
    html_bytes: bytes,
    page_url: str,
    cookies: dict | None = None,
    extra_headers: dict | None = None,
) -> bytes:
    """Top-level entry point. Never raises - on any failure, returns the
    original bytes unchanged and logs why."""
    try:
        html = html_bytes.decode("utf-8")
        soup = BeautifulSoup(html, "html.parser")

        image_changed = absolutize_og_image(soup, page_url)
        url_added = add_og_url_if_missing(soup, page_url)

        new_title = None
        parsed_path = urlparse(page_url).path
        request_match = REQUEST_PATH_RE.match(parsed_path)
        logging.info("path=%r matched=%s cookies=%s", parsed_path, bool(request_match), cookies)
        title_changed = False
        if request_match:
            subject = fetch_request_subject(request_match.group(1), cookies, extra_headers)
            if subject:
                new_title = f"{subject} | Synology Photos"
                title_changed = replace_generic_request_title(soup, subject)

        if not (image_changed or url_added or title_changed):
            return html_bytes

        validate_html(
            soup,
            expect_og_url=url_added,
            original_len=len(html),
            expect_title=new_title if title_changed else None,
        )
        return str(soup).encode("utf-8")

    except Exception:
        logging.exception("og-fix failed for %s, returning original response", page_url)
        return html_bytes


@app.route("/", defaults={"path": ""}, methods=["GET", "HEAD", "POST"])
@app.route("/<path:path>", methods=["GET", "HEAD", "POST"])
def proxy(path):
    upstream_url = f"{BACKEND}/{path}"
    forward_headers = {k: v for k, v in request.headers if k.lower() != "host"}

    resp = requests.request(
        request.method,
        upstream_url,
        params=request.args,
        headers=forward_headers,
        data=request.get_data(),
        timeout=REQUEST_TIMEOUT,
    )

    body = resp.content
    content_type = resp.headers.get("Content-Type", "")
    if request.method == "GET" and "text/html" in content_type:
        # DSM's synofoto backend binds Photo Request sessions to the
        # client IP it saw - any follow-up call (see fetch_request_subject)
        # must present the exact same X-Real-IP/X-Forwarded-For.
        client_ip_headers = {
            k: v for k, v in forward_headers.items() if k.lower() in ("x-real-ip", "x-forwarded-for")
        }
        body = fix_og_tags(
            body, request.url, cookies=resp.cookies.get_dict(), extra_headers=client_ip_headers
        )

    response_headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP]
    return Response(body, status=resp.status_code, headers=response_headers)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8181, threaded=True)
