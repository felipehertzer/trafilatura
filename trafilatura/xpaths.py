# pylint:disable-msg=E0611
"""
X-Path expressions used to extract or filter the main text content,
and to extract metadata.
"""

import re
from collections.abc import Callable
from typing import Any

from lxml.etree import Element, XPath
from lxml.html import HtmlElement

REGEXP_NS = "http://exslt.org/regular-expressions"
# re:test() is a Python callback even on a missing attribute: "(@id and re:test(@id, ...))" skips
# it (up to -30% per expression). Left out where it measured slower (COMMENTS_XPATH[0:2], PRECISION_DISCARD_XPATH[1]).


def _alt(tokens: tuple[str, ...]) -> str:
    "Join concept tokens into a regex alternation; reject an empty group (would match every element)."
    if not tokens:  # not an assert: must hold under python -O
        raise ValueError("empty token group would make re:test(...) match everything")
    return "|".join(tokens)


### 1. CONTENT

# Content-area token vocabulary for BODY_XPATH, grouped per stage, composed via _alt() into the
# re:test(...) alternations below.
_ARTICLE_CONTENT_ID_TOKENS = (
    "(?:entry|article|art)-content",
    "article__content",
    "article(?:-|__)?body",
    "articleBody",
    "body-text",
)
_ARTICLE_CONTENT_CLASS_TOKENS = (
    "post[-_]text",
    "post-body",
    "post-?entry",
    "post[-_]?content",
    "postContent",
    "post_inner_wrapper",
    "article-?text",
    "articleText",
    "(?:entry|page|text|article|art)-content",
    "article__content",
    "article(?:-|__)?body",
    "articleBody",
    "ArticleContent",
    "body-text",
    "article__container",
)
_STORY_ID_TOKENS = ("^primary", "story-body")
_STORY_CLASS_TOKENS = (
    "^article ",
    "post-bodycopy",
    "story-?content",
    "(?:theme|blog|section|single)-content",
    "single-post",
    "main-column",
    "wpb_text_column",
    "story-body",
    "field-body",
)
_MAIN_CONTENT_ID_TOKENS = ("content-main", "content-body", "contentBody")
_MAIN_CONTENT_CLASS_TOKENS = ("content[-_]main", "content(?:-|__)body")

BODY_XPATH = [
    XPath(
        f"""
        .//*[self::article or self::div or self::main or self::section][
        @class='post' or @class='entry' or
        @itemprop='articleBody' or @id='articleContent' or
        (@id and re:test(@id, '{_alt(_ARTICLE_CONTENT_ID_TOKENS)}')) or
        (@class and re:test(@class, '{_alt(_ARTICLE_CONTENT_CLASS_TOKENS)}'))
        ][1]
        """,
        namespaces={"re": REGEXP_NS},
    ),
    # (…)[1] = first occurrence
    XPath("(.//article)[1]"),
    XPath(
        f"""
        (.//*[self::article or self::div or self::main or self::section][
        @role='article' or
        @id='article' or @id='story' or
        @class='postarea' or @class='art-postcontent' or @class='text' or @class='cell' or @class='story' or
        (@id and re:test(@id, '{_alt(_STORY_ID_TOKENS)}')) or
        (@class and re:test(@class, 'fulltext', 'i')) or
        (@class and re:test(@class, '{_alt(_STORY_CLASS_TOKENS)}'))
        ])[1]
        """,
        namespaces={"re": REGEXP_NS},
    ),
    XPath(
        f"""
        (.//*[self::article or self::div or self::main or self::section][
        @id='content' or @class='content' or
        (@id and re:test(@id, '{_alt(_MAIN_CONTENT_ID_TOKENS)}')) or
        (@class and re:test(@class, '{_alt(_MAIN_CONTENT_CLASS_TOKENS)}')) or
        contains(translate(@id, 'CM','cm'), 'main-content') or contains(translate(@class, 'CM','cm'), 'main-content') or
        contains(translate(@class, 'CP','cp'), 'page-content')
        ])[1]
        """,
        namespaces={"re": REGEXP_NS},
    ),
    XPath(
        """
        (.//*[self::article or self::div or self::section][
        starts-with(@class, 'main') or starts-with(@id, 'main') or starts-with(@role, 'main')])[1]|(.//main)[1]
        """
    ),
]
# starts-with(@id, "article") or
# or starts-with(@id, "story") or contains(@class, "story")
# starts-with(@class, "content ") or contains(@class, " content")
# '//div[contains(@class, "text") or contains(@class, "article-wrapper") or contains(@class, "content-wrapper")]',
# '//div[contains(@class, "article-wrapper") or contains(@class, "content-wrapper")]',
# |//*[self::article or self::div or self::main or self::section][contains(@class, "article") or contains(@class, "Article")]
# @id="content"or @class="content" or @class="Content"
# or starts-with(@class, 'post ')
# './/span[@class=""]', # instagram?


COMMENTS_XPATH = [
    XPath(
        """
        .//*[self::div or self::list or self::section][
        re:test(@id|@class, 'comment-?list') or
        re:test(@class, 'comment-page|comments-content|post-comments')]
        """,
        namespaces={"re": REGEXP_NS},
    ),
    XPath(
        """
        .//*[self::div or self::section or self::list][
        re:test(@id|@class, '^comment[s-]') or
        re:test(@class, '^Comments|article-comments')]
        """,
        namespaces={"re": REGEXP_NS},
    ),
    XPath(
        """
        .//*[self::div or self::section or self::list][
        (@id and re:test(@id, '^(?:comol|disqus_thread|dsq-comments)'))]
        """,
        namespaces={"re": REGEXP_NS},
    ),
    XPath(
        """
        .//*[self::div or self::section][
        starts-with(@id, 'social') or contains(@class, 'comment')]
        """
    ),
]
# or contains(@class, 'Comments')

_COMMENTS_ID = "[Cc]omment(?!ary)|comol"
# CSS-module names keep the class name after a hex hash ("_0a7ca1bb3077d74a-commentsPreviewList",
# Nine's mastheads): the name is matched as if it began the class
_COMMENTS_CLASS = (
    "^[Cc]omment(?!ary)|(?:^|\\s)_?[0-9a-f]{8,}-[Cc]omment(?!ary)"
    "|(?:^|\\s)(?!(?:has|no|with|show|open|closed)-)[\\w]+-comments(?:\\s|$)"
)
# Viafoura comment widgets: a class token starting "viafoura", or custom <vf-*>
# elements. Future plc marks its article wrapper with a feature class
# "flw-article-viafoura", which a substring match removed with the article.
_VIAFOURA_XPATH = XPath(
    ".//*[re:test(@class, '(?:^|\\s)viafoura') or starts-with(local-name(), 'vf-')]",
    namespaces={"re": REGEXP_NS},
)
REMOVE_COMMENTS_XPATH = [
    XPath(
        f"""
        .//*[self::div or self::list or self::section or self::details][
        (@id and re:test(@id, '^(?:{_COMMENTS_ID}|disqus_thread|dsq-comments)')) or
        (@class and re:test(@class, '{_COMMENTS_CLASS}'))]
        """,
        namespaces={"re": REGEXP_NS},
    ),
    _VIAFOURA_XPATH,
]
# plus ol/ul comment lists, without the disqus ids, in one tree walk
REMOVE_COMMENTS_AND_LISTS_XPATH = [
    XPath(
        f"""
        .//*[self::div or self::list or self::section or self::details or self::ol or self::ul][
        (@id and re:test(@id, '^(?:{_COMMENTS_ID}|disqus_thread|dsq-comments)')) or
        (@class and re:test(@class, '{_COMMENTS_CLASS}'))][
        not(self::ol or self::ul) or (@id and re:test(@id, '^(?:{_COMMENTS_ID})')) or (@class and re:test(@class, '{_COMMENTS_CLASS}'))][
        not(.//h1)]
        """,
        namespaces={"re": REGEXP_NS},
    ),
    _VIAFOURA_XPATH,
]
# or self::span
# or contains(@class, 'comment') or contains(@id, 'comment')

# Infinite-scroll containers, whatever they hold: core.prune_appended_articles() keeps the
# article marked with the page's permalink in any of them and drops its appended siblings.
APPENDED_ARTICLES_XPATH = [
    XPath(
        """
        .//*[self::div or self::section or self::aside][
        (@id and re:test(@id, 'mvp-post-add-(?:box|wrap)|infinite-?scroll')) or
        (@class and re:test(@class, 'mvp-post-add-(?:box|wrap)|infinite-?scroll'))]
        """,
        namespaces={"re": REGEXP_NS},
    )
]

# Raw-tree prune, kept out of OVERALL_DISCARD_XPATH because its over-pruning guard would revert it.
RAW_TREE_PRUNE_XPATH = [
    # Keep an infinite-scroll shell holding an h1, or the first h2 if no h1
    # exists in the document. Always remove Zox News follow-up boxes.
    XPath(
        """
        .//*[self::div or self::section or self::aside][
        (@id and re:test(@id, 'mvp-post-add-(?:box|wrap)|infinite-?scroll')) or
        (@class and re:test(@class, 'mvp-post-add-(?:box|wrap)|infinite-?scroll'))][
        (@id and re:test(@id, 'mvp-post-add-(?:box|wrap)')) or
        (@class and re:test(@class, 'mvp-post-add-(?:box|wrap)')) or
        (not(.//h1) and not(.//h2[not(//h1) and not(preceding::h2)]))]
        """,
        namespaces={"re": REGEXP_NS},
    ),
    # OVERALL_DISCARD_XPATH's "share-" token matches these already, but its "widget" token
    # also matches the article body ("elementor-widget")
    XPath(".//*[self::div or self::section or self::aside][contains(@class, 'elementor-share-buttons')]"),
]
# On a page without an infinite-scroll container (APPENDED_ARTICLES_XPATH finds none), the container
# rule above has nothing to decide on: the rest of the list is enough.
RAW_TREE_PRUNE_NO_CONTAINER_XPATH = RAW_TREE_PRUNE_XPATH[1:]


# OVERALL_DISCARD_XPATH boilerplate token vocabulary, grouped by concept, composed via _alt()
# below. _LEGACY_SITE_* = single-site/legacy tokens (provenance + per-token audit in memory).
_SHARE_SOCIAL_ID_CLASS_TOKENS = ("^shar", "social", "viral")
_NEWSLETTER_ID_CLASS_TOKENS = ("newsletter", "syndication")
_CONSENT_ID_CLASS_TOKENS = ("cookie",)
_TAGS_ID_CLASS_TOKENS = ("tags",)
_UI_CHROME_ID_CLASS_TOKENS = ("sidebar", "banner", "bread-?crumb", "button")
_AUTHOR_ID_CLASS_TOKENS = ("author",)

_LEGACY_SITE_ID_TOKENS = ("^(?:jp-|dpsp-content)", "bmdh")
_FOOTER_ID_TOKENS = ("footer", "Footer")
_SHARE_ID_TOKENS = ("share", "Share")
_NAV_ID_TOKENS = ("nav", "Nav", "menu")
_RELATED_ID_TOKENS = ("related",)
_UI_CHROME_ID_TOKENS = ("message-container",)
_PAYWALL_ID_TOKENS = ("premium",)
_AD_ID_TOKENS = ("^ad-",)

_NAV_CLASS_TOKENS = ("^(?:nav|post-nav|ZendeskForm)", "subnav", "avigation", "navbar", "navbox", "menu", "bar")
_AD_CLASS_TOKENS = (
    " ad ",
    "-ad-",
    "(?:^| )ad-",
    "dfp[Aa]d",
    # the class "adv" alone: "advert-placement" wrappers hold the story between their slots
    "(?:^| )adv(?:$| )",
    "outbrain",
    "taboola",
    "criteo",
    "paid-?content",
    "widget",
)
_FOOTER_CLASS_TOKENS = ("footer", "Footer")
_AUTHOR_CLASS_TOKENS = ("byline", "Byline")
_SHARE_CLASS_TOKENS = ("share-", "sociable", "embedded", "embed")
_TAGS_CLASS_TOKENS = ("tag-list",)
_CONSENT_CLASS_TOKENS = ("consent", "modal-content", "permission")
_RELATED_CLASS_TOKENS = ("elated", "next-", "-stories", "most-popular", "more-on")
_UI_META_CLASS_TOKENS = (
    "meta",
    "rating",
    "attachment",
    "timestamp",
    "user-info",
    "user-profile",
    "-icon",
    "article-infos",
    "message-container",
    "slide",
    "viewport",
    "overlay",
)
_MISC_CLASS_TOKENS = ("options", "expand", "obfuscated", "blurred")
_LEGACY_SITE_CLASS_TOKENS = ("mol-factbox", "yin", "zlylin", "nfoline")

# id/class concept tokens, checked on BOTH attributes in the xpath below. NOTE re:test(@id|@class,...)
# tests only the SOURCE-FIRST attribute (XPath string(node-set)=first node), so each is applied
# per-attribute. 'cookie' (_CONSENT_ID_CLASS_TOKENS) is deliberately kept first-attr-only (via the
# @id|@class clause): pages ABOUT cookies carry the token on real content, so both-attr over-discards.
_OVERALL_DISCARD_BOTH_TOKENS = (
    _SHARE_SOCIAL_ID_CLASS_TOKENS
    + _NEWSLETTER_ID_CLASS_TOKENS
    + _TAGS_ID_CLASS_TOKENS
    + _UI_CHROME_ID_CLASS_TOKENS
    + _AUTHOR_ID_CLASS_TOKENS
)
_OVERALL_DISCARD_ID_TOKENS = (
    _LEGACY_SITE_ID_TOKENS
    + _FOOTER_ID_TOKENS
    + _SHARE_ID_TOKENS
    + _NAV_ID_TOKENS
    + _RELATED_ID_TOKENS
    + _UI_CHROME_ID_TOKENS
    + _PAYWALL_ID_TOKENS
    + _AD_ID_TOKENS
)
_OVERALL_DISCARD_CLASS_TOKENS = (
    _NAV_CLASS_TOKENS
    + _AD_CLASS_TOKENS
    + _FOOTER_CLASS_TOKENS
    + _AUTHOR_CLASS_TOKENS
    + _SHARE_CLASS_TOKENS
    + _TAGS_CLASS_TOKENS
    + _CONSENT_CLASS_TOKENS
    + _RELATED_CLASS_TOKENS
    + _UI_META_CLASS_TOKENS
    + _MISC_CLASS_TOKENS
    + _LEGACY_SITE_CLASS_TOKENS
)

# The clear page chrome among those tokens: navigation and bars, sidebars, banners, breadcrumbs, author
# boxes, footers, share and social rows, related stories, newsletters, tags, consent notices and ads. The
# others (widget, meta, slide, embed, options ...) also name content wrappers on some sites. When the
# over-pruning guard would keep a frame whole, prune_unwanted_nodes() still removes the clear chrome
# beside the frame's text (htmlprocessing.prune_beside_content).
CHROME_CLASS = re.compile(
    _alt(
        _SHARE_SOCIAL_ID_CLASS_TOKENS
        + _NEWSLETTER_ID_CLASS_TOKENS
        + _CONSENT_ID_CLASS_TOKENS
        + _TAGS_ID_CLASS_TOKENS
        + ("sidebar", "banner", "bread-?crumb")
        + _AUTHOR_ID_CLASS_TOKENS
        + _NAV_CLASS_TOKENS
        + tuple(t for t in _AD_CLASS_TOKENS if t != "widget")
        + _FOOTER_CLASS_TOKENS
        + _AUTHOR_CLASS_TOKENS
        + _SHARE_CLASS_TOKENS[:2]
        + _TAGS_CLASS_TOKENS
        + _CONSENT_CLASS_TOKENS
        + _RELATED_CLASS_TOKENS
    )
)
CHROME_ID = re.compile(
    _alt(
        _SHARE_SOCIAL_ID_CLASS_TOKENS
        + _NEWSLETTER_ID_CLASS_TOKENS
        + _TAGS_ID_CLASS_TOKENS
        + ("sidebar", "banner", "bread-?crumb")
        + _AUTHOR_ID_CLASS_TOKENS
        + _FOOTER_ID_TOKENS
        + _SHARE_ID_TOKENS
        + _NAV_ID_TOKENS
        + _RELATED_ID_TOKENS
        + _AD_ID_TOKENS
    )
)

OVERALL_DISCARD_XPATH = [
    XPath(
        f"""
        .//*[self::div or self::item or self::list or self::p or self::section or self::span][
        @data-lp-replacement-content or
        contains(translate(@role, 'N', 'n'), 'nav') or
        contains(@data-component, 'MostPopularStories') or
        ((@id or @class) and re:test(@id|@class, '{_alt(_CONSENT_ID_CLASS_TOKENS)}')) or
        (@id and re:test(@id, '{_alt(_OVERALL_DISCARD_BOTH_TOKENS + _OVERALL_DISCARD_ID_TOKENS)}')) or
        (@class and re:test(@class, '{_alt(_OVERALL_DISCARD_BOTH_TOKENS + _OVERALL_DISCARD_CLASS_TOKENS)}'))]
        """,
        namespaces={"re": REGEXP_NS},
    ),
    XPath(
        """
        .//*[@class='comments-title' or
        starts-with(@id|@class, 'reply-') or
        ((@id or @style) and re:test(@id|@style, 'hidden')) or
        contains(@style, 'display:none') or contains(@style, 'display: none') or
        (@id and re:test(@id, 'reader-comments|akismet')) or
        (@class and re:test(@class, '^hide-|comments-title|nocomments|-reply-|message|akismet|suggest-links|-hide-|hide-print| hidden| hide|noprint|notloaded')) or @aria-hidden='true']
        """,
        namespaces={"re": REGEXP_NS},
    ),
    # related-links blocks set as quotes ("wp-block-related-links")
    XPath(f".//quote[re:test(@class, '{_alt(_RELATED_CLASS_TOKENS[:1])}')]", namespaces={"re": REGEXP_NS}),
]

# conflicts:
# contains(@id, "header") or contains(@class, "header") or
# class contains "cats" (categories, also tags?)
# or contains(@class, "hidden ")  or contains(@class, "-hide")
# or contains(@class, "paywall")
# contains(@class, "content-info") or contains(@class, "content-title")
# contains(translate(@class, "N", "n"), "nav") or
# contains(@class, "panel") or
# or starts-with(@id, "comment-")


# the following conditions focus on extraction precision
TEASER_DISCARD_XPATH = [
    XPath(
        """
    .//*[self::div or self::item or self::list or self::p or self::section or self::span][
    contains(translate(@id, 'T', 't'), 'teaser') or contains(translate(@class, 'T', 't'), 'teaser')]
    """
    )
]


PRECISION_DISCARD_XPATH = [
    XPath(""".//header"""),
    # 'link' matched as a whole class token, not a substring (that dropped permalink/headline-link/
    # etc.); still drops class="link" (guarded by test_precision_recall). 'border' = substring.
    # 'bottom' must start or end a token: page-bottom chrome is named 'bottom'/'bottom-bar'/
    # 'article-bottom', whereas a mid-token 'bottom' is a CSS spacing utility
    # ('Padding-bottom-lg-30', 'border-bottom-0') that layout wrappers put on real content.
    XPath(
        r"""
    .//*[self::div or self::item or self::list or self::p or self::section or self::span][
    re:test(@id|@class, '(^|\s)bottom|bottom(\s|$)') or re:test(@id|@class, '(^|\s)link(\s|$)') or
    contains(@style, 'border')]
    """,
        namespaces={"re": REGEXP_NS},
    ),
]
# or contains(@id, "-comments") or contains(@class, "-comments")


DISCARD_IMAGE_ELEMENTS = [
    XPath(
        """
    .//*[self::div or self::item or self::list or self::p or self::section or self::span][
    contains(@id, 'caption') or contains(@class, 'caption')]
    """
    )
]


COMMENTS_DISCARD_XPATH = [
    XPath(""".//*[self::div or self::section][starts-with(@id, 'respond')]"""),
    XPath(""".//cite|.//quote"""),
    XPath(
        """
        .//*[
        @class='comments-title' or
        contains(@style, 'display:none') or
        (@class and re:test(@class, 'comments-title|nocomments|-reply-|message|signin')) or
        ((@id or @class) and re:test(@id|@class, '^reply-|akismet'))]
        """,
        namespaces={"re": REGEXP_NS},
    ),
]


# comment text held directly in divs
COMMENTS_DIV_TEXT_XPATH = XPath(".//div[not(.//div or .//p or .//list)][string-length(normalize-space()) >= 80]")


### 2. METADATA


# An author box's prose about the writer ("Our eclectic team of writers from around
# Australia - and some beyond.") is no byline, nor is an element holding it: the name has
# an element of its own (Twenty Seventeen's div.author-description holds h2.author-title
# and p.author-bio). A prose element holding the name heading is the box itself.
_NOT_AUTHOR_BIO = """
        [not(descendant-or-self::*[re:test(@class, '(?:^|\\s)author-(?:about|bio|description)(?:\\s|$)') and
        not(descendant::*[self::h1 or self::h2 or self::h3 or self::h4 or self::h5 or self::h6 or
        re:test(@class, '[Aa]uthor-?(?:[Nn]ame|[Tt]itle)')])])]
"""

# A teaser for another story is no byline. A candidate holding that story's headline with its link (North East
# Media's div.root.with-byline cards: span.teaser-keyword "Digital Editions" and h3.teaser-title > a gave the
# author "Digital Editions Wangaratta Chronicle; Friday"), or sitting in a link around a card with a heading or a
# title (refinery29's a > div.story-content > div.title ... div.story-meta > div.author), is the card. A heading
# naming the author (author-name, author-title, rel="author") is the byline's own.
_HEADING = "self::h1 or self::h2 or self::h3 or self::h4 or self::h5 or self::h6"
_NOT_TEASER = f"""
        [not(descendant-or-self::*[{_HEADING}][descendant::a[@href] or ancestor::a[@href]]
        [not(re:test(@class, '[Aa]uthor') or descendant::a[@rel='author' or re:test(@class, '[Aa]uthor')])])]
        [not(ancestor::a[@href][descendant::*[{_HEADING} or re:test(@class, '(?:^|\\s)(?:title|headline)(?:\\s|$)')]])]
"""

# A meta item's label is no name, and an element whose whole text is that label holds no value:
# the Bricks theme on 943.com.au and waggaslifefm.com puts its reading-time item in a block named
# "fr-blog-post-delta__meta--author", the label p.fr-blog-post-delta__meta-title "Reading time" over
# a widget the page's script fills
_NOT_META_LABEL_ONLY = """
        [not(normalize-space(.) = normalize-space(.//*[re:test(@class, 'meta[-_]+(?:title|label)(?:\\s|$)')]))]
"""

# the order or depth of XPaths could be changed after exhaustive testing
# Python ports of the author XPaths (re:test() callbacks are slow).
# Unlike "//" XPaths, they skip document-level siblings of the root.
AUTHOR_DISCARD_ID = re.compile("^comments|comment-?list|ProductReviews")
AUTHOR_DISCARD_CLASS = re.compile(
    "^[Cc]omments|commentlist|comments-list|sidebar|is-hidden|quote|embedly-instagram|"
    "article-(?:share|support)|print|category|meta-date|meta-reviewer|meta-status"
    r"|(?:^|\s)(?!(?:has|no|with|show|open|closed)-)\w+-comments(?:\s|$)"
    # a WordPress byline row holds the post's categories and tags ("By Jane Roe Posted in Local news")
    r"|(?:^|\s)(?:cat|tags?)-links(?:\s|$)"
)
AUTHOR_CLASS_SPECIFIC = re.compile("author[- ]?name|AuthorName|authorName")
AUTHOR_ID_GENERIC = re.compile("author|zuozhe|bianji|xiaobian")
AUTHOR_CLASS_GENERIC = re.compile("author|channel-name|zuozhe|bianji|xiaobian|submitted-by|posted-by|journalist-name")
AUTHOR_ID_ANY = re.compile("[Aa]uthor")
AUTHOR_CLASS_ANY = re.compile("[Aa]uthor|screenname|writer|[Bb]yline")


# A WordPress post container (hAtom "hentry", "status-publish") is never the byline,
# but its taxonomy classes can name authors ("author_tags-jane-roe").
AUTHOR_POST_CONTAINER = re.compile(r"(?:^|\s)(?:hentry|status-publish)(?:\s|$)")

# An author box's prose about the writer is no byline, nor is an element holding it:
# the name has an element of its own (div.author-about beside h4.author-name).
# A prose element holding the name heading is the box itself.
AUTHOR_BIO_CLASS = re.compile(r"(?:^|\s)author-(?:about|bio|description)(?:\s|$)")
AUTHOR_NAME_OR_TITLE_CLASS = re.compile("[Aa]uthor-?(?:[Nn]ame|[Tt]itle)")
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")


def _holds_author_bio(elem: HtmlElement) -> bool:
    "The element is or holds an author box's prose, which holds no name heading."
    return any(
        not any(
            sub.tag in _HEADINGS or AUTHOR_NAME_OR_TITLE_CLASS.search(sub.get("class", ""))
            for sub in bio.iterdescendants(Element)
        )
        for bio in elem.iter(Element)
        if AUTHOR_BIO_CLASS.search(bio.get("class", ""))
    )


# A meta item's label is no name, and an element whose whole text is that label holds no value:
# a block named "...__meta--author" holding the label p.entry__meta-title "Reading time"
# over a widget the page's script fills
AUTHOR_META_LABEL_CLASS = re.compile(r"meta[-_]+(?:title|label)(?:\s|$)")


def _is_meta_label_only(elem: HtmlElement) -> bool:
    "The element's whole text is the text of the first meta item label it holds."
    label = next((sub for sub in elem.iterdescendants(Element) if AUTHOR_META_LABEL_CLASS.search(sub.get("class", ""))), None)
    return label is not None and elem.text_content().split() == label.text_content().split()


# A teaser for another story is no byline. A candidate holding that story's headline with its link,
# or sitting in a link around a card with a heading or a title, is the card. A heading naming the
# author (an author class, rel="author") is the byline's own.
AUTHOR_MENTION = re.compile("[Aa]uthor")
# "title" or "headline" as a class token or its last part ("entry-title", "card__headline"), not "subtitle"
TEASER_TITLE_CLASS = re.compile(r"(?:^|\s)(?:[\w-]*[-_])?(?:title|headline)(?:\s|$)")


def _is_teaser(elem: HtmlElement) -> bool:
    "The element holds a linked heading not naming the author, or sits in a link around a heading or a title."
    for heading in elem.iter(*_HEADINGS):
        links = list(heading.iterdescendants("a"))
        linked = any("href" in link.attrib for link in links) or any(
            "href" in link.attrib for link in heading.iterancestors("a")
        )
        names_author = AUTHOR_MENTION.search(heading.get("class", "")) or any(
            link.get("rel") == "author" or AUTHOR_MENTION.search(link.get("class", "")) for link in links
        )
        if linked and not names_author:
            return True
    return any(
        "href" in link.attrib
        and any(
            sub.tag in _HEADINGS or TEASER_TITLE_CLASS.search(sub.get("class", "")) for sub in link.iterdescendants(Element)
        )
        for link in elem.iterancestors("a")
    )


def _author_specific(tree: HtmlElement) -> list[HtmlElement]:
    "Specific and almost specific author markers."
    return [
        elem
        for elem in tree.iter("a", "address", "div", "link", "p", "span", "strong", "author")
        if (
            elem.tag == "author"
            or elem.get("rel") == "author"
            or elem.get("id") == "author"
            or elem.get("class") == "author"
            or elem.get("itemprop") == "author name"
            or (elem.tag == "a" and "author-link" in elem.get("class", "").split())
            or elem.get("data-testid") in ("AuthorCard", "AuthorURL")
            or AUTHOR_CLASS_SPECIFIC.search(elem.get("class", ""))
        )
        and not _holds_author_bio(elem)
        and not _is_meta_label_only(elem)
        and not _is_teaser(elem)
    ]


def _author_generic(tree: HtmlElement) -> list[HtmlElement]:
    "Almost generic and generic author markers, last ones not common."
    return [
        elem
        for elem in tree.iter("a", "div", "h3", "h4", "p", "span")
        if (
            elem.get("class") in ("byline", "username", "byl", "BBL")
            or "author" in elem.get("itemprop", "")
            or AUTHOR_ID_GENERIC.search(elem.get("id", ""))
            or AUTHOR_CLASS_GENERIC.search(elem.get("class", ""))
        )
        and not AUTHOR_POST_CONTAINER.search(elem.get("class", ""))
        and not _holds_author_bio(elem)
        and not _is_meta_label_only(elem)
        and not _is_teaser(elem)
    ]


def _author_any(tree: HtmlElement) -> list[HtmlElement]:
    "Last resort: any element."
    return [
        elem
        for elem in tree.iter(Element)
        if (
            "Byline" in elem.get("data-component", "")
            or "author" in elem.get("itemprop", "")
            or AUTHOR_ID_ANY.search(elem.get("id", ""))
            or AUTHOR_CLASS_ANY.search(elem.get("class", ""))
        )
        and not AUTHOR_POST_CONTAINER.search(elem.get("class", ""))
        and not _holds_author_bio(elem)
        and not _is_meta_label_only(elem)
        and not _is_teaser(elem)
    ]


def _author_discard(tree: HtmlElement) -> list[HtmlElement]:
    """Comments, titles, dates and other sections misleading author detection. One holding an h1 is the page's
    wrapper, not a section beside the story ("gh-post-page--with-sidebar" holds Ghost's whole story)."""
    return [
        elem
        for elem in tree.iter("a", "div", "section", "span")
        if (
            elem.get("class") in ("title", "date")
            or AUTHOR_DISCARD_ID.search(elem.get("id", ""))
            or AUTHOR_DISCARD_CLASS.search(elem.get("class", ""))
            or "Figure" in elem.get("data-component", "")
        )
        and next(elem.iter("h1"), None) is None
    ]


AUTHOR_TIMESTAMP_ATTRS = ("datetime", "data-timestamp", "data-date", "data-datetime")
AUTHOR_TIMESTAMP_CLASS = re.compile(r"(?:^|[\s_-])(?:date|timestamp|pubdate)(?:[A-Z\s_-]|$)")
AUTHOR_OR_BYLINE_CLASS = re.compile("[Aa]uthor|[Bb]yline")


def _is_timestamp(elem: HtmlElement) -> bool:
    "A date or time element, by its attributes, class or tag name."
    return (
        any(attr in elem.attrib for attr in AUTHOR_TIMESTAMP_ATTRS)
        or elem.get("itemprop", "").startswith("date")
        or bool(AUTHOR_TIMESTAMP_CLASS.search(elem.get("class", "")))
        or "timestamp" in str(elem.tag)
    )


def _author_timestamps(tree: HtmlElement) -> list[HtmlElement]:
    'A timestamp is no name, unless the element is or holds the author ("date-author").'
    return [
        elem
        for elem in tree.iter(Element)
        if _is_timestamp(elem)
        and not any(
            AUTHOR_OR_BYLINE_CLASS.search(inner.get("class", ""))
            or inner.get("rel") == "author"
            or inner.get("itemprop") == "author"
            for inner in elem.iter(Element)
        )
    ]


AUTHOR_REL_TAG = re.compile(r"(?:^|\s)tag(?:\s|$)", re.IGNORECASE)
AUTHOR_REL_AUTHOR = re.compile(r"(?:^|\s)author(?:\s|$)", re.IGNORECASE)
AUTHOR_MARKED_CLASS = re.compile(r"(?:^|\s)author(?:\s|$)|[Aa]uthor-?[Nn]ame")


def _marks_author(elem: HtmlElement) -> bool:
    "The page marks the element as the author."
    return bool(
        AUTHOR_REL_AUTHOR.search(elem.get("rel", ""))
        or "author" in elem.get("itemprop", "")
        or AUTHOR_MARKED_CLASS.search(elem.get("class", ""))
    )


def _author_tag_links(tree: HtmlElement) -> list[HtmlElement]:
    """A link the page marks as a tag (rel="tag", rel="category tag") names a topic, unless it is
    inside the element the page marks as the author: a byline taxonomy's terms are tags too."""
    return [
        elem
        for elem in tree.iter(Element)
        if AUTHOR_REL_TAG.search(elem.get("rel", ""))
        and not _marks_author(elem)
        and not any(_marks_author(parent) for parent in elem.iterancestors())
    ]


AUTHOR_XPATHS: list[Callable[[HtmlElement], list[HtmlElement]]] = [_author_specific, _author_generic, _author_any]
AUTHOR_DISCARD_XPATHS: list[Callable[[HtmlElement], Any]] = [
    _author_discard,
    _author_timestamps,
    _author_tag_links,
    XPath("//time|//figure|//script|//style"),
]


# links under a block: /descendant::a walks each block once, whereas //a expands to
# descendant-or-self::node()/child::a and libxml2 merges every node with a duplicate check,
# quadratic on pages with many matching blocks (grids of div.row)
CATEGORIES_XPATHS = [
    XPath(
        """
        //div[
        (@class and re:test(@class, '^(?:post-?info|post-?meta|meta|entry-meta|entry-info|entry-utility)')) or
        starts-with(@id, 'postpath')]/descendant::a[@href]
        """,
        namespaces={"re": REGEXP_NS},
    ),
    XPath("""//p[starts-with(@class, 'postmeta') or starts-with(@class, 'entry-categories') or
     @class='postinfo' or @id='filedunder']/descendant::a[@href]"""),
    XPath("""//footer[starts-with(@class, 'entry-meta') or starts-with(@class, 'entry-footer')]/descendant::a[@href]"""),
    XPath("""//*[self::li or self::span][@class='post-category' or @class='postcategory' or
     @class='entry-category' or contains(@class, 'cat-links')]/descendant::a[@href]"""),
    XPath("""//header[@class='entry-header']/descendant::a[@href]"""),
    XPath("""//div[@class='row' or @class='tags']/descendant::a[@href]"""),
]
# "//*[self::div or self::p][contains(@class, 'byline')]",


TAGS_XPATHS = [
    XPath("""//div[@class='tags']/descendant::a[@href]"""),
    XPath("""//p[starts-with(@class, 'entry-tags')]/descendant::a[@href]"""),
    XPath(
        """//div[@class='row' or @class='jp-relatedposts' or @class='entry-utility' or
    (@class and re:test(@class, '^(?:tag|postmeta|meta)'))]/descendant::a[@href]""",
        namespaces={"re": REGEXP_NS},
    ),
    XPath("""//*[@class='entry-meta' or contains(@class, 'topics') or
     contains(@class, 'tags-links')]/descendant::a[@href]"""),
]
# "related-topics"
# https://github.com/grangier/python-goose/blob/develop/goose/extractors/tags.py


TITLE_XPATHS = [
    XPath(
        """
        //*[self::h1 or self::h2][
        (@class and re:test(@class, '(?:post-|entry-|article-|post__)title|headline')) or
        contains(@id, 'headline') or contains(@itemprop, 'headline')]
        """,
        namespaces={"re": REGEXP_NS},
    ),
    XPath("""//*[@class='entry-title' or @class='post-title']"""),
    XPath("""//*[self::h1 or self::h2 or self::h3][
    contains(@class, 'title') or contains(@id, 'title')]"""),
]
# json-ld headline
# '//header/h1',
