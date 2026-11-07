"""Regressions from production publisher audits (titles, bylines, bodies)."""

import json
import re

import pytest
from lxml.html import fromstring

from trafilatura import bare_extraction, extract_metadata
from trafilatura.htmlprocessing import is_call_to_action, prune_trailing_calls
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
