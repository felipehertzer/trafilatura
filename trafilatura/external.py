# pylint:disable-msg=E0611,I1101
"""
Functions grounding on third-party software.
"""

import logging
import re
from collections.abc import Iterator
from copy import copy

# third-party
from lxml import html
from lxml.etree import SubElement, _Element, strip_tags, tostring
from lxml.html import HtmlElement

# own
from .htmlprocessing import convert_tags, prune_unwanted_nodes, tree_cleaning
from .justext_lite import HEADINGS, extract_paragraphs, tidy_spacing
from .main_extractor import handle_image
from .readability_lxml import Document as ReadabilityDocument  # fork
from .settings import Extractor
from .utils import fromstring_bytes, trim
from .xml import TEI_VALID_TAGS, delete_element
from .xpaths import OVERALL_DISCARD_XPATH

LOGGER = logging.getLogger(__name__)

SANITIZED_XPATH = ".//aside|.//audio|.//button|.//fencedframe|.//fieldset|.//figure|.//footer|.//iframe|.//input|.//label|.//link|.//nav|.//noindex|.//noscript|.//object|.//option|.//select|.//source|.//svg|.//time"

# adopt justext only when the text it replaces is at most this much longer (swept 2-4: every
# (3, 4]-band page was justext wrongly replacing a longer, closer-to-target extraction)
JUSTEXT_OVERRIDE_RATIO = 3


def try_readability(htmlinput: HtmlElement) -> HtmlElement:
    """Safety net: try with the generic algorithm readability"""
    try:
        doc = ReadabilityDocument(htmlinput, min_text_length=25)
        # force conversion to utf-8 (see #319)
        summary = fromstring_bytes(doc.summary())
        return summary if summary is not None else HtmlElement()
    except Exception as err:
        LOGGER.warning("readability_lxml failed: %s", err)
        return HtmlElement()


# headings, list items and story paragraphs readability holds that the own text lacks, against the own text's length
MISSED_STRUCTURE_RATIO = 0.5
# a story paragraph: long prose holding a sentence, not a teaser line or a menu entry
STORY_PARAGRAPH_LENGTH = 80
STORY_SENTENCE = re.compile(r"[.!?…][\"'’”)]?(?:\s|$)")
# more prose than a teaser card holds: a run of story, in one paragraph or in a block of its own
STORY_BLOCK_LENGTH = 400
# elements whose text is a paragraph of its own, not part of the inline text of their parent
BLOCK_TAGS = frozenset(
    {"address", "article", "aside", "blockquote", "div", "dl", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5",
     "h6", "header", "li", "main", "nav", "ol", "p", "pre", "section", "table", "ul"}
)  # fmt: skip


def _normalized_text(elem: _Element) -> str:
    return " ".join("".join(elem.itertext()).split())


def _opens_with_link(elem: _Element) -> bool:
    "A teaser opens with the linked headline of the story it teases."
    first = next(iter(elem), None)
    return not (elem.text or "").strip() and first is not None and first.tag == "a"


def _text_runs(block: _Element) -> list[tuple[str, bool]]:
    """The paragraphs right under a block, in document order, each with whether it is a list item: its p and li
    children (not teasers) and the inline text between its other block children, which holds the story when markup
    lost its p tags (an unclosed inline element)."""
    runs: list[tuple[list[str], bool]] = []
    run: list[str] = []

    def walk(node: _Element) -> None:
        nonlocal run
        run.append(node.text or "")
        for child in node:
            if isinstance(child.tag, str) and child.tag in BLOCK_TAGS:
                runs.append((run, False))
                run = []
                if child.tag in ("li", "p") and not _opens_with_link(child):
                    runs.append((list(child.itertext()), child.tag == "li"))
            elif isinstance(child.tag, str):
                walk(child)
            run.append(child.tail or "")

    walk(block)
    runs.append((run, False))
    return [(text, item) for text, item in ((" ".join("".join(parts).split()), item) for parts, item in runs) if text]


def _wraps_own_extraction(body: _Element, algo_body: HtmlElement, algo_text: str, len_text: int, options: Extractor) -> bool:
    """Own extraction is substantial and wholly contained in readability's output, which holds no headings,
    lists or story of the article that the own text lacks.

    Readability then only adds page chrome around the same article (header,
    captions, datelines, teaser cards, menus), so its greater length is not
    missing content. Headings and list items between the own paragraphs that the
    own text lacks are the article's: the own extraction skipped them (text
    recovered without a body frame keeps paragraphs only). So is story prose the
    own text lacks: a paragraph, a list item or text whose p tags the markup
    lost, between the own paragraphs, in the run that holds them (the own
    extraction stopped early or skipped some), long enough on its own to be more
    than a teaser, or adding up to as much in a block beside the own text (the
    own extraction took the lead of a story whose body sits next to it). In these
    cases readability's length is content. A teaser card holds a short paragraph
    of its own or opens with the link to its story, and a site's menu sits in the
    own text's wrapper: they stay chrome.
    """
    if len_text < 4 * options.min_extracted_size:
        return False
    paragraphs = [_normalized_text(p) for p in body.iter("p")]
    paragraphs = [p for p in paragraphs if len(p) >= 40]
    if len(paragraphs) < 3:
        return False
    algo_normalized = " ".join(algo_text.split())
    starts = [algo_normalized.find(p) for p in paragraphs]
    if min(starts) < 0:
        return False
    first = min(starts)
    last = max(start + len(p) for start, p in zip(starts, paragraphs, strict=True))
    own = " ".join(" ".join(body.itertext()).split())
    missed = 0
    for elem in algo_body.iter("h1", "h2", "h3", "h4", "h5", "h6", "li"):
        text = _normalized_text(elem)
        if text and text not in own and first < algo_normalized.find(text) < last:
            missed += len(text)
    # story paragraphs the own text lacks: between its paragraphs, in their run (the own extraction stopped early or
    # skipped some), in a long run of prose, or in a block of their own beside it (the own extraction took the lead)
    own_paragraphs = set(paragraphs)
    for block in algo_body.iter(*BLOCK_TAGS - HEADINGS - {"li", "p"}):
        texts = _text_runs(block)
        story = [
            (text, position, item)
            for text, item in texts
            if len(text) >= STORY_PARAGRAPH_LENGTH
            and STORY_SENTENCE.search(text)
            and text not in own
            and not any(p in text for p in paragraphs)
            and (position := algo_normalized.find(text)) >= 0
        ]
        if not story:
            continue
        own_run = any(text in own_paragraphs for text, _ in texts)
        beside = sum(len(text) for text, _, _ in story) >= STORY_BLOCK_LENGTH and not any(
            p in _normalized_text(block) for p in paragraphs
        )
        # list items between the own paragraphs are counted above
        missed += sum(
            len(text)
            for text, position, item in story
            if (own_run or beside or len(text) >= STORY_BLOCK_LENGTH or first < position < last)
            and not (item and first < position < last)
        )
    return missed < MISSED_STRUCTURE_RATIO * len_text


def _prefer_readability(
    body: _Element,
    algo_body: HtmlElement,
    algo_text: str,
    len_text: int,
    len_algo: int,
    options: Extractor,
) -> bool:
    """Decide whether the readability output should replace the own extraction."""
    # readability empty, or same length as the own extraction (assumed same content)
    if len_algo in (0, len_text):
        return False
    # own extraction much longer
    if len_text > 2 * len_algo:
        return False
    return (
        # own text empty
        len_text == 0
        # readability much longer, unless it grabbed raw JSON (#632) or only
        # wraps a substantial own extraction in page chrome
        or (
            len_algo > 2 * len_text
            and not algo_text.startswith("{")
            and not _wraps_own_extraction(body, algo_body, algo_text, len_text, options)
        )
        # own extraction structurally deficient: no paragraph text or table-dominated
        or (
            len_algo > options.min_extracted_size * 2
            and (not body.xpath(".//p//text()") or len(body.findall(".//table")) > len(body.findall(".//p")))
        )
        # recall mode: readability output substantially longer
        or (options.focus == "recall" and len_algo > 1.5 * len_text and not algo_text.startswith("{"))
        # recall mode: readability recovers a headed article (#354)
        or (
            options.focus == "recall"
            and not body.xpath(".//head")
            and next(algo_body.iter("h2", "h3", "h4"), None) is not None
            and len_algo > len_text
        )
    )


def compare_extraction(
    tree: HtmlElement,
    body: _Element,
    text: str,
    options: Extractor,
) -> tuple[_Element, str]:
    """Decide whether to choose own or external extraction based on a series of heuristics.
    Copies of the raw ``tree`` feed readability (uncleaned) and justext (tree_cleaning'd)."""
    len_text = len(text)
    # bypass for recall
    if options.focus == "recall" and len_text > options.min_extracted_size * 10:
        return body, text

    raw_tree = copy(tree)
    # prior cleaning
    if options.focus == "precision":
        raw_tree = prune_unwanted_nodes(raw_tree, OVERALL_DISCARD_XPATH)

    # try with readability
    temppost_algo = try_readability(raw_tree)
    # unicode fix necessary on certain systems (#331)
    algo_text = trim(tostring(temppost_algo, method="text", encoding="utf-8").decode("utf-8"))
    len_algo = len(algo_text)
    LOGGER.debug("extracted length: %s (algorithm) %s (extraction)", len_algo, len_text)

    use_readability = _prefer_readability(body, temppost_algo, algo_text, len_text, len_algo, options)
    if use_readability:
        body, text, len_text = temppost_algo, algo_text, len_algo
    LOGGER.debug("using %s extraction: %s", "generic" if use_readability else "custom", options.source)

    # override faulty extraction: try with justext
    unclean = bool(body.xpath(SANITIZED_XPATH))
    if unclean or len_text < options.min_extracted_size:
        LOGGER.debug("unclean or short document triggering justext examination: %s", options.source)
        body2, text2 = justext_rescue(tree_cleaning(copy(tree), options), options)
        len_text2 = len(text2)
        # must add text (#896), or on an unclean page may be shorter (boilerplate dropped) unless image-heavy
        accept = len_text2 > len_text or (
            unclean
            and len_text <= JUSTEXT_OVERRIDE_RATIO * len_text2
            and (not options.images or body.find(".//graphic") is None)
        )
        if text2 and accept:
            LOGGER.debug("using justext, length: %s", len_text2)
            body, text, len_text = body2, text2, len_text2
            use_readability = False

    # post-processing: remove unwanted sections
    if use_readability:
        body, text = sanitize_tree(body, options)  # type: ignore[arg-type]

    return body, text


def _blocks(body: _Element) -> Iterator[_Element]:
    "Output blocks of try_justext: paragraphs, headings, quotes, code and list items."
    for elem in body:
        yield from elem if elem.tag == "list" else (elem,)


def try_justext(tree: HtmlElement, url: str | None, *, options: Extractor | None = None) -> _Element:
    """Second safety net: paragraph classification derived from jusText.
    Links, formatting and images are kept according to the options."""
    if options is None:
        options = Extractor()
        options.url = url
    result_body = html.Element("body")
    try:
        current_list, current_id = None, 0
        for _, kind, list_id, block in extract_paragraphs(tree):
            if kind in ("ol", "ul"):
                if current_list is None or list_id != current_id:
                    current_list, current_id = SubElement(result_body, "list", rend=kind), list_id
                block.tag = "item"
                current_list.append(block)
                continue
            current_list = None
            if kind in HEADINGS:
                block.tag = "head"
                block.set("rend", kind)
            else:
                block.tag = kind
            result_body.append(block)
        convert_markup(result_body, options)
        for elem in _blocks(result_body):
            if elem.tag != "code":
                tidy_spacing(elem)
    except Exception as err:
        LOGGER.error("justext %s %s", err, url)
        result_body = html.Element("body")
    return result_body


def justext_rescue(tree: HtmlElement, options: Extractor) -> tuple[_Element, str]:
    """Try to use justext algorithm as a second fallback"""
    temppost_algo = try_justext(tree, options.url, options=options)
    # blocks joined by a space, inline markup without: the length of the plain text
    return temppost_algo, trim(" ".join("".join(block.itertext()) for block in _blocks(temppost_algo)))


def sanitize_tree(tree: HtmlElement, options: Extractor) -> tuple[HtmlElement, str]:
    """Convert and sanitize the output from the generic algorithm (post-processing)"""
    cleaned_tree = convert_markup(tree_cleaning(tree, options), options)
    return cleaned_tree, trim(" ".join(cleaned_tree.itertext()))


def convert_markup(tree: HtmlElement, options: Extractor) -> HtmlElement:
    "Convert HTML markup to the output format according to the options, other tags stripped."
    strip_tags(tree, "span", *(() if options.links else ("a",)))
    # pass url so relative links are absolutized on the fallback path
    tree = convert_tags(tree, options, options.url)
    for elem in list(tree.iter("graphic")):
        image = handle_image(elem, options)
        if image is None:
            delete_element(elem)
        else:
            elem.attrib.clear()
            elem.attrib.update(image.attrib)
    # Mark first <th>-containing row per parent group as head (mirrors handle_table logic).
    # Groups by direct parent (the enclosing table once tbody/thead/tfoot are stripped upstream);
    # nested tables form their own group.
    seen_group_elems: set[_Element | None] = set()
    for tr in tree.iter("tr"):
        parent = tr.getparent()
        if parent not in seen_group_elems and tr.find("th") is not None:
            seen_group_elems.add(parent)
            for c in tr.iterchildren("th"):
                c.set("role", "head")
    for elem in tree.iter("td", "th", "tr"):
        elem.tag = "row" if elem.tag == "tr" else "cell"
    strip_tags(tree, *({str(elem.tag) for elem in tree.iter("*")} - TEI_VALID_TAGS))
    return tree
