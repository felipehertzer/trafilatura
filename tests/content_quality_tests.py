"""Regressions from publisher metadata and hidden content audits."""

import json

import pytest

from trafilatura import extract_metadata

URL = "https://example.org/story"
IMAGE = "https://example.org/article.jpg"


@pytest.mark.parametrize(
    "value", [IMAGE, {"@type": "ImageObject", "url": IMAGE}, {"contentUrl": IMAGE}, [None, {"url": IMAGE}]]
)
def test_article_jsonld_image_without_opengraph(value):
    schema = {"@context": "https://schema.org", "@type": "BlogPosting", "url": URL, "image": value}
    page = '<html><head><script type="application/ld+json">' + json.dumps(schema) + "</script></head><body/></html>"
    assert extract_metadata(page, default_url=URL).image == IMAGE


def test_linked_article_image_skips_related_article_and_publisher_logo():
    schema = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "Organization", "logo": {"url": "https://example.org/logo.jpg"}},
            {"@type": "NewsArticle", "url": "https://example.org/related", "image": "https://example.org/wrong.jpg"},
            {"@type": "NewsArticle", "mainEntityOfPage": {"@id": URL}, "image": {"@id": "#photo"}},
            {"@type": "ImageObject", "@id": "#photo", "url": IMAGE},
        ],
    }
    page = '<html><head><script type="application/ld+json">' + json.dumps(schema) + "</script></head><body/></html>"
    assert extract_metadata(page, default_url=URL).image == IMAGE
    page = page.replace("<head>", '<head><meta property="og:image" content="https://example.org/og.jpg">')
    assert extract_metadata(page, default_url=URL).image == "https://example.org/og.jpg"
