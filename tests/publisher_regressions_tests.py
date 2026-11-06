"""Regressions from production publisher audits (titles, bylines, bodies)."""

import json

from trafilatura import bare_extraction, extract_metadata
from trafilatura.json_metadata import normalize_authors

PARAGRAPH = "The council confirmed on Saturday that the new bridge will open to traffic next month. "


def page(head="", body="", url="https://news.example.com/story"):
    return f'<html><head><link rel="canonical" href="{url}"/>{head}</head><body>{body}</body></html>'


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
    head += ld(
        {"@context": "https://schema.org", "@type": "NewsArticle", "author": [{"@type": "Person", "name": "Thomas Grove"}]}
    )
    assert extract_metadata(page(head)).author == "Thomas Grove; Daria Matviichuk"


def test_meta_author_kept_without_jsonld_person():
    head = '<meta name="author" content="Jane Doe">'
    head += ld({"@context": "https://schema.org", "@type": "NewsArticle", "headline": "Storm"})
    assert extract_metadata(page(head)).author == "Jane Doe"


def test_prefixed_comment_section_is_not_byline_or_body():
    body = (
        "<article><h1>Storm</h1><p>"
        + PARAGRAPH * 3
        + "</p>" * 1
        + '<section class="sas-comments"><div><span class="sas-comments__author">John S.</span>'
        + "<p>Join the conversation. Comments are reviewed before posting and I disagree strongly.</p></div></section>"
        + "</article>"
    )
    result = bare_extraction(page("", body), with_metadata=True)
    assert result.author != "John S"
    assert "Join the conversation" not in result.text


def test_viafoura_feature_class_on_the_article_wrapper_keeps_the_article():
    # Future plc (GamesRadar, 2026-10): the article wrapper carries feature flags,
    # one of them "flw-article-viafoura"; the comment widget is its own element.
    body = (
        '<article><div id="content" class="flw-widget-newsletter flw-article-viafoura">'
        f'<div id="article-body"><p>{PARAGRAPH * 8}</p></div>'
        '<div class="viafoura"><p>Join the conversation. Comments are closed.</p></div>'
        "<vf-conversations><p>Sign in to comment on this story.</p></vf-conversations>"
        "</div></article>"
    )
    result = bare_extraction(page(body=body), include_comments=False)
    assert PARAGRAPH.strip() in result.text
    assert "Join the conversation" not in result.text
    assert "Sign in to comment" not in result.text


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
    article = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": "Storm hits coast",
        "articleSection": None,
        "author": {"@type": "Person", "name": "Jane Doe"},
    }
    head = ld(article) + ld([[{"@type": "WebPage"}], None, "text"])
    metadata = extract_metadata(page(head))
    assert metadata.title == "Storm hits coast"
    assert metadata.author == "Jane Doe"


def test_article_sections_accept_only_strings():
    article = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": "Storm hits coast",
        "articleSection": ["Weather", None, 3],
    }
    assert extract_metadata(page(ld(article))).categories == ["Weather"]


def test_quote_truncated_meta_title_uses_title_element():
    head = '<title>Mojtaba Khamenei was "pulled from rubble" of hospital: Report</title>'
    head += '<meta property="og:title" content="Mojtaba Khamenei was "pulled from rubble" of hospital: Report">'
    assert extract_metadata(page(head)).title == 'Mojtaba Khamenei was "pulled from rubble" of hospital: Report'


def test_complete_meta_title_is_kept():
    head = '<title>Storm hits coast | Example</title><meta property="og:title" content="Storm hits coast">'
    assert extract_metadata(page(head)).title == "Storm hits coast"


def test_all_capitals_bylines_are_title_cased():
    assert (
        normalize_authors(None, "VANESSA PAIGE CHELVAN; NICOLAS VAUX-MONTAGNY")
        == "Vanessa Paige Chelvan; Nicolas Vaux-Montagny"
    )
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
    body = (
        "<article><p>"
        + PARAGRAPH * 3
        + '</p></article><div class="infinite-scroll"><p>'
        + "Appended story text. " * 20
        + "</p></div>"
    )
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


def test_hidden_continuation_after_its_control_is_text():
    rest = "Residents will be consulted on the final design before construction begins next spring. "
    body = (
        '<article><div class="body"><div class="items"><p>' + PARAGRAPH * 2 + "</p><p>" + PARAGRAPH * 2 + "</p></div>"
        '<div class="more"><button>Story continues</button></div>'
        '<div class="rest" style="display: none"><p>' + rest * 2 + "</p><p>" + rest * 2 + "</p></div></div></article>"
    )
    text = bare_extraction(page("", body)).text
    assert "consulted on the final design" in text
    assert "Story continues" not in text


def test_only_a_continuation_behind_its_control_is_revealed():
    from lxml import html

    from trafilatura.htmlprocessing import reveal_hidden_continuation

    prose = "<p>" + PARAGRAPH * 2 + "</p>"
    visible = '<div class="items">' + prose * 2 + "</div>"
    hidden = '<div class="rest" style="display:none">' + prose * 2 + "</div>"
    cases = {
        # the control alone, then the rest of the article, last in the body
        visible + '<div class="more"><button>Continue reading</button></div>' + hidden: True,
        # a dialog: its button comes with other text
        visible + '<div class="consent"><p>We use cookies.</p><button>Accept</button></div>' + hidden: False,
        # not the last block of its container
        visible + "<button>Show</button>" + hidden + "<div>Related stories</div>": False,
        # no visible article paragraphs before it
        "<button>Show</button>" + hidden: False,
    }
    for body, revealed in cases.items():
        tree = html.fromstring(f"<html><body><article>{body}</article></body></html>")
        reveal_hidden_continuation(tree)
        assert (tree.find(".//div[@class='rest']").get("style") is None) is revealed, body


def test_caption_of_a_figure_left_in_a_paragraph_is_not_text():
    # WordPress image blocks nested in a figure inside <p>: the cleaning loses track of later figures
    image = (
        '<div class="media"><figure class="outer"><p><figure class="wp-caption"><img src="a.jpg">'
        "<figcaption>The Disney line was a staple of the catalog. H. Armstrong Roberts/Getty Images"
        "</figcaption></figure></p></figure></div>"
    )
    body = "<article>" + ("<p>" + PARAGRAPH * 2 + "</p>" + image) * 3 + "</article>"
    text = bare_extraction(page("", body)).text
    assert "new bridge" in text
    assert "Getty Images" not in text


def test_apostrophes_inside_words_are_not_quotes():
    # a quoted nickname goes, an apostrophe inside a word does not open one
    assert normalize_authors(None, "From CNN's Dana O'Neil in Atlanta") == "CNN's Dana O'Neil"
    assert normalize_authors(None, "From CNN’s Frank Nunns O’Connell") == "CNN’s Frank Nunns O’Connell"
    assert normalize_authors(None, "Dana 'Dee' O'Neil") == "Dana O'Neil"
    assert normalize_authors(None, 'Jim "Jimbo" Smith') == "Jim Smith"
    assert normalize_authors(None, "Yoshi(nostalgicfish)") == "Yoshi"


def test_capitals_label_glued_to_a_name_goes():
    assert normalize_authors(None, "CITY BEATChris Herde") == "Chris Herde"
    assert normalize_authors(None, "By CITY BEATChris Herde") == "Chris Herde"
    assert normalize_authors(None, "CNNNeil Connell") == "Neil Connell"
    # brands written in the same way stay
    for brand in ("TRUCKtalk", "SUPERCentral", "FOXBusiness", "HCLTech", "ACROBiosystems", "IDTechEx", "AJLabs", "RLDatix"):
        assert normalize_authors(None, brand) == brand
    for byline in ("CXFocus Editor", "CQUniversity Australia", "USCFootball Staff", "DDNet News Desk"):
        assert normalize_authors(None, byline) == byline


def test_site_suffix_after_a_headline_holding_a_separator():
    title = "These earbuds are a top pick for workouts - and they're now $40 off"
    head = f'<meta property="og:title" content="{title} - ZDNET"><meta property="og:site_name" content="ZDNET">'
    url = "https://www.zdnet.com/tech/earbuds-deal/"
    assert extract_metadata(page(head, url=url)).title == title
    # the JSON-LD headline writes the same title with an en dash: no match, the suffix still goes
    headline = title.replace(" - ", " – ")
    head += ld({"@context": "https://schema.org", "@type": "NewsArticle", "headline": headline})
    assert extract_metadata(page(head, url=url)).title == title
    # a first segment naming the site still takes the rest with it
    head = '<meta property="og:title" content="Page Not Found - Three D Radio - South Australia">'
    head += '<meta property="og:site_name" content="Three D Radio">'
    assert extract_metadata(page(head)).title == "Page Not Found"


def test_site_suffix_naming_the_publisher_without_its_legal_form():
    head = '<meta property="og:title" content="Wordy Challenge (18/06/2026) 18/06/2026 - The North Central Review">'
    head += ld(
        {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "publisher": {"@type": "Organization", "name": "The North Central Review Pty Ltd"},
        }
    )
    metadata = extract_metadata(page(head, url="https://ncreview.com.au/2026/06/18/wordy-2026-06-18/"))
    assert metadata.title == "Wordy Challenge (18/06/2026) 18/06/2026"


def test_site_prefix_before_the_page_headline():
    headline = "Weezer Australia 2027 Tour"
    head = f'<meta property="og:title" content="scenestr - {headline}"><meta property="og:site_name" content="scenestr">'
    url = "https://scenestr.com.au/music/weezer-australia-2027-tour-20261103"
    assert extract_metadata(page(head, f"<h1>{headline}</h1>", url=url)).title == headline
    # without the headline on the page the rest may be a tagline: it stays
    head = '<meta property="og:title" content="The Canberra Times | Canberra, ACT">'
    head += '<meta property="og:site_name" content="The Canberra Times">'
    title = extract_metadata(page(head, "<h1>Local News</h1>", url="https://www.canberratimes.com.au/")).title
    assert title == "The Canberra Times | Canberra, ACT"


def test_byline_skips_author_bio_prose_and_category_links():
    # scenestr: the author box's prose is no name; its byline is a generic one
    box = (
        '<div id="author-bio" class="d-flex" itemprop="author"><div class="author-details">'
        '<h4 class="author-name"> Written by <a href="/staff-writers">Staff Writers</a> </h4>'
        '<div class="author-about mb-2">Our eclectic team of writers from around Australia – and some beyond.</div>'
        "</div></div>"
    )
    html = page('<meta name="author" content="Staff Writers">', f"<p>{PARAGRAPH}</p>{box}")
    assert extract_metadata(html).author == "Staff Writers"
    assert extract_metadata(html, author_blacklist={"Staff Writers"}).author is None
    # cxfocus: a WordPress byline row also holds the post's categories
    byline = (
        '<div class="byline">By <span class="author vcard"><a class="url fn n" href="/author/admin/">Mark Atterby</a>'
        '</span> <span class="cat-links">Posted in <a href="/category/contact-centre/" rel="category tag">'
        "Contact Centre &amp; Channels</a></span></div>"
    )
    assert extract_metadata(page(body=f"{byline}<p>{PARAGRAPH}</p>")).author == "Mark Atterby"
    # Twenty Seventeen: the author-description box holds the name heading, which stays
    box = (
        '<div class="author-description"><h2 class="author-title">Jane Doe</h2>'
        '<p class="author-bio">Jane writes about cities and the people who build them.</p></div>'
    )
    assert extract_metadata(page(body=f"<p>{PARAGRAPH}</p>{box}")).author == "Jane Doe"


STANDFIRST = "The metadata block carries the standfirst of the live coverage for readers. "


def test_css_module_comment_preview_is_removed():
    # Nine's mastheads (AFR) show a reader comment under the live-blog header; CSS-module class
    # names put a hash before the name, so "commentsPreviewList" does not begin the class
    body = (
        '<article><section class="_93a09f9fd29a3122-articleMetadata _93a09f9fd29a3122-hasCommentsButton">'
        "<p>" + STANDFIRST * 3 + "</p></section>"
        '<section class="_0a7ca1bb3077d74a-wrapper"><ul class="_0a7ca1bb3077d74a-commentsPreviewList"><li>'
        '<div class="_0a7ca1bb3077d74a-commentPreview"><div class="_0a7ca1bb3077d74a-commentInfo">'
        '<span class="_0a7ca1bb3077d74a-commenterName">investmentideas2026</span>'
        '<span class="_0a7ca1bb3077d74a-commentTimeStamp">2 months ago</span></div>'
        "<p>Shame nobody copies either side's policies, they would do more good.</p></div></li></ul></section>"
        "<p>" + PARAGRAPH * 4 + "</p></article>"
    )
    text = bare_extraction(page("", body), include_comments=False).text
    assert "investmentideas2026" not in text
    assert "nobody copies" not in text
    assert "new bridge will open" in text
    # the metadata section only says the page has a comments button: it keeps its text
    assert "standfirst of the live coverage" in text


def test_css_module_commentary_is_kept():
    body = '<article><div class="_0a7ca1bb3077d74a-commentary"><p>' + PARAGRAPH * 6 + "</p></div></article>"
    text = bare_extraction(page("", body), include_comments=False).text
    assert "new bridge will open" in text
