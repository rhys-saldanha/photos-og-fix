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
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, request
from werkzeug.middleware.proxy_fix import ProxyFix

BACKEND = "http://127.0.0.1:5000"
REQUEST_TIMEOUT = 15
MIN_SIZE_RATIO = 0.5  # modified output must be at least this fraction of the original size

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


def validate_html(soup: BeautifulSoup, expect_og_url: bool, original_len: int) -> None:
    """Sanity-check the modified document before it's allowed out the door.
    Raises on anything that looks like corruption; callers must treat any
    exception as "discard the modification"."""
    if soup.html is None or soup.head is None or soup.body is None:
        raise ValueError("missing html/head/body after modification")
    if expect_og_url and soup.head.find("meta", attrs={"property": "og:url"}) is None:
        raise ValueError("og:url missing after insertion")
    rendered_len = len(str(soup))
    if rendered_len < original_len * MIN_SIZE_RATIO:
        raise ValueError(
            f"output suspiciously small ({rendered_len} bytes vs {original_len} original)"
        )


def fix_og_tags(html_bytes: bytes, page_url: str) -> bytes:
    """Top-level entry point. Never raises - on any failure, returns the
    original bytes unchanged and logs why."""
    try:
        html = html_bytes.decode("utf-8")
        soup = BeautifulSoup(html, "html.parser")

        image_changed = absolutize_og_image(soup, page_url)
        url_added = add_og_url_if_missing(soup, page_url)

        if not (image_changed or url_added):
            return html_bytes

        validate_html(soup, expect_og_url=url_added, original_len=len(html))
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
        body = fix_og_tags(body, request.url)

    response_headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP]
    return Response(body, status=resp.status_code, headers=response_headers)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8181, threaded=True)
