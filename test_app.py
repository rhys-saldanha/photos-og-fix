"""
Self-check for app.py's fix_og_tags(). No framework, no fixtures - just
asserts. Run with: python3 test_app.py
"""
from bs4 import BeautifulSoup

from app import fix_og_tags


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


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n{len(tests)} checks passed")
