# pylint:disable-msg=C0301,E0611,I1101
"""
Functions to process nodes in HTML code.
"""

import logging
import re
from copy import deepcopy
from urllib.parse import urlsplit

from lxml.etree import Element, SubElement, XPath, _Element, strip_elements, strip_tags, tostring
from lxml.html import HtmlElement

from .deduplication import duplicate_test
from .settings import (
    CUT_EMPTY_ELEMS,
    MANUALLY_CLEANED,
    MANUALLY_STRIPPED,
    Document,
    Extractor,
)
from .utils import LINK_FARM_RATIO, RE_FILTER, RELATED_LABEL, image_src, safe_base_url, safe_relative_url, textfilter, trim
from .xml import delete_element, meta_items, separates_inline

LOGGER = logging.getLogger(__name__)

REND_TAG_MAPPING = {
    **dict.fromkeys(("em", "i"), "#i"),
    **dict.fromkeys(("b", "strong"), "#b"),
    "u": "#u",
    **dict.fromkeys(("kbd", "samp", "tt", "var"), "#t"),
    "sub": "#sub",
    "sup": "#sup",
}

HTML_TAG_MAPPING = {v: k for k, v in REND_TAG_MAPPING.items()}

PRESERVE_IMG_CLEANING = {"figure", "picture", "source"}

CODE_INDICATORS = ["{", '("', "('", "\n    "]

# LaTeX source carried by MathML, in order of preference: the annotation holds the
# original markup, alttext a rendering of it. local-name() also matches XHTML pages
# where the subtree keeps its MathML namespace.
TEX_ANNOTATION_XPATH = XPath('.//*[local-name()="annotation"][@encoding="application/x-tex"]')


def recover_math(tree: HtmlElement) -> HtmlElement:
    "Turn MathML into its LaTeX source so formulas survive the cleaning of <math>."
    for element in tree.iter("math"):
        annotation = TEX_ANNOTATION_XPATH(element)
        latex = trim(annotation[0].text or "") if annotation else trim(element.get("alttext") or "")
        if not latex:
            continue
        # delimiters the Markdown converter understands, see _convert_math()
        opening, closing = ("\\[", "\\]") if element.get("display") == "block" else ("\\(", "\\)")
        # the tail is kept when the element is deleted further down, the subtree is not
        element.tail = f"{opening}{latex}{closing}{element.tail or ''}"
    return tree


def _handle_forms(tree: HtmlElement) -> None:
    """Delete <form> elements, keeping those that wrap the page's main content.

    Frameworks like ASP.NET WebForms put the whole document inside a single
    <form id="aspnetForm">, so dropping every form leaves nothing to extract.
    A form holding most of the remaining text is such a layout wrapper and gets
    demoted to a plain container; genuine widgets (search, login, newsletter)
    hold little text and are still removed.
    """
    forms = list(tree.iter("form"))
    if not forms:
        return
    # run after the other elements are gone, so script/head text cannot skew the ratio
    total = len(tree.text_content())
    for form in forms:
        if total and len(form.text_content()) > total / 2:
            form.tag = "div"
        else:
            delete_element(_form_widget(form, total))


# A form's widget: the short block around a sign-up, sign-in or subscription form ("Why
# register?", "Registration is free", the field and its button), never a block holding prose.
# A comment or search form goes alone: the comments next to it are not its widget.
WIDGET_MAX_CHARS = 600
WIDGET_LINE_MAX_CHARS = 100
_ACCOUNT_FORM = re.compile(r"regist|log-?in|sign-?(?:in|up)|subscri|news-?letter", re.IGNORECASE)
_OTHER_FORM = re.compile(r"comment|reply|search", re.IGNORECASE)


def _account_form(form: HtmlElement) -> bool:
    "A form that signs the reader up, in or on: named so, or asking for an email or a password."
    names = " ".join(form.get(attr) or "" for attr in ("id", "class", "action", "name"))
    if _OTHER_FORM.search(names):
        return False
    return _ACCOUNT_FORM.search(names) is not None or bool(form.xpath(".//input[@type='email' or @type='password']"))


def _form_widget(form: HtmlElement, total: int) -> HtmlElement:
    "The largest block around a sign-up form that holds nothing but its short texts."
    widget = form
    if not _account_form(form):
        return widget
    for parent in form.iterancestors():
        length = len(trim(parent.text_content()))
        if (
            parent.tag not in ("div", "section", "aside")
            or length > WIDGET_MAX_CHARS
            or length * 3 > total
            or parent.find(".//h1") is not None
            or any(len(trim(p.text_content())) > WIDGET_LINE_MAX_CHARS for p in parent.iter("p", "li"))
        ):
            break
        widget = parent
    return widget


# fallback or embedded copies: paragraphs inside them neither count as removed nor as surviving content
ALTERNATE_CONTENT = {"noscript", "fencedframe"}
UNDO_MAX_WORDS = 50


# The rest of an article that a "continue reading" control reveals: a hidden block, last in its
# container, right after an element that is nothing but that control, holding article paragraphs
# and preceded by the visible ones. Hidden blocks are otherwise discarded as page furniture.
HIDDEN_CONTINUATION_XPATH = XPath(
    """
    .//*[@hidden or contains(translate(@style, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ \t\r\n', 'abcdefghijklmnopqrstuvwxyz'),
    'display:none')][not(following-sibling::*)]
    [preceding-sibling::*[1][self::button or (.//button and normalize-space() = normalize-space(.//button[1]))]]
    [count(.//p[string-length(normalize-space()) > 80]) >= 2]
    [preceding-sibling::*//p[string-length(normalize-space()) > 80]]
    """
)


def reveal_hidden_continuation(tree: HtmlElement) -> None:
    "Un-hide the rest of an article kept behind a continue-reading control."
    for elem in HIDDEN_CONTINUATION_XPATH(tree):
        elem.attrib.pop("hidden", None)
        elem.attrib.pop("style", None)


# A figure's own prose: list articles write each item's text beside its image, inside the figure
# (BuzzFeed: figure > div.subbuzz__figure-footer > div.subbuzz__description > p). Figures are deleted
# with their content when images are off, so such paragraphs take the figure's place first; its
# media, captions, quotes and tables go with it. Only a figure holding a paragraph of prose counts:
# a short credit line in a <p> is no reason to keep anything.
FIGURE_PROSE_MIN_CHARS = 80
_FIGURE_PROSE_EXCLUDED = {"figcaption", "blockquote", "table", "figure"}


def _figure_paragraphs(figure: HtmlElement) -> list[HtmlElement]:
    "The paragraphs of a figure that are its own text, not a caption, quote, table or nested figure."
    paragraphs = []
    for para in figure.iter("p"):
        between = []
        for ancestor in para.iterancestors():
            if ancestor is figure:
                break
            between.append(ancestor.tag)
        if not _FIGURE_PROSE_EXCLUDED.intersection(between) and trim(para.text_content()):
            paragraphs.append(para)
    return paragraphs


def keep_figure_prose(tree: HtmlElement) -> None:
    "Move the prose paragraphs of a figure out of it, in place, before the figure is discarded."
    for figure in list(tree.iter("figure")):
        parent = figure.getparent()
        paragraphs = _figure_paragraphs(figure)
        if parent is None or not any(len(trim(p.text_content())) >= FIGURE_PROSE_MIN_CHARS for p in paragraphs):
            continue
        index = parent.index(figure)
        for offset, para in enumerate(paragraphs):
            para.tail = None
            parent.insert(index + offset, para)


def tree_cleaning(tree: HtmlElement, options: Extractor) -> HtmlElement:
    "Prune the tree by discarding unwanted elements."
    # before the controls go: their hidden continuation would be discarded with them
    reveal_hidden_continuation(tree)
    # salvage formulas before <math> is discarded along with its subtree
    recover_math(tree)
    # determine cleaning strategy
    cleaning_list, stripping_list = set(MANUALLY_CLEANED), set(MANUALLY_STRIPPED)
    # forms are handled separately below, once the rest of the noise is gone. MANUALLY_CLEANED is
    # public API users mutate in place, so honour a removed "form" instead of assuming it is there.
    clean_forms = "form" in cleaning_list
    cleaning_list.discard("form")
    if not options.tables:
        cleaning_list |= {"table", "td", "th", "tr"}
    else:
        # figures holding a table (#301) and ARIA layout tables (role=presentation/none)
        for elem in tree.iter("figure", "table"):
            if elem.find(".//table") is not None if elem.tag == "figure" else elem.get("role") in ("presentation", "none"):
                elem.tag = "div"
        # a footer row group may come before the body in the markup but is shown below it, so it is
        # moved last before <tfoot> is stripped: a "Total" row stays under the rows it sums
        for tfoot in list(tree.iter("tfoot")):
            parent = tfoot.getparent()
            if parent is not None and parent.tag == "table":
                parent.append(tfoot)
        # the cells of a head row group are header cells even when written as <td>,
        # which can no longer be told once <thead> is stripped
        for cell in tree.iterfind(".//thead/tr/td"):
            cell.tag = "th"
    if "figure" in cleaning_list and not options.images:
        keep_figure_prose(tree)
    if options.images:
        # Many websites have <img> inside <figure> or <picture> or <source> tag
        cleaning_list -= PRESERVE_IMG_CLEANING
        stripping_list.discard("img")

    # a <template> is inert markup a script may clone and is never shown as it is, unless it is a
    # declarative shadow root, which the browser renders in place (and which stays stripped below)
    for template in [t for t in tree.iter("template") if t.get("shadowrootmode", t.get("shadowroot")) is None]:
        delete_element(template)

    # strip targeted elements
    strip_tags(tree, *stripping_list)

    # undo the deletions if they remove every paragraph and leave almost no text, e.g. a page swallowed
    # by an unclosed <aside> or <button> (copy only if none can survive)
    tcopy = None
    survives = (
        "p" not in cleaning_list and next(p.iterancestors(cleaning_list), None) is None
        for p in tree.iter("p")
        if next(p.iterancestors(ALTERNATE_CONTENT), None) is None
    )
    if next(survives, True) is False and not any(survives):
        tcopy = deepcopy(tree)
    strip_elements(tree, *cleaning_list, with_tail=False)
    if tcopy is not None and (options.focus == "recall" or len(" ".join(tree.itertext()).split()) < UNDO_MAX_WORDS):
        tree = tcopy
        # honour tables=False, recall keeps them as before
        if not options.tables and options.focus != "recall":
            strip_tags(tree, "table", "tr", "td", "th")

    # prefixed tags like Word's <o:p> or <fb:like> are not valid element names in the output, keep their text
    if prefixed := {tag for tag in {elem.tag for elem in tree.iter("*")} if isinstance(tag, str) and ":" in tag}:
        strip_tags(tree, *prefixed)

    if clean_forms:
        _handle_forms(tree)

    return prune_html(tree, options.focus)


def prune_html(tree: HtmlElement, focus: str = "balanced") -> HtmlElement:
    "Delete selected empty elements to save space and processing time."
    tails = focus != "precision"
    for element in [e for e in tree.iterdescendants(CUT_EMPTY_ELEMS) if e.text is None and len(e) == 0]:
        delete_element(element, keep_tail=tails)
    return tree


def prune_unwanted_nodes(
    tree: HtmlElement, nodelist: list[XPath], with_backup: bool = False, extracted_len: int = 0
) -> HtmlElement:
    """Prune the HTML tree by removing unwanted sections.

    With a backup, nothing is pruned when that would remove almost all the text. Text
    already extracted from the page (``extracted_len`` characters, taken by an earlier,
    narrower frame that holds no short extraction) counts as kept: that frame took the
    article out of this one, and what is left here is mostly page furniture the guard would
    otherwise keep whole.
    """
    if not with_backup:
        for expression in nodelist:
            for subtree in expression(tree):
                delete_element(subtree)  # the tail text is preserved
        return tree
    # guard: prune nothing if it would remove too much text, the tree keeps its elements
    matches = dict.fromkeys(elem for expression in nodelist for elem in expression(tree))
    outer = [elem for elem in matches if not any(a in matches for a in elem.iterancestors())]
    old_len = len(tree.text_content()) + extracted_len
    # todo: adjust for recall and precision settings
    if old_len - sum(len(elem.text_content()) for elem in outer) > old_len / 7:
        for elem in outer:
            delete_element(elem)
    return tree


def collect_link_info(
    links_xpath: list[HtmlElement],
) -> tuple[int, int, int, list[str]]:
    "Collect heuristics on link text"
    mylist = [e for e in (trim(elem.text_content()) for elem in links_xpath) if e]
    lengths = list(map(len, mylist))
    # longer strings impact recall in favor of precision
    shortelems = sum(1 for length in lengths if length < 10)
    return sum(lengths), len(mylist), shortelems, mylist


def is_paragraph_listing(links_xpath: list[HtmlElement]) -> bool:
    "Tell a document listing (every link alone in its paragraph) from a farm (links running together)"
    for link in links_xpath:
        parent = link.getparent()
        if parent is None or parent.tag != "p" or len(list(parent.iterdescendants("ref"))) > 1:
            return False
    return True


SENTENCE_END = re.compile(r"[.!?][\"'’”)]?$")


def is_linked_sentence(element: HtmlElement, text: str) -> bool:
    "A paragraph that is a complete sentence is prose even when a link spans it."
    return element.tag == "p" and len(text.split()) >= 12 and SENTENCE_END.search(text) is not None


def link_density_test(element: HtmlElement, favor_precision: bool = False) -> tuple[bool, bool]:
    "Remove sections which are rich in links (probably boilerplate), flag short linked ones."
    links_xpath = list(element.iterdescendants("ref"))
    if not links_xpath:
        return False, False
    # preserve image containers
    if next(element.iterdescendants("graphic"), None) is not None:
        return False, False
    text = trim(element.text_content())
    # shortcut
    if len(links_xpath) == 1 and not is_linked_sentence(element, text):
        len_threshold = 10 if favor_precision else 100
        link_text = trim(links_xpath[0].text_content())
        if len(link_text) > len_threshold and len(link_text) > len(text) * 0.9:
            return True, False
    if element.tag == "p":
        limitlen = 60 if element.getnext() is None else 30
    elif element.getnext() is None:
        limitlen = 300
    else:
        limitlen = 100
    elemlen = len(text)
    if elemlen < limitlen:
        linklen, elemnum, shortelems, _ = collect_link_info(links_xpath)
        if elemnum == 0:
            return True, False
        LOGGER.debug(
            "list link text/total: %s/%s – short elems/total: %s/%s",
            linklen,
            elemlen,
            shortelems,
            elemnum,
        )
        return linklen > elemlen * 0.8 or (elemnum > 1 and shortelems / elemnum > 0.8 and linklen > elemlen * 0.02), True
    # large near-total-link farms ("latest news" sidebars) at/above limitlen, which the size gate
    # above never tests (#584); small farms are already caught there at the plain 0.8 ratio.
    # >4: a farm is MANY links -- a handful of long sentence-links is editorial (knowtechie realworld test)
    if len(links_xpath) > 4:
        linklen, elemnum, _, _ = collect_link_info(links_xpath)
        # avg link len >= 100 => catalog/listing content (one link per card), not a farm: keep it
        if linklen > len(text) * LINK_FARM_RATIO and linklen < 100 * elemnum and not is_paragraph_listing(links_xpath):
            return True, False
    return False, False


def link_density_test_tables(element: HtmlElement) -> bool:
    "Remove tables which are rich in links (probably boilerplate)."
    links_xpath = list(element.iterdescendants("ref"))

    if not links_xpath:
        return False

    elemlen = len(trim(element.text_content()))
    if elemlen < 200:
        return False

    # links with no text (e.g. icon/flag links wrapping images) yield linklen 0 -> not boilerplate
    linklen, _, _, _ = collect_link_info(links_xpath)
    LOGGER.debug("table link text: %s / total: %s", linklen, elemlen)
    return linklen > 0.8 * elemlen if elemlen < 1000 else linklen > 0.5 * elemlen


def delete_by_link_density(
    subtree: HtmlElement,
    tagname: str,
    backtracking: bool = False,
    favor_precision: bool = False,
) -> HtmlElement:
    """Determine the link density of elements with respect to their length,
    and remove the elements identified as boilerplate."""
    deletions = []
    len_threshold = 200 if favor_precision else 100
    depth_threshold = 1 if favor_precision else 3

    for elem in subtree.iter(tagname):
        result, short_with_links = link_density_test(elem, favor_precision)
        if result or (
            backtracking
            and short_with_links
            and len(elem) >= depth_threshold
            and len(trim(elem.text_content())) < len_threshold
        ):
            # a paragraph that holds the content of a list item is kept: the
            # link density of the whole list is checked separately, and
            # removing it here would leave the item empty (GH #788)
            parent = elem.getparent()
            if tagname == "p" and parent is not None and parent.tag in ("item", "td", "th"):
                continue
            deletions.append(elem)

    for elem in dict.fromkeys(deletions):
        delete_element(elem)

    return subtree


# The calls to action that end an article: a link to the site's own social-network profile (not a
# post, not someone else's profile), or to its own newsletter, subscription or sign-up page
_SOCIAL_HOST = re.compile(
    r"(?:^|\.)(?:facebook|instagram|linkedin|twitter|x|tiktok|youtube|pinterest|threads)\.(?:com|net)(?:\.[a-z]{2})?$"
)
_SOCIAL_PROFILE_PATH = re.compile(
    r"^/(?:(?:groups|company|channel|c|user|in|pages)/)?"
    r"(?!(?:watch|shorts|status|p|reels?|videos?|photos?|posts?|stor(?:y|ies)|hashtag|search|events?|sharer?|intent|embed)(?:[/.]|$))"
    r"@?([^/]+)/?$"
)
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")
# a site name of fewer letters only counts at the start of a handle ("abc" -> "abcnews")
SITE_NAME_MIN_CHARS = 4
_NEWSLETTER_LINK = re.compile(r"news-?letter", re.IGNORECASE)
_SUBSCRIBE_LINK = re.compile(r"subscri|sign-?up|mailing-?list", re.IGNORECASE)
# a copyright notice: "© 2026 Example", "Copyright Example 2026", "... All rights reserved."
_NOTICE = re.compile(r"^\W*(?:©|copyright\b)|\ball rights reserved\b", re.IGNORECASE)
TRAILING_CALL_MAX_CHARS = 300
_TRAILING_BLOCKS = ("p", "head", "item", "quote", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote")


def _same_site(host: str, page_host: str) -> bool:
    host, page_host = host.removeprefix("www."), page_host.removeprefix("www.")
    return host == page_host or host.endswith("." + page_host) or page_host.endswith("." + host)


def _link_parts(target: str) -> tuple[str, str, str]:
    "Host, path and fragment of a link, empty for an address that does not parse."
    try:
        parts = urlsplit(target.strip())
    except ValueError:
        return "", "", ""
    return (parts.hostname or "").casefold(), parts.path, parts.fragment


def _addresses(elem: HtmlElement) -> list[str]:
    # a converted link keeps its address in "target"; an unconverted one in "href", where
    # "target" is the HTML window name ("_blank")
    return [link.get("href") or link.get("target") or "" for link in elem.iter("ref", "a")]


def is_call_to_action(target: str, page_host: str | None) -> bool:
    "A link to a social-network profile, to a newsletter, or to the site's own subscription page."
    host, path, fragment = _link_parts(target)
    if not (host or path or fragment):
        return False
    if _SOCIAL_HOST.search(host):
        profile = _SOCIAL_PROFILE_PATH.match(path)
        return profile is not None and _site_handle(profile.group(1), page_host)
    where = f"{path}#{fragment}"
    return _own_site(host, page_host) and (
        _NEWSLETTER_LINK.search(where) is not None or _SUBSCRIBE_LINK.search(where) is not None
    )


def _site_handle(handle: str, page_host: str | None) -> bool:
    'A profile handle naming the site: "adnews" on adnews.com.au, "australianageingagenda" on theageingagenda.com.au.'
    handle = _NOT_ALNUM.sub("", handle.casefold())
    if not handle or not page_host:
        return False
    for label in page_host.casefold().removeprefix("www.").split("."):
        name = _NOT_ALNUM.sub("", label.removeprefix("the"))
        if len(name) >= SITE_NAME_MIN_CHARS and (name in handle or (len(handle) >= SITE_NAME_MIN_CHARS and handle in name)):
            return True
        if name and len(name) < SITE_NAME_MIN_CHARS and handle.startswith(name):
            return True
    return False


def _own_site(host: str, page_host: str | None) -> bool:
    return not host or (page_host is not None and _same_site(host, page_host.casefold()))


def _home_page(target: str, page_host: str | None) -> bool:
    "A link to the site's own home page (its shop or another subdomain too)."
    host, path, _ = _link_parts(target)
    return bool(host) and _own_site(host, page_host) and path in ("", "/")


def _trailing_call(elem: HtmlElement, page_host: str | None) -> bool:
    "A short block that is a call to action or a copyright notice."
    text = trim(elem.text_content())
    if len(text) > TRAILING_CALL_MAX_CHARS:
        return False
    return _NOTICE.search(text) is not None or any(is_call_to_action(target, page_host) for target in _addresses(elem))


def _call_list(elem: HtmlElement, page_host: str | None) -> bool:
    "A list of short links, most of them calls to action or the site's home page."
    items = [item for item in elem if item.tag in ("item", "li")]
    if len(items) < 2:
        return False
    calls = 0
    for item in items:
        targets = _addresses(item)
        if not targets or len(trim(item.text_content())) > TRAILING_CALL_MAX_CHARS / 2:
            return False
        calls += _trailing_call(item, page_host) or all(_home_page(target, page_host) for target in targets)
    return calls * 2 >= len(items)


# a block passed over, as a lone label is: a few words and no full stop ("Request a Correction")
MINOR_BLOCK_MAX_WORDS = 8
_SENTENCE_END = (".", "!", "?", "…", '"', "\u201d", "\u2019")


def _minor_block(text: str) -> bool:
    return len(text.split()) <= MINOR_BLOCK_MAX_WORDS and not text.endswith(_SENTENCE_END)


def _short_linked(elem: HtmlElement) -> bool:
    "A short block holding a link: a teaser, the byline and date of a related story."
    return len(trim(elem.text_content())) <= TRAILING_CALL_MAX_CHARS and bool(_addresses(elem))


RELATED_LIST_ITEM_MAX_CHARS = 150


def prune_related_lists(tree: HtmlElement) -> None:
    """Delete a related-stories label ("Related stories:") and the list of links right under it,
    wherever it sits in the article: a list of short items, each a link."""
    for label in [elem for elem in tree.iter(*_TRAILING_BLOCKS) if RELATED_LABEL.match(trim(elem.text_content()))]:
        following = label.getnext()
        if following is None or following.tag not in ("list", "ul", "ol"):
            continue
        items = [item for item in following if item.tag in ("item", "li")]
        if items and all(_addresses(item) and len(trim(item.text_content())) <= RELATED_LIST_ITEM_MAX_CHARS for item in items):
            delete_element(following)
            delete_element(label)


def prune_trailing_calls(tree: HtmlElement, url: str | None) -> None:
    """Delete the calls to action and the copyright notice that end an article.

    Walking back from the last text block: a short block linking to a social-network profile, a
    newsletter or the site's own subscription page, or a copyright notice, is page furniture, and
    so is a list of short links most of which are such calls; the first other block ends the
    story. Lone labels ("Share", "Advertisement", textfilter() drops them) and other blocks of a
    few words with no full stop are passed over; short linked blocks under a related-stories label
    ("Related news") go with it. Nothing goes if the furniture would hold half the text.
    """
    try:
        page_host = urlsplit(url).hostname if url else None
    except ValueError:
        page_host = None
    blocks = [
        elem
        for elem in tree.iter(*_TRAILING_BLOCKS)
        if elem is not tree and next(elem.iterdescendants(*_TRAILING_BLOCKS), None) is None and trim(elem.text_content())
    ]
    furniture: list[HtmlElement] = []
    # short linked blocks walked over: furniture once a related-stories label tops them
    teasers: list[HtmlElement] = []
    index = len(blocks) - 1
    while index >= 0:
        block = blocks[index]
        parent = block.getparent()
        text = trim(block.text_content())
        if block.tag in ("item", "li") and parent is not None and _call_list(parent, page_host):
            furniture.append(parent)
            while index >= 0 and parent in blocks[index].iterancestors():
                index -= 1
            continue
        if _trailing_call(block, page_host):
            furniture.append(block)
        elif RELATED_LABEL.match(text):
            furniture.extend(teasers)
            teasers = []
        elif _short_linked(block):
            teasers.append(block)
        elif not (RE_FILTER.match(text) or _minor_block(text)):
            break
        index -= 1
    if furniture and 2 * sum(len(trim(elem.text_content())) for elem in furniture) < len(trim(tree.text_content())):
        for elem in furniture:
            delete_element(elem)


def handle_textnode(
    elem: _Element,
    options: Extractor,
    comments_fix: bool = True,
    preserve_spaces: bool = False,
) -> _Element | None:
    "Convert, format, and probe potential text elements."
    if elem.tag == "graphic" and image_src(elem) is not None:
        return elem
    if elem.tag == "done" or (
        len(elem) == 0 and not elem.text and not elem.tail and (comments_fix or not separates_inline(elem))
    ):
        return None

    # lb bypass
    if not comments_fix and elem.tag == "lb":
        if not preserve_spaces:
            elem.tail = trim(elem.tail) or None
        # no textfilter/dedup for lb, unlike process_node: the two probes are not interchangeable
        return elem

    if not elem.text and len(elem) == 0:
        # try the tail
        elem.text, elem.tail = elem.tail, ""
        # handle differently for br/lb
        if comments_fix and elem.tag == "lb":
            elem.tag = "p"

    # trim
    if not preserve_spaces:
        elem.text = trim(elem.text) or None
        if elem.tail:
            elem.tail = trim(elem.tail) or None

    # filter content
    if (not elem.text and textfilter(elem)) or (options.dedup and duplicate_test(elem, options)):
        return None
    return elem


def process_node(elem: _Element, options: Extractor) -> _Element | None:
    "Convert, format, and probe potential text elements (light format)."
    if elem.tag == "done" or (len(elem) == 0 and not elem.text and not elem.tail):
        return None

    # trim
    elem.text, elem.tail = trim(elem.text) or None, trim(elem.tail) or None

    # adapt content string
    if elem.tag != "lb" and not elem.text and elem.tail and len(elem) == 0:
        elem.text, elem.tail = elem.tail, None

    # content checks
    if elem.text or elem.tail:
        if textfilter(elem) or (options.dedup and duplicate_test(elem, options)):
            return None

    return elem


def convert_lists(elem: _Element) -> None:
    "Convert <ul> and <ol> to <list> and underlying <li> elements to <item>."
    elem.set("rend", elem.tag)
    elem.tag = "list"
    i = 1
    for subelem in elem.iter("dd", "dt", "li"):
        # keep track of dd/dt items
        if subelem.tag in ("dd", "dt"):
            subelem.set("rend", f"{subelem.tag!s}-{i}")
            # increment counter after <dd> in description list
            if subelem.tag == "dd":
                i += 1
        # convert elem tag (needs to happen after the rest)
        subelem.tag = "item"


def convert_quotes(elem: _Element) -> None:
    "Convert quoted elements while accounting for nested structures."
    # lang on a blockquote names a natural language (embedded tweets): no code marker or code language
    if elem.tag == "blockquote":
        elem.attrib.pop("lang", None)
    # a pre is code if it wraps a single span, carries highlight.js spans or code-like text
    hljs = elem.xpath(".//span[starts-with(@class,'hljs')]") if elem.tag == "pre" else []
    for subelem in hljs:
        subelem.attrib.clear()
    is_code = elem.tag == "pre" and (
        (len(elem) == 1 and elem[0].tag == "span") or bool(hljs) or any(i in (elem.text or "") for i in CODE_INDICATORS)
    )
    elem.tag = "code" if is_code else "quote"


def convert_headings(elem: _Element) -> None:
    "Add head tags and delete attributes."
    elem.attrib.clear()
    elem.set("rend", elem.tag)
    elem.tag = "head"


def convert_line_breaks(elem: _Element) -> None:
    "Convert <br> and <hr> to <lb>"
    elem.tag = "lb"


def convert_deletions(elem: _Element) -> None:
    'Convert <del>, <s>, <strike> to <del rend="overstrike">'
    elem.tag = "del"
    elem.set("rend", "overstrike")


def convert_details(elem: _Element) -> None:
    "Handle details and summary."
    elem.tag = "div"
    for subelem in elem.iter("summary"):
        subelem.tag = "head"


CONVERSIONS = {
    **dict.fromkeys(("dl", "ol", "ul"), convert_lists),
    **dict.fromkeys(("h1", "h2", "h3", "h4", "h5", "h6"), convert_headings),
    **dict.fromkeys(("br", "hr"), convert_line_breaks),
    **dict.fromkeys(("blockquote", "pre"), convert_quotes),
    **dict.fromkeys(("del", "s", "strike"), convert_deletions),
    "details": convert_details,
}


def convert_link(elem: HtmlElement, base_url: str | None) -> None:
    "Replace link tags and href attributes, delete the rest."
    elem.tag = "ref"
    target = elem.get("href")  # defaults to None
    elem.attrib.clear()
    if target:
        # convert relative URLs
        if base_url:
            target = safe_relative_url(base_url, target) or target
        elem.set("target", target)


def convert_tags(tree: HtmlElement, options: Extractor, url: str | None = None) -> HtmlElement:
    "Simplify markup and convert relevant HTML tags to an XML standard."
    # delete links for faster processing
    if not options.links:
        containers = ("div", "li", "p", "table") if options.tables else ("div", "li", "p")
        # necessary for further detection, the tree root does not count as a container
        for elem in tree.iter("a"):
            if next(elem.iterancestors(containers), tree) is not tree:
                elem.tag = "ref"
        # strip the rest
        strip_tags(tree, "a")
    else:
        # get base URL for converting relative URLs
        base_url = url and safe_base_url(url)
        for elem in tree.iter("a", "ref"):
            convert_link(elem, base_url)

    # Yoast FAQ blocks: question headers are bold but act as titles (#471)
    for elem in tree.iter("strong"):
        if "schema-faq-question" in elem.get("class", ""):
            elem.attrib.clear()
            elem.set("rend", "h3")
            elem.tag = "head"

    # an empty sup/sub carries nothing to raise or lower, and process_node() would later
    # hand it the following text as its own, so the marker would wrap the wrong words.
    # The tail is kept in every focus mode, unlike the pruning of CUT_EMPTY_ELEMS.
    for elem in tree.iter("sub", "sup"):
        if not elem.text and len(elem) == 0:
            delete_element(elem)

    if options.formatting:
        for elem in tree.iter(REND_TAG_MAPPING.keys()):
            elem.attrib.clear()
            elem.set("rend", REND_TAG_MAPPING[elem.tag])  # type: ignore[index]
            elem.tag = "hi"
    else:
        strip_tags(tree, *REND_TAG_MAPPING.keys())

    # <q> is an inline quotation: unwrapped, it stays in its sentence
    strip_tags(tree, "q")

    # iterate over all concerned elements
    for elem in tree.iter(CONVERSIONS.keys()):
        CONVERSIONS[elem.tag](elem)  # type: ignore[index]
    # images
    if options.images:
        for elem in tree.iter("img"):
            elem.tag = "graphic"
        # move images out of links to prevent duplication/loss
        if options.links:
            for ref in list(tree.iter("ref")):
                graphics = list(ref.iter("graphic"))
                # addnext() would land after the tail text of the link
                for graphic in graphics:
                    if graphic.tail:  # keep the text where the image was
                        prev, parent = graphic.getprevious(), graphic.getparent()
                        if prev is not None:
                            prev.tail = (prev.tail or "") + graphic.tail
                        elif parent is not None:
                            parent.text = (parent.text or "") + graphic.tail
                        graphic.tail = None
                    ref.addprevious(graphic)
                # remove ref if it only contained images (no text),
                # its leftover children (picture, source) would
                # otherwise trigger justext fallback and lose images
                if graphics and not ref.text_content().strip():
                    delete_element(ref)

    return tree


HTML_CONVERSIONS = {
    "list": lambda elem: "ol" if elem.get("rend") == "ol" else "ul",
    "item": "li",
    "code": "pre",
    "quote": "blockquote",
    "head": lambda elem: f"h{int(elem.get('rend', 'h3')[1:])}",
    "lb": "br",
    "graphic": "img",
    "ref": "a",
    "hi": lambda elem: HTML_TAG_MAPPING[elem.get("rend", "#i")],
    "row": "tr",
    "cell": lambda elem: "th" if elem.get("role") == "head" else "td",
}


def convert_to_html(tree: _Element) -> _Element:
    "Convert XML to simplified HTML."
    for elem in tree.iter(HTML_CONVERSIONS.keys()):
        conversion = HTML_CONVERSIONS[str(elem.tag)]
        elem.tag = conversion(elem) if callable(conversion) else conversion  # type: ignore[assignment]
        # handle attributes
        if elem.tag == "a":
            elem.set("href", elem.attrib.pop("target", ""))
        elif elem.tag != "img":  # keep <img> src/alt/title; drop everything else
            elem.attrib.clear()
    tree.tag = "body"
    root = Element("html")
    root.append(tree)
    return root


def build_html_output(document: Document, with_metadata: bool = False) -> str:
    "Convert the document to HTML and return a string."
    html_tree = convert_to_html(document.body)

    if with_metadata:
        head = Element("head")
        for item, value in meta_items(document):
            SubElement(head, "meta", name=item, content=value)
        html_tree.insert(0, head)

    return tostring(html_tree, pretty_print=True, encoding="unicode").strip()
