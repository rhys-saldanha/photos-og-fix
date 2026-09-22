"""
Self-check for app.py's fix_og_tags(). No framework, no fixtures - just
asserts. Run with: python3 test_app.py
"""
import json
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup

import app as app_module
from app import GENERIC_REQUEST_TITLE, fix_og_tags


def og_content(html_bytes: bytes, property_name: str):
    """Parse out a given og:* meta tag's content, the same way a real
    crawler would - not by assuming a specific attribute serialization
    order, which is a parser implementation detail."""
    soup = BeautifulSoup(html_bytes, "html.parser")
    tag = soup.head.find("meta", attrs={"property": property_name})
    return tag["content"] if tag else None


def header(headers: dict, name: str):
    """Case-insensitive header lookup - HTTP header names are
    case-insensitive on the wire, so tests shouldn't assume a specific
    casing survived whatever normalization happened along the way."""
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v
    return None

PAGE_URL = "https://photos.example.com/mo/sharing/ABC123"

RELATIVE_IMAGE_HTML = b"""<!DOCTYPE html><html><head>
<title>My Album | Synology Photos</title>
<meta property="og:title" content="My Album | Synology Photos" />
<meta property="og:image" content="ABC123/cover.jpg" />
</head><body><div id="reactRoot"></div></body></html>"""

ALREADY_ABSOLUTE_HTML = b"""<!DOCTYPE html><html><head>
<title>Some Album</title>
<meta property="og:title" content="Some Album" />
<meta property="og:image" content="https://photos.example.com/mo/sharing/XYZ789/cover.jpg" />
<meta property="og:url" content="https://photos.example.com/mo/sharing/XYZ789" />
</head><body><div id="reactRoot"></div></body></html>"""

NO_HEAD_HTML = b"<html><body>broken page, no head at all</body></html>"

NO_OG_TAGS_HTML = b"""<!DOCTYPE html><html><head>
<title>Generic Photo Request Page</title>
</head><body><div id="reactRoot"></div></body></html>"""

REQUEST_PAGE_URL = "https://photos.example.com/mo/request/REQ123"

# Synology renders the generic title with a non-breaking space (U+00A0),
# not a regular one - the fixtures below must match that exactly, since
# that's precisely the mismatch that silently broke this in production.
GENERIC_REQUEST_HTML = f"""<!DOCTYPE html><html><head>
<title>Synology\xa0Photos</title>
<meta property="og:title" content="Synology\xa0Photos" />
<meta property="og:image" content="webman/3rdparty/SynologyPhotos/images/icon/photos_512.png" />
</head><body><div id="reactRoot"></div></body></html>""".encode("utf-8")

# Same generic page, but without a fixable relative og:image - used for the
# "subject lookup failed/skipped" tests, so a full-equality assertion tests
# only the title behavior and isn't muddied by the (correct, independent)
# image-fixing behavior also firing on GENERIC_REQUEST_HTML.
GENERIC_REQUEST_HTML_NO_IMAGE = f"""<!DOCTYPE html><html><head>
<title>Synology\xa0Photos</title>
<meta property="og:title" content="Synology\xa0Photos" />
</head><body><div id="reactRoot"></div></body></html>""".encode("utf-8")

NON_GENERIC_TITLE_REQUEST_HTML = b"""<!DOCTYPE html><html><head>
<title>Something Else Entirely</title>
<meta property="og:title" content="Something Else Entirely" />
</head><body><div id="reactRoot"></div></body></html>"""

NON_GENERIC_IMAGE_REQUEST_HTML = f"""<!DOCTYPE html><html><head>
<title>Synology\xa0Photos</title>
<meta property="og:title" content="Synology\xa0Photos" />
<meta property="og:image" content="https://photos.example.com/some/other/real-photo.jpg" />
</head><body><div id="reactRoot"></div></body></html>""".encode("utf-8")


def test_relative_image_becomes_absolute():
    fixed = fix_og_tags(RELATIVE_IMAGE_HTML, PAGE_URL)
    assert (
        og_content(fixed, "og:image")
        == "https://photos.example.com/mo/sharing/ABC123/cover.jpg"
    )


def test_og_url_added_once():
    fixed = fix_og_tags(RELATIVE_IMAGE_HTML, PAGE_URL)
    soup = BeautifulSoup(fixed, "html.parser")
    matches = soup.head.find_all("meta", attrs={"property": "og:url"})
    assert len(matches) == 1
    assert matches[0]["content"] == PAGE_URL


def test_already_correct_html_untouched():
    fixed = fix_og_tags(ALREADY_ABSOLUTE_HTML, PAGE_URL)
    assert fixed == ALREADY_ABSOLUTE_HTML
    soup = BeautifulSoup(fixed, "html.parser")
    assert len(soup.head.find_all("meta", attrs={"property": "og:url"})) == 1  # never duplicated


def test_missing_head_leaves_original_untouched():
    fixed = fix_og_tags(NO_HEAD_HTML, PAGE_URL)
    assert fixed == NO_HEAD_HTML


def test_no_og_tags_leaves_original_untouched():
    fixed = fix_og_tags(NO_OG_TAGS_HTML, PAGE_URL)
    assert fixed == NO_OG_TAGS_HTML


def test_request_page_title_replaced_with_real_subject():
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "success": True,
        "data": {"subject": "Karishma & Ankush Wedding"},
    }
    with patch("app.requests.post", return_value=mock_response) as mock_post:
        fixed = fix_og_tags(
            GENERIC_REQUEST_HTML, REQUEST_PAGE_URL, cookies={"sharing_sid": "abc123"}
        )

    mock_post.assert_called_once()
    soup = BeautifulSoup(fixed, "html.parser")
    assert soup.title.string == "Karishma & Ankush Wedding | Synology Photos"
    assert (
        og_content(fixed, "og:title") == "Karishma & Ankush Wedding | Synology Photos"
    )


def test_request_info_fetch_uses_the_undocumented_required_recipe():
    """Reverse-engineered against the real DSM backend: this API only
    works with (a) the /mo/request/webapi/... path prefix - the bare
    /webapi/... path fails, (b) an x-syno-sharing header naming the share
    ID, and (c) the sharing_sid cookie set by the initial page load. All
    three were confirmed missing/wrong independently via manual curl
    testing against the live backend before landing on this combination."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"success": True, "data": {"subject": "X"}}

    with patch("app.requests.post", return_value=mock_response) as mock_post:
        app_module.fetch_request_info("REQ123", cookies={"sharing_sid": "abc123"})

    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    call_url = mock_post.call_args[0][0]
    assert call_url.endswith("/mo/request/webapi/entry.cgi/SYNO.Foto.Sharing.Passphrase")
    assert kwargs["headers"]["x-syno-sharing"] == "REQ123"
    assert kwargs["cookies"] == {"sharing_sid": "abc123"}
    assert kwargs["data"]["passphrase"] == '"REQ123"'


def test_request_info_fetch_forwards_client_ip_headers():
    """DSM binds the sharing_sid session to the client IP it saw on the
    page's initial load (X-Real-IP/X-Forwarded-For) and rejects the
    info-fetch call with error 150 if the IP doesn't match - confirmed
    by reproducing that exact failure and fix against the live backend.
    So we must forward the SAME client-IP headers the initial request
    carried, not just the cookie."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"success": True, "data": {"subject": "X"}}

    client_headers = {"X-Real-IP": "192.168.1.1", "X-Forwarded-For": "192.168.1.1"}
    with patch("app.requests.post", return_value=mock_response) as mock_post:
        app_module.fetch_request_info(
            "REQ123", cookies={"sharing_sid": "abc123"}, extra_headers=client_headers
        )

    kwargs = mock_post.call_args.kwargs
    assert kwargs["headers"]["X-Real-IP"] == "192.168.1.1"
    assert kwargs["headers"]["X-Forwarded-For"] == "192.168.1.1"
    assert kwargs["headers"]["x-syno-sharing"] == "REQ123"  # still present alongside


def _assert_generic_title_preserved(fixed: bytes):
    """Shared assertion for the "title lookup didn't succeed" tests. Only
    checks the title-related tags - og:url/og:image are separate, correctly
    independent behaviors and shouldn't be conflated with this check."""
    soup = BeautifulSoup(fixed, "html.parser")
    assert soup.title.string == GENERIC_REQUEST_TITLE
    assert og_content(fixed, "og:title") == GENERIC_REQUEST_TITLE


def test_request_page_title_untouched_when_api_call_fails():
    with patch("app.requests.post", side_effect=ConnectionError("boom")):
        fixed = fix_og_tags(GENERIC_REQUEST_HTML_NO_IMAGE, REQUEST_PAGE_URL)
    _assert_generic_title_preserved(fixed)


def test_request_page_title_untouched_when_api_reports_failure():
    mock_response = MagicMock()
    mock_response.json.return_value = {"success": False}
    with patch("app.requests.post", return_value=mock_response):
        fixed = fix_og_tags(GENERIC_REQUEST_HTML_NO_IMAGE, REQUEST_PAGE_URL)
    _assert_generic_title_preserved(fixed)


def test_request_page_title_untouched_when_subject_missing():
    mock_response = MagicMock()
    mock_response.json.return_value = {"success": True, "data": {}}
    with patch("app.requests.post", return_value=mock_response):
        fixed = fix_og_tags(GENERIC_REQUEST_HTML_NO_IMAGE, REQUEST_PAGE_URL)
    _assert_generic_title_preserved(fixed)


def test_request_page_title_never_overwrites_non_generic_title():
    """Defensive: only ever replace the exact known generic title. If a
    future DSM version puts something else there, don't touch it - we
    don't understand it well enough to safely overwrite it."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "success": True,
        "data": {"subject": "Some Subject"},
    }
    with patch("app.requests.post", return_value=mock_response):
        fixed = fix_og_tags(NON_GENERIC_TITLE_REQUEST_HTML, REQUEST_PAGE_URL)
    soup = BeautifulSoup(fixed, "html.parser")
    assert soup.title.string == "Something Else Entirely"
    assert og_content(fixed, "og:title") == "Something Else Entirely"


def test_extract_album_share_id_parses_quickconnect_link():
    assert (
        app_module.extract_album_share_id(
            "https://rhys-saldanha.quickconnect.to/mo/sharing/KrYBkLIsS"
        )
        == "KrYBkLIsS"
    )


def test_extract_album_share_id_rejects_malformed_links():
    assert app_module.extract_album_share_id("") is None
    assert app_module.extract_album_share_id("not-a-link") is None
    assert app_module.extract_album_share_id("https://example.com/mo/sharing/") is None


def test_request_page_og_image_swapped_to_album_cover():
    """When the request's album_sharing_link is present, the generic icon in
    og:image gets replaced with <visitor-base>/mo/sharing/<album_id>/cover.jpg
    - the album's real cover photo, built from the visitor's own base URL
    (never the quickconnect.to host that appears in the API payload)."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "success": True,
        "data": {
            "subject": "Karishma & Ankush Wedding",
            "album_sharing_link": "https://rhys-saldanha.quickconnect.to/mo/sharing/KrYBkLIsS",
        },
    }
    with patch("app.requests.post", return_value=mock_response):
        fixed = fix_og_tags(GENERIC_REQUEST_HTML, REQUEST_PAGE_URL)
    assert (
        og_content(fixed, "og:image") == "https://photos.example.com/mo/sharing/KrYBkLIsS/cover.jpg"
    )


def test_request_page_without_album_keeps_generic_icon():
    """Not every Photo Request uploads to an album - without an
    album_sharing_link, leave the (absolutized) generic icon alone rather
    than guess at a cover URL."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"success": True, "data": {"subject": "Some Subject"}}
    with patch("app.requests.post", return_value=mock_response):
        fixed = fix_og_tags(GENERIC_REQUEST_HTML, REQUEST_PAGE_URL)
    assert (
        og_content(fixed, "og:image")
        == "https://photos.example.com/mo/request/webman/3rdparty/SynologyPhotos/images/icon/photos_512.png"
    )


def test_request_page_real_image_never_replaced():
    """The cover swap must only fire for the exact known generic icon - a
    page carrying a genuine custom og:image keeps it, even when the request
    has an album."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "success": True,
        "data": {
            "subject": "W",
            "album_sharing_link": "https://rhys-saldanha.quickconnect.to/mo/sharing/KrYBkLIsS",
        },
    }
    with patch("app.requests.post", return_value=mock_response):
        fixed = fix_og_tags(NON_GENERIC_IMAGE_REQUEST_HTML, REQUEST_PAGE_URL)
    assert og_content(fixed, "og:image") == "https://photos.example.com/some/other/real-photo.jpg"


def test_sharing_pages_never_trigger_request_subject_lookup():
    """/mo/sharing/ pages already carry correct title data - the extra
    API call is only relevant for /mo/request/ pages."""
    with patch("app.requests.post") as mock_post:
        fix_og_tags(RELATIVE_IMAGE_HTML, PAGE_URL)
    mock_post.assert_not_called()


def test_proxy_forwards_sharing_sid_cookie_from_initial_response():
    """The critical wiring: the sharing_sid cookie comes from the SAME
    initial backend response we're already fixing the HTML of - it must
    be extracted from there and threaded through to the subject-fetch
    call, not fetched separately."""
    client = app_module.app.test_client()

    initial_resp = MagicMock()
    initial_resp.status_code = 200
    initial_resp.headers = {"Content-Type": "text/html"}
    initial_resp.content = GENERIC_REQUEST_HTML_NO_IMAGE
    initial_resp.cookies.get_dict.return_value = {"sharing_sid": "real-cookie-value"}

    subject_resp = MagicMock()
    subject_resp.json.return_value = {"success": True, "data": {"subject": "Real Subject"}}

    with patch("app.requests.request", return_value=initial_resp):
        with patch("app.requests.post", return_value=subject_resp) as mock_post:
            resp = client.get(
                "/mo/request/REQ123",
                headers={"X-Real-IP": "203.0.113.5", "X-Forwarded-For": "203.0.113.5"},
            )

    assert mock_post.call_args.kwargs["cookies"] == {"sharing_sid": "real-cookie-value"}
    # the same client-IP headers the initial request carried must also
    # reach the subject-fetch call, or DSM rejects it (error 150).
    sent_headers = mock_post.call_args.kwargs["headers"]
    assert header(sent_headers, "X-Real-IP") == "203.0.113.5"
    assert header(sent_headers, "X-Forwarded-For") == "203.0.113.5"
    soup = BeautifulSoup(resp.data, "html.parser")
    assert soup.title.string == "Real Subject | Synology Photos"


def test_proxy_trusts_forwarded_proto_and_host():
    """DSM's reverse proxy terminates TLS and forwards to us over plain
    HTTP with X-Forwarded-Proto/X-Forwarded-Host set (confirmed against
    the real nginx config). Without trusting those headers, absolutized
    URLs would incorrectly come out as http://<internal-hostname>/... ."""
    client = app_module.app.test_client()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "text/html"}
    mock_resp.content = RELATIVE_IMAGE_HTML

    with patch("app.requests.request", return_value=mock_resp):
        resp = client.get(
            "/mo/sharing/ABC123",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "photos.example.com",
            },
        )

    assert (
        og_content(resp.data, "og:image")
        == "https://photos.example.com/mo/sharing/ABC123/cover.jpg"
    )


def test_proxy_logs_http_level_upload_failure():
    """A real HTTP-level failure (e.g. a reverse-proxy size-limit reject)
    must show up in the log with its actual status code."""
    client = app_module.app.test_client()
    mock_resp = MagicMock()
    mock_resp.status_code = 413
    mock_resp.headers = {"Content-Type": "text/html"}
    mock_resp.content = b"<html>413 Request Entity Too Large</html>"

    with patch("app.requests.request", return_value=mock_resp):
        with patch("app.logging.info") as mock_log:
            client.post("/mo/request/webapi/entry.cgi/upload", data=b"filedata")

    mock_log.assert_called_once_with(
        "proxied method=%s path=%s status=%s client_ip=%s body=%s",
        "POST", "/mo/request/webapi/entry.cgi/upload", 413, mock_log.call_args.args[4], "-",
    )


def test_proxy_logs_and_returns_504_on_upstream_timeout():
    """A timeout/connection failure to the backend must not propagate as
    an unhandled exception (which would 500 with zero trace in our log) -
    it must be caught, logged (status=504, so it's visible to the same
    "status >= 400" queries as any other failure), and turned into a real
    504 response. This is the previously-invisible failure mode for large
    uploads: see the REQUEST_TIMEOUT comment in app.py."""
    client = app_module.app.test_client()

    with patch(
        "app.requests.request",
        side_effect=app_module.requests.exceptions.ReadTimeout("timed out"),
    ):
        with patch("app.logging.info") as mock_log:
            resp = client.post("/mo/request/webapi/entry.cgi/upload", data=b"filedata")

    assert resp.status_code == 504
    mock_log.assert_called_once_with(
        "proxied method=%s path=%s status=%s client_ip=%s body=%s",
        "POST", "/mo/request/webapi/entry.cgi/upload", 504, mock_log.call_args.args[4],
        "proxy_error:timed out",
    )


def test_proxy_logs_full_json_body_of_real_captured_dsm_failure():
    """Confirmed against the live backend: DSM returns HTTP 200 even for a
    genuine failure (sent a real upload request missing the file field to
    the real SYNO.Foto.Upload.PhotoRequestItem endpoint). The whole body
    is logged verbatim, not picked apart into specific fields - DSM's
    failure shapes are undocumented, so status=200 must not be mistaken
    for success, and the exact structure of a failure isn't assumed."""
    client = app_module.app.test_client()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "application/json"}
    body = {"error": {"code": 101}, "success": False}
    mock_resp.content = json.dumps(body).encode()
    mock_resp.json.return_value = body

    with patch("app.requests.request", return_value=mock_resp):
        with patch("app.logging.info") as mock_log:
            client.post(
                "/mo/request/webapi/entry.cgi/SYNO.Foto.Upload.PhotoRequestItem",
                data=b"name=missing-file-field.jpg",
            )

    mock_log.assert_called_once_with(
        "proxied method=%s path=%s status=%s client_ip=%s body=%s",
        "POST",
        "/mo/request/webapi/entry.cgi/SYNO.Foto.Upload.PhotoRequestItem",
        200,
        mock_log.call_args.args[4],
        '{"error":{"code":101},"success":false}',
    )


def test_proxy_logs_full_json_body_of_real_captured_dsm_success():
    """Same real endpoint, but the actual success shape captured from a
    live (test) upload - included so the log format is exercised against
    both real outcomes, not just the failure one."""
    client = app_module.app.test_client()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "application/json"}
    body = {"data": {"action": "new", "id": 44468, "unit_id": 44468}, "success": True}
    mock_resp.content = json.dumps(body).encode()
    mock_resp.json.return_value = body

    with patch("app.requests.request", return_value=mock_resp):
        with patch("app.logging.info") as mock_log:
            client.post("/mo/request/webapi/entry.cgi/SYNO.Foto.Upload.PhotoRequestItem")

    logged_body = mock_log.call_args.args[5]
    assert logged_body == '{"data":{"action":"new","id":44468,"unit_id":44468},"success":true}'


def test_json_body_for_log_ignores_non_json_and_malformed_bodies():
    html_resp = MagicMock()
    assert app_module.json_body_for_log(html_resp, "text/html") == "-"

    bad_json_resp = MagicMock()
    bad_json_resp.json.side_effect = ValueError("not json")
    assert app_module.json_body_for_log(bad_json_resp, "application/json") == "-"


def test_json_body_for_log_truncates_oversized_bodies():
    huge_resp = MagicMock()
    huge_resp.json.return_value = {"items": ["x"] * 2000}
    logged = app_module.json_body_for_log(huge_resp, "application/json")
    assert logged.endswith("...TRUNCATED")
    assert len(logged) == app_module.MAX_LOGGED_BODY + len("...TRUNCATED")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} checks passed")
