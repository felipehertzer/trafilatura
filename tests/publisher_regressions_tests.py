"""Regressions from production publisher audits (titles, bylines, bodies)."""

import json

from trafilatura import bare_extraction, extract_metadata
from trafilatura.json_metadata import normalize_authors

PARAGRAPH = "The council confirmed on Saturday that the new bridge will open to traffic next month. "


def page(head="", body="", url="https://news.example.com/story"):
    return (
        f'<html><head><link rel="canonical" href="{url}"/>{head}</head>'
        f"<body>{body}</body></html>"
    )


def ld(value):
    return '<script type="application/ld+json">' + json.dumps(value) + "</script>"


def test_jsonld_headline_replaces_branded_meta_title():
    head = '<meta property="og:title" content="Storm hits coast | Example News | Local News covering the region">'
    head += ld({"@context": "https://schema.org", "@type": "NewsArticle", "headline": "Storm hits coast"})
    assert extract_metadata(page(head)).title == "Storm hits coast"


def test_site_name_suffix_is_removed():
    head = '<meta property="og:title" content="Senate passes the budget bill | CNN Politics">'
    head += '<meta property="og:site_name" content="CNN">'
    assert extract_metadata(page(head)).title == "Senate passes the budget bill"


def test_hostname_suffix_is_removed():
    head = '<meta property="og:title" content="Apple ships new firmware update - 9to5Mac">'
    metadata = extract_metadata(page(head, url="https://9to5mac.com/story"))
    assert metadata.title == "Apple ships new firmware update"


def test_headline_prefix_marks_branding():
    head = '<meta property="og:title" content="Mayor opens new library | The Express | Regional news">'
    assert extract_metadata(page(head, "<h1>Mayor opens new library</h1>")).title == "Mayor opens new library"


def test_unrelated_trailing_segment_is_kept():
    head = '<meta property="og:title" content="Taylor Swift announces tour - Love">'
    head += '<meta property="og:site_name" content="Love Music">'
    metadata = extract_metadata(page(head, url="https://lovemusicblog.com/story"))
    assert metadata.title == "Taylor Swift announces tour - Love"


def test_story_by_prefix_is_removed():
    assert normalize_authors(None, "Story by Reuters") == "Reuters"
    assert normalize_authors(None, "Analysis by Jane Doe") == "Jane Doe"


def test_visual_credits_are_not_authors():
    assert normalize_authors(None, "Jane Doe | Photography by John Roe for WSJ") == "Jane Doe"
    assert normalize_authors(None, "Jane Doe and Illustrations by John Roe") == "Jane Doe"
    assert normalize_authors(None, "Photos by John Roe") is None


def test_meta_coauthors_are_kept_beside_jsonld_person():
    head = '<meta name="author" content="Thomas Grove and Daria Matviichuk">'
    head += ld({"@context": "https://schema.org", "@type": "NewsArticle", "author": [{"@type": "Person", "name": "Thomas Grove"}]})
    assert extract_metadata(page(head)).author == "Thomas Grove; Daria Matviichuk"


def test_meta_author_kept_without_jsonld_person():
    head = '<meta name="author" content="Jane Doe">'
    head += ld({"@context": "https://schema.org", "@type": "NewsArticle", "headline": "Storm"})
    assert extract_metadata(page(head)).author == "Jane Doe"


def test_prefixed_comment_section_is_not_byline_or_body():
    body = (
        "<article><h1>Storm</h1>" + "<p>" + PARAGRAPH * 3 + "</p>" * 1
        + '<section class="sas-comments"><div><span class="sas-comments__author">John S.</span>'
        + "<p>Join the conversation. Comments are reviewed before posting and I disagree strongly.</p></div></section>"
        + "</article>"
    )
    result = bare_extraction(page("", body), with_metadata=True)
    assert result.author != "John S"
    assert "Join the conversation" not in result.text


def test_has_comments_state_class_does_not_drop_article():
    body = '<article class="post has-comments"><p>' + PARAGRAPH * 4 + "</p></article>"
    assert "new bridge" in bare_extraction(page("", body)).text


def test_linked_sentence_is_kept():
    linked = (
        '<p><a href="/other">The Queenslander camp, as reported by this masthead, '
        "was holding out hope for close to $1.7 million.</a></p>"
    )
    body = "<article>" + "<p>" + PARAGRAPH * 2 + "</p>" + linked + "<p>" + PARAGRAPH + "</p></article>"
    assert "holding out hope" in bare_extraction(page("", body)).text


def test_linked_headline_teaser_is_removed():
    teaser = '<p><a href="/other">Read the latest live updates on the coastal storm, the road closures and the evacuation centres across the region today here</a></p>'
    body = "<article>" + "<p>" + PARAGRAPH * 3 + "</p>" + teaser + "</article>"
    assert "latest updates" not in bare_extraction(page("", body)).text


def test_split_article_body_is_merged():
    body = (
        '<article><section class="article__body"><p>' + PARAGRAPH * 3 + "</p></section>"
        '<div class="ad">Advertisement</div>'
        '<section class="article__body article__body--bottom"><p>'
        "The other three people were taken to Waitakere Hospital in a stable condition.</p></section></article>"
    )
    assert "Waitakere Hospital" in bare_extraction(page("", body)).text


def test_template_content_is_not_text():
    body = "<article><template><p>[category] [title]</p></template><p>" + PARAGRAPH * 3 + "</p></article>"
    assert "[category]" not in bare_extraction(page("", body)).text


def test_malformed_jsonld_nodes_do_not_discard_metadata():
    article = {"@context": "https://schema.org", "@type": "NewsArticle", "headline": "Storm hits coast",
               "articleSection": None, "author": {"@type": "Person", "name": "Jane Doe"}}
    head = ld(article) + ld([[{"@type": "WebPage"}], None, "text"])
    metadata = extract_metadata(page(head))
    assert metadata.title == "Storm hits coast"
    assert metadata.author == "Jane Doe"


def test_article_sections_accept_only_strings():
    article = {"@context": "https://schema.org", "@type": "NewsArticle", "headline": "Storm hits coast",
               "articleSection": ["Weather", None, 3]}
    assert extract_metadata(page(ld(article))).categories == ["Weather"]


def test_quote_truncated_meta_title_uses_title_element():
    head = '<title>Mojtaba Khamenei was "pulled from rubble" of hospital: Report</title>'
    head += '<meta property="og:title" content="Mojtaba Khamenei was "pulled from rubble" of hospital: Report">'
    assert extract_metadata(page(head)).title == 'Mojtaba Khamenei was "pulled from rubble" of hospital: Report'


def test_complete_meta_title_is_kept():
    head = '<title>Storm hits coast | Example</title><meta property="og:title" content="Storm hits coast">'
    assert extract_metadata(page(head)).title == "Storm hits coast"


def test_all_capitals_bylines_are_title_cased():
    assert normalize_authors(None, "VANESSA PAIGE CHELVAN; NICOLAS VAUX-MONTAGNY") == "Vanessa Paige Chelvan; Nicolas Vaux-Montagny"
    assert normalize_authors(None, "AAP") == "AAP"
    assert normalize_authors(None, "Jane McDonald") == "Jane McDonald"


def test_infinite_scroll_keeps_current_article():
    url = "https://news.example.com/story"
    body = (
        '<section><div class="box-wrap infinite-scroll">'
        f'<article class="box open" rel="{url}/"><h1>Storm</h1><p>' + PARAGRAPH * 4 + "</p></article>"
        '<article class="box" rel="https://news.example.com/other"><p>Another story entirely about football results.</p></article>'
        "</div></section>"
    )
    text = bare_extraction(page("", body, url), url=url).text
    assert "new bridge" in text
    assert "football results" not in text


def test_infinite_scroll_without_permalink_is_still_removed():
    body = "<article><p>" + PARAGRAPH * 3 + '</p></article><div class="infinite-scroll"><p>' + "Appended story text. " * 20 + "</p></div>"
    assert "Appended story" not in bare_extraction(page("", body)).text


def test_select_options_do_not_steer_readability():
    from trafilatura.readability_lxml import Document as ReadabilityDocument

    options = "".join(f"<option>Country number {i} with a long descriptive name</option>" for i in range(200))
    html = page("", f"<form><select>{options}</select></form><article><p>" + PARAGRAPH * 6 + "</p></article>")
    from trafilatura.utils import load_html

    summary = ReadabilityDocument(load_html(html)).summary()
    assert "new bridge" in summary
    assert "Country number" not in summary


def test_viafoura_comment_widgets_are_removed():
    body = (
        "<article><p>" + PARAGRAPH * 4 + "</p>"
        '<div class="viafoura"><vf-conversations><p>You must confirm your public display name before commenting.</p>'
        "</vf-conversations></div></article>"
    )
    text = bare_extraction(page("", body), include_comments=False).text
    assert "public display name" not in text
