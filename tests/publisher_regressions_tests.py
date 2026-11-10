"""Regressions from production publisher audits (titles, bylines, bodies)."""

import json
import re

import pytest
from lxml.html import fromstring

from trafilatura import bare_extraction, extract_metadata
from trafilatura.baseline import baseline
from trafilatura.external import _wraps_own_extraction
from trafilatura.htmlprocessing import is_call_to_action, prune_trailing_calls, reveal_hidden_continuation
from trafilatura.json_metadata import normalize_authors
from trafilatura.justext_lite import extract_paragraphs
from trafilatura.readability_lxml import Document as ReadabilityDocument
from trafilatura.settings import Extractor

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


def test_furniture_beside_a_one_block_body_stays_out():
    # Inside Retail: the story is one plain text run (no <p>), which the first frame
    # (article__body) takes whole as one block, so the search goes on to the wider <article>.
    # All that is left there is furniture (meta, the subscription offer box, related stories);
    # the over-pruning guard counts the text already taken and prunes it instead of keeping it.
    story = "Same-store sales fell three per cent, yet operating profit rose thirteen per cent to a record. "
    offer = (
        '<div id="premium-box" class="premium-box__content"><div class="premium-box">'
        "<h3>This content is for Pro subscribers only.</h3><p>Subscribe now to unlock an all-access pass.</p>"
        "<h4>Pro - Monthly</h4><ul><li>Unlimited news access</li><li>Daily Pro content straight to your inbox</li>"
        "<li>Exclusive members only masterclasses</li><li>Weekly careers advice</li></ul>"
        '<a href="https://news.example.com/subscription">Subscribe now</a></div></div>'
    )
    related = "".join(
        f'<div class="post__item post-{n} category-top-stories"><a href="https://news.example.com/s{n}">Story {n}</a>'
        "<div>Retailers report a mixed start to the season across the region.</div></div>"
        for n in range(5)
    )
    body = (
        '<article class="post-1 post type-post status-publish hentry">'
        '<div class="article__meta">By Jane Doe | 5 November 2026</div><div class="row"><div class="col">'
        '<div class="article__body"><div class="article__text__holder">'
        + (story * 3 + "\n\n\n") * 12
        + "</div></div>"
        + offer
        + related
        + "</div></div></article>"
    )
    for options in ({"fast": True}, {}):
        text = bare_extraction(page("", body), **options).text
        assert text.count("operating profit rose thirteen per cent") == 36, options
        assert "subscribers only" not in text, options
        assert "Subscribe now" not in text, options
        assert "mixed start to the season" not in text, options


def test_over_pruning_guard_counts_text_taken_by_a_narrower_frame():
    from lxml import html
    from lxml.etree import XPath

    from trafilatura.htmlprocessing import prune_unwanted_nodes

    def box_pruned(extracted_len):
        frame = html.fromstring('<article><div class="box">' + "Subscribe now for full access. " * 20 + "</div>x</article>")
        prune_unwanted_nodes(frame, [XPath(".//div[@class='box']")], with_backup=True, extracted_len=extracted_len)
        return frame.find(".//div[@class='box']") is None

    # alone in its frame the box is almost all the text, so the guard keeps it, as before
    assert not box_pruned(0)
    # beside an article already taken from a narrower frame it is furniture
    assert box_pruned(5000)


def test_a_short_block_taken_before_leaves_the_guard_as_it_was():
    # a standfirst-length block from the first frame is no article: the wider frame is
    # read with the guard as before (its headline and lead still come in)
    from trafilatura.main_extractor import _extract
    from trafilatura.settings import Extractor
    from trafilatura.utils import load_html

    body = (
        '<article class="post-1 post type-post"><h1>Holidays kick off with gumboot hunt</h1>'
        '<div class="article__body"><div class="article__text__holder">With school holidays set to kick off tonight.</div></div>'
        '<div class="share-buttons">Share this: Facebook X LinkedIn</div></article>'
    )
    _, text, _ = _extract(load_html(page("", body)), Extractor(output_format="txt", fast=True))
    assert "With school holidays" in text


def test_byline_details_after_a_comma_describe_the_person():
    # role, then the role's organisation
    assert normalize_authors(None, "Lydia Edwards, Fashion historian, Edith Cowan University") == "Lydia Edwards"
    assert normalize_authors(None, "Phillip Makowski, Marketing Director, Dematic Australia") == "Phillip Makowski"
    assert normalize_authors(None, "Sean Crawford, Managing Director, North America, SMG") == "Sean Crawford"
    assert normalize_authors(None, "Marlow Nickell, Co-founder and CEO, Grocery TV") == "Marlow Nickell"
    # credentials, an organisation, a website and a programme part on their own
    assert normalize_authors(None, "Chelsea Rae Bourgeois, RDN, LD") == "Chelsea Rae Bourgeois"
    assert normalize_authors(None, "Kate Dore, CFP®, EA") == "Kate Dore"
    assert normalize_authors(None, "Cynthia Lee, McGill University") == "Cynthia Lee"
    assert normalize_authors(None, "Robert Jennings, InnerSelf.com") == "Robert Jennings"
    assert normalize_authors(None, "Andrew Whitelaw, Episode 3") == "Andrew Whitelaw"
    # a line break before the affiliation, and the city after it
    assert normalize_authors(None, "Santosh Mehrotra <br> \nHigher School of Economics, Moscow") == "Santosh Mehrotra"


def test_byline_list_and_joined_names_stay():
    assert normalize_authors(None, "Sanj Patel, Sebastian Bugeja-Drinkell, Gabriel Rojas") == (
        "Sanj Patel; Sebastian Bugeja-Drinkell; Gabriel Rojas"
    )
    assert normalize_authors(None, "Sarah Martin, Chief political correspondent and Paul Karp") == "Sarah Martin; Paul Karp"
    assert normalize_authors(None, "Jane Doe, Editor, and John Roe") == "Jane Doe; John Roe"
    # a surname that is a plural role word names a person
    assert normalize_authors(None, "Jane Doe, Taylah Fellows") == "Jane Doe; Taylah Fellows"


def test_byline_label_keeps_the_capitals_of_the_name():
    assert normalize_authors(None, "Written By: EIN Presswire") == "EIN Presswire"


def test_a_website_byline_is_no_person():
    assert normalize_authors(None, "Australia.so") is None
    assert normalize_authors(None, "mirror.co.uk") is None
    assert normalize_authors(None, "Jane Doe; cricket.com.au") == "Jane Doe"
    assert normalize_authors(None, "J.R.R. Tolkien") == "J R R Tolkien"
    assert normalize_authors(None, "Dr.Smith") == "Dr Smith"
    assert normalize_authors(None, "g.lynch") == "G Lynch"


STORY = "".join(f"<p>{PARAGRAPH}Paragraph {n} of the story says more about the bridge works.</p>" for n in range(6))
LAST_WORDS = "Paragraph 5 of the story says more about the bridge works."


def story_text(tail, url="https://news.example.com/story", fast=True):
    body = f"<article><h1>Bridge opens</h1>{STORY}{tail}</article><footer><p>Contact us</p></footer>"
    return bare_extraction(page(body=body, url=url), url=url, fast=fast).text


@pytest.mark.parametrize("fast", [True, False])
@pytest.mark.parametrize(
    "tail",
    [
        # newsletter, social profiles and the copyright notice under the story
        (
            '<p><strong>Sign up to our <a href="/toolbar/sign-up-for-e-news" target="_blank">newsletter</a>, like us on '
            '<a href="https://www.facebook.com/examplenews">Facebook</a> or follow us on '
            '<a href="https://twitter.com/examplenews">Twitter</a>.</strong></p>'
            "<p>Advertisement</p><p>© 2026 Example News. All rights reserved.</p>"
        ),
        # a list of the site's own pages and calls
        (
            '<ul><li><a href="https://shop.example.com/">Subscribe to our magazine</a></li>'
            '<li>Check out <a href="https://www.example.com/">www.example.com</a></li>'
            '<li><a href="https://www.example.com/subscription/">Subscribe to our newsletter</a></li>'
            '<li><a href="https://issuu.com/example/docs/issue-12">View our digital magazine</a></li></ul>'
        ),
        # related stories under their label, one card a category, a byline and a date
        (
            '<h2>Related news</h2><div><a href="/category/whats-new/">What\'s New</a> '
            '<a href="/author/jane/">BY Jane Doe</a> November 4, 2026</div>'
        ),
        # sharing buttons and a related-links block set as a quote
        (
            '<p>Share</p><blockquote class="wp-block-related-links"><p>Related*</p>'
            '<p><a href="/other-story">Council approves the second bridge</a></p></blockquote>'
        ),
        # a registration box around its form, and an ad slot
        (
            '<div class="paywall"><div class="register_box"><p>Register to continue</p><p>Why register?</p>'
            "<ul><li>Registration is free and only takes a moment</li><li>Read 3 articles a month</li></ul>"
            '<form action="/register"><input type="email"><button>Register</button></form></div></div>'
            '<div class="adv adv-leaderboard"><div class="code-block-label">Advertisement</div></div>'
        ),
        # the copyright notice above a few page labels
        "<p>© 2026 Example News. All rights reserved.</p><div>MEET OUR NEWS CHANNELS</div><div>Request a Correction</div>",
        # a copyright line marked as such
        '<div class="row article-copyright"><span>© All content copyright Example. Do not reproduce.</span></div>',
    ],
)
def test_page_furniture_after_the_story_goes(tail, fast):
    text = story_text(tail, url="https://www.example.com/story", fast=fast)
    assert text.endswith(LAST_WORDS)


@pytest.mark.parametrize(
    "tail",
    [
        # a social post, not a profile
        '<p>In a post on <a href="https://www.instagram.com/p/C1x2y3/">Instagram</a>, the mayor thanked residents.</p>',
        # another site's subscription page
        '<p>Residents can <a href="https://council.example.org/subscribe">subscribe to council alerts</a>.</p>',
        # a related label inside the story: prose follows it
        "<p>Related</p><p>The second stage of the project starts in 2028, the state treasurer said.</p>",
        # the writer's own profile closes the story
        (
            "<p>Great bridges need not cost a fortune, and this one came in under budget. Follow "
            '<a href="https://www.instagram.com/dr.yalda/">Dr Yalda</a> for more.</p>'
        ),
    ],
)
def test_story_paragraphs_that_link_out_stay(tail):
    text = story_text(tail)
    last = re.sub("<[^>]+>", "", tail.split("</p>")[-2].split("<p>")[-1])
    assert text.endswith(last)


def test_furniture_holding_half_the_text_stays():
    tree = fromstring(
        '<div><p>The bridge opens.</p><p>Follow us on <a href="https://www.facebook.com/examplenews">Facebook</a>'
        " for every update on the bridge works, the detours and the opening day.</p></div>"
    )
    prune_trailing_calls(tree, "https://news.example.com/story")
    assert len(tree.findall(".//p")) == 2


@pytest.mark.parametrize(
    ("target", "call"),
    [
        ("https://www.linkedin.com/company/example-news/", True),
        ("https://www.facebook.com/groups/examplenewsreaders", True),
        # someone else's profile, or a group known by its number only
        ("https://www.instagram.com/dr.yalda/", False),
        ("https://www.facebook.com/groups/1264421139061862", False),
        ("https://www.tiktok.com/@example", True),
        ("https://twitter.com/example/status/123", False),
        ("https://www.youtube.com/watch?v=abc", False),
        ("https://example.com/#subscribe", True),
        ("https://news.example.com/newsletter", True),
        ("https://lists.example.org/newsletter", False),
        ("https://other.example.org/sign-up", False),
        ("/subscribe/", True),
        ("http://[broken", False),
        ("", False),
    ],
)
def test_calls_to_action(target, call):
    assert is_call_to_action(target, "news.example.com") is call


def test_a_related_list_inside_the_story_goes_with_its_label():
    related = (
        "<p><strong>Related stories:</strong></p><ul>"
        '<li><a href="/one">Council approves the second bridge</a></li>'
        '<li><a href="/two">Detours start on Monday</a></li></ul>'
    )
    body = f"<article><h1>Bridge opens</h1>{STORY[:400]}{related}{STORY[400:]}</article>"
    text = bare_extraction(page(body=body), url="https://news.example.com/story").text
    assert "Related stories" not in text
    assert "Detours start on Monday" not in text
    assert text.endswith(LAST_WORDS)


# A list article whose items keep their text inside the figure beside the image (BuzzFeed).
ITEM_PROSE = [
    "This texture spray recreates beachy hair without the dry crunchy feeling, and doubles as a light hair powder.",
    "A leave-in conditioning spray that adds moisture, detangles and seals the cuticle after colour, every single day.",
    "This balm melts into the skin with coconut and jojoba oil, and it is the one thing everybody asks to borrow.",
]


def figure_item(number, prose, credit="Example Photos / Getty Images"):
    return (
        f'<div class="subbuzz"><h2 class="subbuzz__title">{number}. Item {number}</h2>'
        f'<figure class="subbuzz__media"><img src="https://img.example.com/{number}.jpg">'
        f'<div class="subbuzz__figure-footer"><span class="subbuzz__attribution">{credit}</span>'
        f'<div class="subbuzz__description"><p>{prose}</p><p>Buy it from Example Store for $4{number}.</p></div>'
        "</div></figure></div>"
    )


def test_a_figures_own_prose_is_article_text():
    items = "".join(figure_item(n, prose) for n, prose in enumerate(ITEM_PROSE, 1))
    body = f'<main><article><div class="post_content"><h1>Gifts</h1><p>{PARAGRAPH * 2}</p>{items}</div></article></main>'
    text = bare_extraction(page(body=body), include_comments=False, with_metadata=False).text
    for number, prose in enumerate(ITEM_PROSE, 1):
        assert prose in text
        assert f"Buy it from Example Store for $4{number}." in text
    assert "Getty Images" not in text


@pytest.mark.parametrize(
    "figure",
    [
        # a caption, a credit line and a quoted post are no prose of the figure's own
        (
            '<figure><img src="a.jpg"><figcaption><p>The new bridge, seen from the river at dusk on Friday evening, '
            "shortly before the lights were switched on for the first time.</p></figcaption></figure>"
        ),
        '<figure><img src="a.jpg"><p>Photo: Example Photos</p></figure>',
        (
            '<figure class="tweet"><blockquote><p>The bridge looks amazing tonight, well done to everyone who '
            "worked on it over the last three years!</p></blockquote></figure>"
        ),
    ],
)
def test_figure_captions_credits_and_quotes_still_go(figure):
    body = f"<article><h1>Bridge</h1><p>{PARAGRAPH * 3}</p>{figure}<p>{PARAGRAPH}</p></article>"
    text = bare_extraction(page(body=body), include_comments=False, with_metadata=False).text
    for words in ("dusk", "Example Photos", "amazing"):
        assert words not in text


# The site menu in the page header (BuzzFeed): its container's class starts with "main", like a
# main-content frame, and it holds the footer links and the privacy notice.
SITE_MENU = (
    '<header class="header"><div class="mainNavContainer"><section id="js-more-nav" role="dialog">'
    "<div>Browse links</div><p>US residents can opt out of sales of personal data.</p>"
    '<ul><li><a href="/press">Press</a></li><li><a href="/rss">RSS</a></li><li><a href="/privacy">Privacy</a></li></ul>'
    "<p>© 2026 Example Media, Inc</p></section></div></header>"
)


def test_the_site_menu_is_not_the_body_frame():
    body = SITE_MENU + f"<main><div><p>{PARAGRAPH}</p><p>Residents can cross on foot from Monday.</p></div></main>"
    text = bare_extraction(page(body=body), include_comments=False, with_metadata=False).text
    assert PARAGRAPH.strip() in text
    assert "opt out" not in text
    assert "Example Media" not in text


@pytest.mark.parametrize(
    "body",
    [
        # an unclosed header swallows the page, its h1 and the story with it
        f'<header class="Fixed"><h1>Bridge opens</h1><div class="main-content"><p>{PARAGRAPH * 3}</p></div>',
        # an article's own header holding its standfirst
        (
            f'<article><header class="entry-header"><h1>Bridge</h1><div class="entry-content"><p>{PARAGRAPH * 3}</p>'
            "</div></header></article>"
        ),
    ],
)
def test_a_header_holding_the_page_is_no_chrome(body):
    text = bare_extraction(page(body=body), include_comments=False, with_metadata=False).text
    assert PARAGRAPH.strip() in text


# A Big News Network masthead: no frame class, so the text is recovered from the whole page.
STORY_SENTENCES = [
    "NEW YORK, New York - U.S. stock markets were again sold off on Tuesday as the ceasefire deadline loomed.",
    "The technology-heavy NASDAQ Composite led declines among the major U.S. benchmarks, falling 144 points.",
    "The Dow Jones Industrial Average also suffered a triple-digit loss, shedding 292 points to close lower.",
]


def masthead(between="", tail_inside=""):
    # a story of the usual length, far longer than the promos (the over-pruning guard keeps its frame)
    markets = ["Tokyo", "Sydney", "Frankfurt", "London", "Paris", "Toronto", "Sao Paulo", "Mumbai", "Seoul"]
    story = "".join(f"<p><span>{sentence}</span></p>" for sentence in STORY_SENTENCES) + "".join(
        f"<p>In {market}, the benchmark index moved {index + 1}.{index} percent as investors weighed the outlook for "
        f"interest rates, energy prices and the {market} currency over the coming {index + 2} weeks.</p>"
        for index, market in enumerate(markets)
    )
    return page(
        body='<div class="wrapper"><div class="container"><div class="left-section single">'
        '<div class="world-new-section"><div class="banner-section"><h2>Stocks slide</h2><p>Lola<br>22 Apr 2026</p>'
        f'<div class="banner-text"><div>{story}{between}{tail_inside}</div></div></div></div>'
        '<div class="row collapse share-section"><div class="share"><h5><a href="#">Share article:</a></h5></div></div>'
        '<div id="taboola-below-article-thumbnails"></div>'
        '<div class="row collapse subscribe"><div class="subscribe-section"><h5><a href="#">Subscribe and Follow</a></h5>'
        "<p>Get a daily dose of Example Sun news through our daily email, its complimentary and keeps you fully up "
        'to date with world and business news as well.</p><form><input type="text" name="email">'
        "<button>Subscribe Now</button></form></div></div>"
        '<div class="row collapse news-releases"><div class="news-releases-text"><h4><a href="#">News RELEASES</a></h4>'
        "<p>Publish news of your business, community or sports group by submitting a news release to Example Sun.</p>"
        '<a href="/news-releases" class="more-info">More Information</a></div></div>'
        "</div></div></div>"
    )


def test_promos_after_the_end_of_a_recovered_story_go():
    text = bare_extraction(masthead(), include_comments=False, with_metadata=False).text
    for sentence in STORY_SENTENCES:
        assert sentence in text
    assert "daily dose" not in text
    assert "news release" not in text


def test_story_text_after_an_inline_share_row_stays():
    inline = '<div class="share-inline"><a href="#">Share</a></div>'
    last = "Wall Street closed the session lower for a second straight day, traders said in New York on Tuesday."
    text = bare_extraction(masthead(between=inline, tail_inside=f"<p>{last}</p>"), include_comments=False).text
    assert last in text


# The share bar under a BuzzFeed quiz: a heading over a group of share links (Bluesky, Reddit, Pinterest...)
SHARE_BAR = (
    '<div class="share-bar-bottom_shareBottom"><h3 class="share-bar-bottom_shareCta">Share This Quiz</h3>'
    '<div role="group" aria-label="Share"><ul><li><a href="https://bsky.app/intent/compose?text=Quiz">Bluesky</a></li>'
    '<li><a href="https://www.reddit.com/submit?url=https%3A%2F%2Fnews.example.com%2Fquiz">Reddit</a></li>'
    '<li><a href="https://www.pinterest.com/pin/create/button/?url=x">Pinterest</a></li>'
    '<li><a href="https://t.me/share/url?url=x">Telegram</a></li></ul></div></div>'
)


def test_a_share_bar_goes_with_its_label_in_the_baseline():
    body = f"<article><p>{PARAGRAPH}</p><p>How many of these idioms can you finish?</p>{SHARE_BAR}</article>"
    _, text, _ = baseline(page(body=body))
    assert "How many of these idioms" in text
    for words in ("Share This Quiz", "Bluesky", "Reddit", "Pinterest", "Telegram"):
        assert words not in text


def test_a_sentence_holding_a_share_link_stays_in_the_baseline():
    sentence = "Readers can send the council their questions about the bridge through the form below"
    body = f'<article><p>{PARAGRAPH}</p><p>{sentence} or <a href="https://t.me/share/url?url=x">Telegram</a>.</p></article>'
    _, text, _ = baseline(page(body=body))
    assert sentence in text


# Investing News: the whole page column is the frame, and the story's own wrapper (div.widget) is named by
# a discard token, so pruning every match would gut the frame and the guard used to keep all of it.
def column(story_wrapper='class="widget post-partial"', extra=""):
    story = "".join(
        f"<p>{sentence} The company said drilling continues through the quarter at its flagship project.</p>"
        for sentence in (
            "Gran Tierra Energy today announced an operational and financial update for the quarter.",
            "The Charapa Norte-1 well is producing at an average rate of 1,188 barrels of oil per day.",
            "The Rose-1 well in Colombia is producing at an average rate of 305 barrels of oil per day.",
            "The company bought back 15 million shares of common stock for 20 million dollars.",
            "Exploration in the Oriente Basin of Ecuador produced oil from four independent prospects.",
        )
    )
    return page(
        body='<div class="main v-sep">'
        '<div class="topbar"><ul><li><span>WORLD EDITION</span></li><li>Australia</li><li>North America</li></ul></div>'
        f'<div {story_wrapper}><div class="widget__body clearfix"><p>The transaction would have created the '
        "world's second-largest gold miner.</p></div>"
        f'<div class="widget__body clearfix"><div class="body"><div class="body-description">{story}</div></div></div></div>'
        '<div class="author__modal-wrapper"><p>Giann Liguid is a graduate of Ateneo De Manila University with an AB '
        "in Interdisciplinary Studies.</p><p>Learn about our editorial policies.</p></div>"
        '<div class="column__right"><div class="innform--press--releases--sidebar"><h3>Latest News</h3>'
        "<p>Other company announces another drilling result in Nevada this week.</p></div></div>"
        '<div class="alert-wrapper"><div role="dialog" class="alert-popup is-hidden"><h3>Alert Options</h3>'
        "<p>No alert set</p></div></div>"
        f"{extra}</div>"
    )


def test_page_chrome_beside_a_story_named_by_a_discard_token_goes():
    text = bare_extraction(column(), include_comments=False, with_metadata=False).text
    assert "Charapa Norte-1 well is producing" in text
    assert "second-largest gold miner" in text
    for chrome in ("WORLD EDITION", "Ateneo De Manila", "editorial policies", "Latest News", "Alert Options"):
        assert chrome not in text


def test_a_story_wrapper_named_like_chrome_stays():
    text = bare_extraction(column(story_wrapper='class="mb-2 post--socialux"'), include_comments=False).text
    assert "Charapa Norte-1 well is producing" in text
    assert "WORLD EDITION" not in text


def test_content_blocks_named_widget_all_stay():
    # Elementor and watson keep content in several widget-classed blocks: none is chrome
    blocks = "".join(
        f'<div class="elementor-widget elementor-widget-text-editor"><p>{PARAGRAPH} Part {n} of the history '
        "of the network and its founders, written by the editors.</p></div>"
        for n in range(1, 5)
    )
    text = bare_extraction(page(body=f'<div class="main">{blocks}</div>'), include_comments=False).text
    for n in range(1, 5):
        assert f"Part {n} of the history" in text


def test_the_byline_line_before_a_recovered_story_goes():
    text = bare_extraction(masthead(), include_comments=False, with_metadata=False).text
    assert text.startswith(STORY_SENTENCES[0])
    assert "22 Apr 2026" not in text


def test_a_standfirst_before_a_recovered_story_stays():
    lead = "Wall Street fell for a second day as the ceasefire deadline neared and oil prices climbed again."
    html = masthead().replace("<p>Lola<br>22 Apr 2026</p>", f"<p>Lola<br>22 Apr 2026</p><p>{lead}</p>")
    text = bare_extraction(html, include_comments=False, with_metadata=False).text
    assert lead in text


# McPherson mastheads: the rest of the story waits, hidden, behind a notice a script replaces with it,
# and part of it is hidden again inside, after an ad slot.
LOCKED = [
    "This is not our preferred choice, we do not want this, the president said at his annual news conference.",
    "He said Russia had received a generally positive initial response to security proposals it handed over.",
    "The ball is in their court, they should answer us with something, he said of the talks in Geneva.",
]


def locked_page(notice="<p>Hold tight - we're checking permissions before loading more content</p>"):
    return page(
        body='<main><article><div class="article-body"><div class="content">'
        f"<div><p>{PARAGRAPH * 2}</p></div><div>{notice}</div>"
        f'<div id="lockable-1" style="display:none;"><p>{LOCKED[0]}</p>'
        "<div><script>googletag.cmd.push(function() { googletag.display('div-gpt-ad-MREC3'); });</script></div>"
        f'<div style="display:none;"><p>{LOCKED[1]}</p><p>{LOCKED[2]}</p></div></div>'
        "</div></div></article></main>"
    )


def test_the_story_behind_a_loading_notice_is_read_whole():
    text = bare_extraction(locked_page(), include_comments=False, with_metadata=False).text
    for sentence in LOCKED:
        assert sentence in text
    assert "Hold tight" not in text


def test_a_hidden_block_behind_a_subscription_link_stays_hidden():
    notice = '<p>Subscribers only: <a href="/subscribe">subscribe to keep reading</a></p>'
    tree = fromstring(locked_page(notice))
    reveal_hidden_continuation(tree)
    assert tree.xpath("//div[@id='lockable-1']")[0].get("style") == "display:none;"
    assert "Subscribers only" in tree.text_content()


# boredpanda: the story is rendered by a script; the page's own text is a newsletter modal (role=dialog)
NEWSLETTER_MODAL = (
    '<div id="more-categories-modal" class="modal fade" role="dialog"><div class="modal-dialog" role="document">'
    + "".join(
        f'<label class="category-option"><input type="checkbox" name="category[]"><div><span class="info">{name} '
        f"The week's finest content carefully selected to bring you the best articles directly, once per week.</span>"
        "</div></label>"
        for name in ("Weekly", "Daily", "Funny", "Animals", "Politics")
    )
    + "</div></div>"
)


STORY_BLOCK = "".join(f"<p>{' '.join(LOCKED)} {sentence}</p>" for sentence in STORY_SENTENCES)


def test_justext_leaves_a_dialog_out():
    tree = fromstring(f"<html><body>{NEWSLETTER_MODAL}<div>{STORY_BLOCK}</div></body></html>")
    texts = " ".join(text for text, *_ in extract_paragraphs(tree))
    assert "finest content" not in texts
    assert "annual news conference" in texts


def test_recall_recovery_leaves_the_site_menu_out():
    menu = (
        '<header class="header"><div class="mainNavContainer"><div class="ccpaCopy">US residents can opt out of '
        "sales of personal data.</div><div>Browse links</div><div>Press</div><div>Privacy</div></div></header>"
    )
    body = f'{menu}<main><div class="post"><p>How many of these songs do you know?</p></div></main>'
    text = bare_extraction(page(body=body), include_comments=False, with_metadata=False).text
    assert "opt out" not in text


def test_a_list_item_with_its_share_button_keeps_its_title_in_the_baseline():
    item = (
        '<div class="subbuzz"><h2>1. Raya and the Last Dragon</h2><figure><img src="https://img.example.com/1.jpg">'
        '</figure><div class="share"><a href="https://bsky.app/intent/compose?text=x">Bluesky</a></div></div>'
    )
    _, text, _ = baseline(page(body=f"<article><p>{PARAGRAPH}</p>{item}</article>"))
    assert "Raya and the Last Dragon" in text


def test_a_short_last_part_hidden_again_inside_the_story_is_read():
    signature = "Louis Cook | Numurkah"
    inner = f'<div style="display:none;"><p>{LOCKED[2]}</p><p>{signature}</p></div>'
    html = locked_page().replace(f'<div style="display:none;"><p>{LOCKED[1]}</p><p>{LOCKED[2]}</p></div>', inner)
    text = bare_extraction(html, include_comments=False, with_metadata=False).text
    assert LOCKED[2] in text
    assert signature in text


GUIDE_TOWNS = ["Patagonia", "Terlingua", "Taos", "Gunnison", "Escalante", "Cottonwood"]


def guide_paragraph(day, town):
    return (
        f"Day {day} starts in {town}, a quiet ranching town where the road follows an old rail line past dry creeks, "
        f"mesquite flats and a bar in {town} that still opens on weekends for the few travellers who stop. "
    )


def guide_item(day, town, trail):
    return (
        f"Hikers leaving {town} on day {day} reach summit trail number {trail} and its old fire lookout after a steady "
        f"climb of {trail + 3} hours, with views across three ranges toward the border. "
    )


def test_headings_and_lists_between_recovered_paragraphs_stay():
    # outsideonline.com road-trip guide: no body frame, so only the paragraphs are recovered, while readability
    # holds the stops' headings and lists between them
    stops = "".join(
        f"<h2>{day}. Route: to {town}</h2><h3>Distance: {day * 90} miles</h3><p>{guide_paragraph(day, town)}</p>"
        f"<h3>Adventures in {town}</h3><ul>"
        + "".join(f"<li>{guide_item(day, town, trail)}</li>" for trail in range(3))
        + "</ul>"
        for day, town in enumerate(GUIDE_TOWNS, 1)
    )
    body = (
        '<div class="article-template"><div class="body-content"><div>Published November 2, 2022 05:00AM</div></div>'
        f'<div class="o-wrap"><p>Heading into the desert at dusk (Photo: Example/Agency)</p>'
        f"<p>{guide_paragraph(0, 'Tucson')}</p>{stops}</div></div>"
    )
    text = bare_extraction(page(body=body), include_comments=False, with_metadata=False).text
    assert "Route: to Taos" in text
    assert "summit trail number 2" in text


def own_body(paragraphs):
    return fromstring("<div>" + "".join(f"<p>{text}</p>" for text in paragraphs) + "</div>")


def wrap_check(paragraphs, algo_html):
    algo_body = fromstring(f"<div>{algo_html}</div>")
    own = " ".join(paragraphs)
    return _wraps_own_extraction(own_body(paragraphs), algo_body, algo_body.text_content(), len(own), Extractor())


def test_readability_wrapping_the_own_story_in_chrome_does_not_win():
    paragraphs = [guide_paragraph(day, town) for day, town in enumerate(GUIDE_TOWNS)]
    menu = "".join(f"<li>{section} news and features</li>" for section in ("World", "Sport", "Travel", "Opinion") * 10)
    story = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    assert wrap_check(paragraphs, f"<ul>{menu}</ul>{story}<ul>{menu}</ul>")


def test_readability_holding_the_storys_headings_and_lists_between_the_own_paragraphs_wins():
    paragraphs = [guide_paragraph(day, town) for day, town in enumerate(GUIDE_TOWNS)]
    story = "".join(
        f"<p>{paragraph}</p><h3>Adventures in {town}</h3><ul>"
        + "".join(f"<li>{guide_item(day, town, trail)}</li>" for trail in range(3))
        + "</ul>"
        for (day, town), paragraph in zip(enumerate(GUIDE_TOWNS), paragraphs, strict=True)
    )
    assert not wrap_check(paragraphs, story)


def test_a_caption_or_two_between_the_own_paragraphs_is_still_a_wrap():
    paragraphs = [guide_paragraph(day, town) for day, town in enumerate(GUIDE_TOWNS)]
    story = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs[:3])
    story += "<ul><li>Photo: Example/Agency</li></ul><h4>Advertisement</h4>"
    story += "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs[3:])
    menu = "".join(f"<li>{section} news and features</li>" for section in ("World", "Sport", "Travel") * 12)
    assert wrap_check(paragraphs, f"<ul>{menu}</ul>{story}")


def test_a_quiz_question_and_its_answers_stay_when_the_share_bar_goes_in_the_baseline():
    answers = "".join(
        f'<li><label><input type="radio" name="season" value="{season}">{season}</label></li>'
        for season in ("Fall", "Winter", "Summer", "Spring")
    )
    question = f"<fieldset><legend>And finally, you drink it during...</legend><ul>{answers}</ul></fieldset>"
    _, text, _ = baseline(page(body=f"<article><p>{PARAGRAPH}</p>{question}{SHARE_BAR}</article>"))
    assert "you drink it during" in text
    assert "Winter" in text
    assert "Share This Quiz" not in text


# 929voice.fm, trimmed (life1051.org.au, riverlandlife.org.au, waggaslifefm.com, 943.com.au, pulse941.com.au and
# 1035fm.com.au run the same theme): the byline row holds the date, the category, the tags and the vcard credit
VOICE_FM_URL = "https://www.929voice.fm/news/what-it-means-to-be-an-emotionally-available-parent/"
VOICE_FM_DESK = "92.9 Voice FM Contributors"
VOICE_FM_HEAD = f'<meta name="author" content="{VOICE_FM_DESK}" />' + ld(
    {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "Article",
                "author": {"name": VOICE_FM_DESK, "@id": "https://www.929voice.fm/#/schema/person/ea36ee5d"},
                "headline": "What It Means to be An Emotionally Available Parent",
                "keywords": ["Articles", "emotions", "mums at the table", "parenting advice"],
                "articleSection": ["Parenting"],
            },
            {"@type": "Person", "@id": "https://www.929voice.fm/#/schema/person/ea36ee5d", "name": VOICE_FM_DESK},
        ],
    }
)


def voice_fm_story(credit):
    tags = " &middot; ".join(
        f'<a href="https://www.929voice.fm/news/tag/{slug}/" rel="tag">{name}</a>'
        for slug, name in (
            ("articles", "Articles"),
            ("emotions", "emotions"),
            ("mums-at-the-table", "mums at the table"),
            ("parenting-advice", "parenting advice"),
        )
    )
    byline = (
        '<p class="byline"><span>9 November 2026</span><span><a href="https://www.929voice.fm/news/category/parenting/"'
        f' rel="category tag">Parenting</a></span><span>{tags}</span><span class="author vcard">{credit} </span></p>'
    )
    return (
        '<article class="post type-post status-publish hentry category-parenting tag-articles tag-emotions">'
        '<section class="article-header"><h1 class="post-heading">What It Means to be An Emotionally Available'
        f' Parent</h1>{byline}</section><main role="main"><section class="entry-content" itemprop="text">'
        '<p>By: <a rel="nofollow noopener" href="http://tag/mums-at-the-table">Adriana Wales</a></p>'
        f"<p>{PARAGRAPH * 3}</p><p>{PARAGRAPH * 3}</p></section></main></article>"
    )


def test_tag_links_in_the_byline_row_are_no_authors():
    # the tags came out as the author "Emotions; Mums; Parenting Advice". The meta author and the JSON-LD
    # credit the station's desk; the story opens with its writer's credit, "By: Adriana Wales"
    html = page(VOICE_FM_HEAD, voice_fm_story(VOICE_FM_DESK), url=VOICE_FM_URL)
    assert extract_metadata(html).author == "Adriana Wales"
    assert bare_extraction(html, url=VOICE_FM_URL, with_metadata=True).author == "Adriana Wales"
    # the byline row the page marks as one stays: the desk's vcard
    assert extract_metadata(page(body=voice_fm_story(VOICE_FM_DESK))).author == VOICE_FM_DESK
    # a person in the vcard was glued to the tags ("· Anzac Day · · · Laura Bennett")
    assert extract_metadata(page(body=voice_fm_story("Laura Bennett"))).author == "Laura Bennett"


def bricks_meta_row(label, value):
    # 943.com.au and waggaslifefm.com (Bricks theme), trimmed
    return (
        '<div class="brxe-block fr-blog-post-delta__meta-wrapper"><div class="brxe-div fr-blog-post-delta__meta--author'
        f' fr-blog-post-delta__meta"><p class="brxe-text-basic fr-blog-post-delta__meta-title">{label}</p>'
        f'<div class="brxe-div fr-blog-post-delta__author">{value}</div></div><div class="brxe-div'
        ' fr-blog-post-delta__meta--date fr-blog-post-delta__meta"><p class="brxe-text-basic'
        ' fr-blog-post-delta__meta-title">Published on</p><time class="brxe-text-basic fr-blog-post-delta__date"'
        ' datetime="November 6, 2026">November 6, 2026</time></div></div>'
    )


def test_a_meta_items_label_without_its_value_is_no_author():
    widget = (
        '<div data-script-id="aoyhxx" class="brxe-post-reading-time" data-suffix=" minutes"'
        ' data-calculation-method="words" data-wpm="200"></div>'
    )
    story = f"<h1>Strong Until I Wasn't</h1>{bricks_meta_row('Reading time', widget)}<p>{PARAGRAPH * 3}</p>"
    assert extract_metadata(page(body=story)).author is None
    # the same row holding a name
    story = f"<h1>Strong Until I Wasn't</h1>{bricks_meta_row('Written by', 'Jane Doe')}<p>{PARAGRAPH * 3}</p>"
    assert extract_metadata(page(body=story)).author == "Jane Doe"


def test_a_stations_frequency_stays_in_its_credit():
    # life1051.org.au's vcard was cut at the number: "Life", and "Parenting Life" with a tag before it
    assert normalize_authors(None, "Life 105.1 Contributors") == "Life 105.1 Contributors"
    assert extract_metadata(page(body=voice_fm_story("Life 105.1 Contributors"))).author == "Life 105.1 Contributors"
    assert normalize_authors(None, "92.9 Voice FM Contributors") == "92.9 Voice FM Contributors"
    # a number after the name still ends it: a date, a time, a reading time
    assert normalize_authors(None, "Ross Guest 1 October 2025 22 1") == "Ross Guest"
    assert normalize_authors(None, "von Ulrike 18.10.2019 16:06 Uhr") == "Ulrike"
    assert normalize_authors(None, "Jane Doe 2.30pm") == "Jane Doe"
    assert normalize_authors(None, "Sunrise The Nightly 2 Min Read Updated Comments") == "Sunrise The Nightly"


def test_a_tag_link_inside_the_author_element_stays_the_byline():
    # elpais.cr: WordPress marks a byline taxonomy's terms rel="tag" too
    byline = (
        '<div class="tdb-author-name-wrap"><span class="tdb-author-by">Por</span> <a class="tdb-author-name"'
        ' href="https://www.elpais.cr/author/csalazar/"><a href="https://www.elpais.cr/byline/elpais-cr/" rel="tag">'
        "Elpais.cr</a></a></div>"
    )
    assert extract_metadata(page(body=byline + f"<p>{PARAGRAPH}</p>")).author == "Elpais cr"
    byline = '<p class="byline">By <a href="/tag/jane-doe/" rel="author tag">Jane Doe</a></p>'
    assert extract_metadata(page(body=byline + f"<p>{PARAGRAPH}</p>")).author == "Jane Doe"
    byline = '<p class="byline">By <span class="author vcard"><a href="/writer/jane-doe/" rel="tag">Jane Doe</a></span></p>'
    assert extract_metadata(page(body=byline + f"<p>{PARAGRAPH}</p>")).author == "Jane Doe"


def story(credit_html, head="", headline="New Council to take Dorset out of neutral", tag="h1"):
    return page(head, f"<{tag}>{headline}</{tag}>{credit_html}<p>{PARAGRAPH * 3}</p>")


def test_the_credit_a_story_opens_with_names_its_writer():
    # North Eastern Advertiser: the meta author is the production desk
    desk = '<meta name="author" content="Production North-Eastern Advertiser">'
    assert extract_metadata(story("<p><strong>By Rachel Williams</strong></p>", desk)).author == "Rachel Williams"
    # its author block names the same desk, and the scraper's blacklist holds it
    desk_block = '<div class="author">Production North-Eastern Advertiser</div><p><strong>By Rachel Williams</strong></p>'
    blacklist = {"Production North-Eastern Advertiser"}
    assert extract_metadata(story(desk_block), author_blacklist=blacklist).author == "Rachel Williams"
    # Sydney Sun: the name over the dateline under the headline, in one block
    head = '<meta property="og:title" content="CFTC allows single-stock perpetual futures to begin trading">'
    title = "CFTC allows single-stock perpetual futures to begin trading"
    credit = "<p>Anabelle Colaco <br>09 Nov 2026, 04:06 GMT+10 </p>"
    assert extract_metadata(story(credit, head, title, "h2")).author == "Anabelle Colaco"
    # a byline the page marks as one stays, and co-authors the meta adds are kept
    marked = '<p class="byline">Jane Doe</p><p>By John Roe</p>'
    assert extract_metadata(story(marked)).author == "Jane Doe"
    both = '<meta name="author" content="Natalie Musumeci, Stephen Council">'
    assert extract_metadata(story('<p>By <a href="/author/nm">Natalie Musumeci</a></p>', both)).author == (
        "Natalie Musumeci; Stephen Council"
    )


def test_no_credit_from_labels_brands_or_story_text():
    # a one-word credit over the dateline is a brand or an agency ("Packers", "ANI")
    head = '<meta property="og:title" content="Packers host Eagles at Lambeau Field">'
    credit = "<p>Packers <br>06 Nov 2025, 02:37 GMT+10 </p>"
    assert extract_metadata(story(credit, head, "Packers host Eagles at Lambeau Field", "h2")).author is None
    # a label over a labelled date ("Ort" and "Datum" on bsi.bund.de) is no credit
    label = "<p>Ort Saarbrücken<br>Datum 28.01.2022</p>"
    assert extract_metadata(story(label)).author is None
    # a by-line linking only within the page ("No Comments") or to a handle beside a role names no one
    empty = '<p><span class="author-leading">By</span> <a href="https://pakmag.com.au/gift-guide/#respond">No Comments</a></p>'
    assert extract_metadata(story(empty)).author is None
    handle = '<p>By <a href="/users/elanii">Elanii</a> Community Contributor</p>'
    assert extract_metadata(story(handle)).author is None
    # past the story's first sentence, a "By" line is the text's
    late = f"<p>{PARAGRAPH}</p><p>By John Roe</p>"
    assert extract_metadata(story(late)).author is None


def test_a_teaser_for_another_story_is_no_byline():
    # North East Media (Atex): the "most popular" cards carry the class "with-byline"; the story has none
    card = (
        '<div class="jsx-2373710916 root with-byline"><div class="teaser"><div class="teaser-content">'
        '<span class="teaser-keyword"><span class="keyword-text"><span>Digital Editions</span></span></span>'
        '<h3 class="teaser-title"><a href="/digital-editions/wangaratta-chronicle-friday-6-november-2026-r8pwu5jy">'
        "Wangaratta Chronicle, Friday, 6 November, 2026</a></h3></div></div></div>"
    )
    head = ld({"@context": "https://schema.org", "@type": "NewsArticle", "author": {"@type": "Person", "name": ""}})
    title = '<h1 class="article-title">Probus clubs a hive of activity</h1>'
    html = page(head, f"{title}<p>{PARAGRAPH * 3}</p><h2>Most popular stories</h2>{card}")
    assert extract_metadata(html).author is None
    # a card inside the link to its story (McPherson Media's most-read teasers, refinery29)
    card = (
        '<a class="teaser-link" href="/news/two-dead"><div class="teaser__container"><h3 class="teaser__title">'
        'Two dead in crash</h3><div class="teaser__footer"><div class="teaser__author">By Ivy Jensen</div></div></div></a>'
    )
    assert extract_metadata(page(body=f"<h1>Basin issues need resolving</h1><p>{PARAGRAPH}</p>{card}")).author is None
    # an author box whose heading links to the author's page is the byline's own
    box = '<div class="author-box"><h4 class="author-name"><a href="/author/jane-doe">Jane Doe</a></h4></div>'
    assert extract_metadata(page(body=f"<h1>Basin issues need resolving</h1><p>{PARAGRAPH}</p>{box}")).author == ("Jane Doe")


def test_a_syndicator_or_a_role_after_the_comma_is_no_second_author():
    # North East Media's motoring column and school pieces
    assert normalize_authors(None, "By DEREK OGDEN, MARQUE MOTORING") == "Derek Ogden"
    assert normalize_authors(None, "JENNY GOUGH, year three teacher, Mansfield Primary School") == "Jenny Gough"
    # co-authors stay
    assert normalize_authors(None, "Natalie Musumeci, Stephen Council") == "Natalie Musumeci; Stephen Council"


@pytest.mark.parametrize("meta_author", [None, "Site Admin"])
def test_the_story_credit_is_read_under_a_headline_inside_a_block_bylines_skip(meta_author):
    # the byline and the credit leave the same elements out; the credit still starts from the headline,
    # also when a block that bylines skip (class "title") holds it
    head = "<title>Bridge opens next month</title>"
    if meta_author:
        head += f'<meta name="author" content="{meta_author}">'
    body = f'<div class="title"><h1>Bridge opens next month</h1></div><p>By Jane Doe</p><p>{PARAGRAPH * 2}</p>'
    assert extract_metadata(page(head, body)).author == "Jane Doe"


@pytest.mark.parametrize(
    ("byline", "author"),
    [
        # a function word that is also a personal name is no sign of a sentence in a byline written as a name
        ("Do My Linh", "Do My Linh"),
        ("Thi Kim Anh Do", "Thi Kim Anh Do"),
        ("Will Van Der Merwe", "Will Van Der Merwe"),
        ("CNN's Will Ripley", "CNN's Will Ripley"),
        ("Maria João do Carmo", "Maria João do Carmo"),
        # a phrase is none: other function words, lower case, or three such words
        ("You are going", None),
        ("On The Attack", None),
        ("So You Think You Can Dance", None),
        ("W willfaulkner profile photo Added By Will", None),
    ],
)
def test_a_name_may_hold_words_that_are_also_function_words(byline, author):
    assert normalize_authors(None, byline) == author


@pytest.mark.parametrize(
    ("byline", "author"),
    [
        # the surname is no job title: the name starts after the credit words
        ("words: Jennie Officer", "Jennie Officer"),
        ("Story by Jane Doe Political editor", "Jane Doe"),
        ("By Staff Writer", "Staff Writer"),
        ("Tom McIlroy Political editor", "Tom McIlroy"),
        # after an outlet's possessive the title starts at once
        ("CNN's Chief Media Analyst Brian Stelter", "CNN's Brian Stelter"),
        ("From CNN's Chief Media Analyst Brian Stelter", "CNN's Brian Stelter"),
    ],
)
def test_a_job_title_is_read_after_the_credit_words(byline, author):
    assert normalize_authors(None, byline) == author


@pytest.mark.parametrize(
    ("byline", "author"),
    [
        # "and" inside a job title joins no second person
        ("Lesley Thomas, Weekend and Beauty Editor", "Lesley Thomas"),
        ("Phoebe McDowell, Property and Interiors Director", "Phoebe McDowell"),
        # a person after the title is a second credit
        ("Sarah Martin, Chief political correspondent and Paul Karp", "Sarah Martin; Paul Karp"),
    ],
)
def test_and_inside_a_job_title_credits_no_one_else(byline, author):
    assert normalize_authors(None, byline) == author


@pytest.mark.parametrize(
    ("byline", "author"),
    [
        # "with" joins two people
        ("Reged Ahmad with Richard Luscombe", "Reged Ahmad; Richard Luscombe"),
        ("Danny Kemp with Anuj Chopra in Riyadh", "Danny Kemp; Anuj Chopra"),
        ("Arthur Connan with Marie-Morgane LE MOEL", "Arthur Connan; Marie-Morgane LE MOEL"),
        ("Victoria Lavelle, with Guillaume Souvant", "Victoria Lavelle; Guillaume Souvant"),
        # before anything else it leads in what is no author
        ("Jane Doe with Reuters", "Jane Doe"),
        ("In paid partnership with Vypr", "In paid partnership"),
        ("Leslie Fauvel with AFP bureaus in Abidjan", "Leslie Fauvel"),
        ("AFP with Fox Sports", "AFP"),
    ],
)
def test_with_joins_two_people_only_before_a_person(byline, author):
    assert normalize_authors(None, byline) == author


@pytest.mark.parametrize("fast", [True, False])
def test_the_credit_under_each_picture_is_no_copyright_notice(fast):
    # a gallery: a number, the picture's text and its credit, again and again; a notice names a year
    gallery = "".join(f"<h3>#{n}</h3><p>{PARAGRAPH}Picture {n} shows it.</p><p>© Photo: reader{n}</p>" for n in range(1, 7))
    url = "https://www.example.com/gallery"
    body = f"<article><h1>The best bridge pictures</h1>{gallery}<p>© 2026 Example News. All rights reserved.</p></article>"
    text = bare_extraction(page(body=body, url=url), url=url, fast=fast).text
    assert text.count("© Photo: reader") == 6
    assert "All rights reserved" not in text


@pytest.mark.parametrize(("min_size", "paragraphs"), [(0, 1), (250, 2)])
def test_the_last_lines_stay_when_too_little_would_be_left(min_size, paragraphs):
    # a story of 200 characters stands on its last lines: without them the extraction is discarded
    story = "The fairy garden behind the glassblowing studio opened to visitors on Saturday morning. " * 2
    tree = fromstring(f"<div><p>{story}</p><p>© 2026 Example News. All rights reserved.</p></div>")
    prune_trailing_calls(tree, "https://news.example.com/story", min_size)
    assert len(tree.findall(".//p")) == paragraphs


def test_a_sentence_under_a_related_label_is_the_storys():
    # a teaser is a headline; the sentence after the related box closes the story
    last = "The opening is very different from the delays we covered in February, when the works stopped for a month."
    tail = (
        '<p>Related</p><p><a href="/other-story">Council approves the second bridge</a></p>'
        "<p>The opening is very different from the delays "
        '<a href="/delays">we covered in February</a>, when the works stopped for a month.</p>'
    )
    text = story_text(tail)
    assert text.endswith(last)
    assert "Council approves the second bridge" not in text


@pytest.mark.parametrize(
    "before",
    [
        '<h2 class="subheadline"><strong>A strange birthday</strong></h2>',
        "<div><ul><li>Broadford</li><li>Tallarook</li></ul></div>",
    ],
)
def test_what_precedes_a_part_hidden_again_inside_the_story_stays(before):
    # the notice stands in for the text in the visible page; inside the revealed story nothing does
    ad = "<div><script>googletag.cmd.push(function() { googletag.display('div-gpt-ad-MREC3'); });</script></div>"
    text = bare_extraction(locked_page().replace(ad, before), include_comments=False, with_metadata=False).text
    for kept in re.findall(r">([^<>]+)<", before):
        assert kept in text
    for sentence in LOCKED:
        assert sentence in text
    assert "Hold tight" not in text


def test_a_colon_opens_a_subtitle_not_the_branding():
    head = '<meta property="og:title" content="Coast v. Council: how do they compare?">'
    head += ld({"@context": "https://schema.org", "@type": "NewsArticle", "headline": "Coast v. Council"})
    assert extract_metadata(page(head)).title == "Coast v. Council: how do they compare?"
