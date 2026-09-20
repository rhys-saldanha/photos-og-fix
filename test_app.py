"""
Self-check for app.py's fix_og_tags(). No framework, no fixtures - just
asserts. Run with: python3 test_app.py
"""
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

GENERIC_REQUEST_HTML = b"""<!DOCTYPE html><html><head>
<title>Synology Photos</title>
<meta property="og:title" content="Synology Photos" />
<meta property="og:image" content="webman/3rdparty/SynologyPhotos/images/icon/photos_512.png" />
</head><body><div id="reactRoot"></div></body></html>"""

# Same generic page, but without a fixable relative og:image - used for the
# "subject lookup failed/skipped" tests, so a full-equality assertion tests
# only the title behavior and isn't muddied by the (correct, independent)
# image-fixing behavior also firing on GENERIC_REQUEST_HTML.
GENERIC_REQUEST_HTML_NO_IMAGE = b"""<!DOCTYPE html><html><head>
<title>Synology Photos</title>
<meta property="og:title" content="Synology Photos" />
</head><body><div id="reactRoot"></div></body></html>"""

NON_GENERIC_TITLE_REQUEST_HTML = b"""<!DOCTYPE html><html><head>
<title>Something Else Entirely</title>
<meta property="og:title" content="Something Else Entirely" />
</head><body><div id="reactRoot"></div></body></html>"""


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


def test_request_subject_fetch_uses_the_undocumented_required_recipe():
    """Reverse-engineered against the real DSM backend: this API only
    works with (a) the /mo/request/webapi/... path prefix - the bare
    /webapi/... path fails, (b) an x-syno-sharing header naming the share
    ID, and (c) the sharing_sid cookie set by the initial page load. All
    three were confirmed missing/wrong independently via manual curl
    testing against the live backend before landing on this combination."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"success": True, "data": {"subject": "X"}}

    with patch("app.requests.post", return_value=mock_response) as mock_post:
        app_module.fetch_request_subject("REQ123", cookies={"sharing_sid": "abc123"})

    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    call_url = mock_post.call_args[0][0]
    assert call_url.endswith("/mo/request/webapi/entry.cgi/SYNO.Foto.Sharing.Passphrase")
    assert kwargs["headers"]["x-syno-sharing"] == "REQ123"
    assert kwargs["cookies"] == {"sharing_sid": "abc123"}
    assert kwargs["data"]["passphrase"] == '"REQ123"'


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
            resp = client.get("/mo/request/REQ123")

    assert mock_post.call_args.kwargs["cookies"] == {"sharing_sid": "real-cookie-value"}
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


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} checks passed")
