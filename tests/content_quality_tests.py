"""Regressions from publisher metadata and hidden content audits."""

import json

import pytest

from trafilatura import extract_metadata

URL = "https://example.org/story"
IMAGE = "https://example.org/article.jpg"


def test_author_credit_excludes_review_status_badge():
    page = (
        '<html><body><div class="article-meta-byline">'
        '<span class="article-meta-source"><a href="/source">Kennesaw State University</a></span>'
        '<span class="article-meta-date">Sep 26 2026</span>'
        '<span class="article-meta-status"><a href="#">Reviewed</a></span>'
        '</div></body></html>'
    )
    assert extract_metadata(page).author == "Kennesaw State University"


@pytest.mark.parametrize("author", ["Jane Smith", "Daily Cargo News"])
def test_author_profile_link_excludes_surrounding_social_navigation(author):
    page = (
        '<html><body><div class="hs-author-avatar"><span>Posted by </span>'
        f'<a class="profile author-link" href="/authors/profile"><strong>{author}</strong></a>'
        '<p><a class="hs-author-social-link" href="">LinkedIn</a> | '
        '<a href="">Website</a></p></div></body></html>'
    )
    assert extract_metadata(page).author == author
    assert extract_metadata(page, author_blacklist={author}).author is None


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
