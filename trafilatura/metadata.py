"""
Module bundling all functions needed to scrape metadata from webpages.
"""

import json
import logging
import re
from copy import deepcopy
from html import unescape
from typing import Any

from courlan import (
    extract_domain,
    is_valid_url,
    normalize_url,
    validate_url,
)
from htmldate import find_date
from lxml.etree import XPath
from lxml.html import HtmlElement, tostring

from .baseline import block_text
from .json_metadata import (
    extract_json,
    extract_json_parse_error,
    normalize_authors,
    normalize_json,
    normalize_json_values,
)
from .settings import Document, set_date_params
from .utils import HTML_STRIP_TAGS, line_processing, load_html, safe_base_url, trim
from .xml import delete_element
from .xpaths import (
    AUTHOR_DISCARD_XPATHS,
    AUTHOR_XPATHS,
    CATEGORIES_XPATHS,
    TAGS_XPATHS,
    TITLE_XPATHS,
)

__all__ = ["Document"]

LOGGER = logging.getLogger(__name__)
logging.getLogger("htmldate").setLevel(logging.WARNING)

META_URL = re.compile(r"https?://(?:www\.|w[0-9]+\.)?([^/]+)")

JSON_MINIFY = re.compile(r'("(?:\\.|[^"\\])*")|\s')

HTMLTITLE_REGEX = re.compile(r"^(.+)?\s+[–•·—|⁄*⋆~‹«<›»>:-]\s+(.+)$")  # part without dots?
# host name: dot-separated labels ending in a top-level domain of letters (or a punycode one)
HOSTNAME_REGEX = re.compile(r"(?:[\w-]+\.)+(?:[^\W\d_]{2,}|xn--[a-z\d-]+)")

CLEAN_META_TAGS = re.compile(r'["\']')

LICENSE_REGEX = re.compile(r"/(by-nc-nd|by-nc-sa|by-nc|by-nd|by-sa|by|zero)/([1-9]\.[0-9])")
TEXT_LICENSE_REGEX = re.compile(
    r"(cc|creative commons) (by-nc-nd|by-nc-sa|by-nc|by-nd|by-sa|by|zero) ?([1-9]\.[0-9])?",
    re.IGNORECASE,
)

METANAME_AUTHOR = {
    "article:author",
    "atc-metaauthor",
    "author",
    "authors",
    "byl",
    "citation_author",
    "creator",
    "dc.creator",
    "dc.creator.aut",
    "dc:creator",
    "dcterms.creator",
    "dcterms.creator.aut",
    "dcsext.author",
    "parsely-author",
    "rbauthors",
    "sailthru.author",
    "shareaholic:article_author_name",
}  # questionable: twitter:creator
METANAME_DESCRIPTION = {
    "dc.description",
    "dc:description",
    "dcterms.abstract",
    "dcterms.description",
    "description",
    "sailthru.description",
    "twitter:description",
}
METANAME_PUBLISHER = {
    "article:publisher",
    "citation_journal_title",
    "copyright",
    "dc.publisher",
    "dc:publisher",
    "dcterms.publisher",
    "publisher",
    "sailthru.publisher",
    "rbpubname",
    "twitter:site",
}  # questionable: citation_publisher
METANAME_TAG = {
    "citation_keywords",
    "dcterms.subject",
    "keywords",
    "parsely-tags",
    "shareaholic:keywords",
    "tags",
}
METANAME_TITLE = {
    "citation_title",
    "dc.title",
    "dcterms.title",
    "fb_title",
    "headline",
    "parsely-title",
    "sailthru.title",
    "shareaholic:title",
    "rbtitle",
    "title",
    "twitter:title",
}
METANAME_IMAGE = {
    "image",
    "og:image",
    "og:image:url",
    "og:image:secure_url",
    "twitter:image",
    "twitter:image:src",
}
PROPERTY_AUTHOR = {"author", "article:author"}
TWITTER_ATTRS = {"twitter:site", "application-name"}

# also interesting: article:section

EXTRA_META = {"charset", "http-equiv", "property"}

OG_PROPERTIES = {
    "og:title": "title",
    "og:description": "description",
    "og:site_name": "sitename",
    "og:image": "image",
    "og:image:url": "image",
    "og:image:secure_url": "image",
    "og:type": "pagetype",
}

OG_AUTHOR = {"og:author", "og:article:author"}

URL_SELECTORS = ['.//head//link[@rel="canonical"]', ".//head//base", './/head//link[@rel="alternate"][@hreflang="x-default"]']


def normalize_tags(tags: str) -> str:
    """Remove special characters of tags"""
    trimmed = trim(unescape(tags))
    if not trimmed:
        return ""
    tags = CLEAN_META_TAGS.sub(r"", trimmed)
    return ", ".join(filter(None, tags.split(", ")))


def invert_citation_name(name: str) -> str:
    "citation_author holds one name per tag, often as 'Last, First': turn it into 'First Last'."
    last, sep, first = (part.strip() for part in name.partition(","))
    # leave lists of full names ("Jane Doe, John Roe") to the regular splitting
    if sep and last and first and "," not in first and (" " not in last or " " not in first):
        return f"{first} {last}"
    return name


def check_authors(authors: str, author_blacklist: set[str]) -> str | None:
    "Check if the authors string correspond to expected values."
    author_blacklist = {a.lower() for a in author_blacklist}
    new_authors = [author.strip() for author in authors.split(";") if author.strip().lower() not in author_blacklist]
    if new_authors:
        return "; ".join(new_authors).strip("; ")
    return None


def extract_meta_json(tree: HtmlElement, metadata: Document) -> Document:
    """Parse and extract metadata from JSON-LD data"""
    for elem in tree.xpath('.//script[@type="application/ld+json" or @type="application/settings+json"]'):
        if not elem.text:
            continue
        element_text = JSON_MINIFY.sub(r"\1", elem.text)
        try:
            # strict=False: trim() handles \n\r\t, but strict JSON rejects the full 0x00-0x1F
            # range; rarer control chars (from encoding issues) would otherwise raise here
            # parse before normalizing: decoding the escapes in the raw text would turn an escaped
            # quote into a string delimiter and escaped angle brackets into tags spanning fields
            schema = normalize_json_values(json.loads(element_text, strict=False))
        except json.JSONDecodeError:
            element_text = normalize_json(element_text)
            try:
                schema = json.loads(element_text, strict=False)
            except json.JSONDecodeError:
                metadata = extract_json_parse_error(element_text, metadata)
                continue
        metadata = extract_json(schema, metadata)
    return metadata


def extract_opengraph(tree: HtmlElement) -> dict[str, str | None]:
    """Search meta tags following the OpenGraph guidelines (https://ogp.me/)"""
    result = dict.fromkeys(("title", "author", "url", "description", "sitename", "image", "pagetype"))

    # detect OpenGraph schema
    for elem in tree.xpath('.//head/meta[starts-with(@property, "og:")]'):
        property_name, content = elem.get("property"), elem.get("content")
        # safeguard
        if content and not content.isspace():
            if property_name in OG_PROPERTIES:
                result[OG_PROPERTIES[property_name]] = content
            elif property_name == "og:url" and is_valid_url(content):
                result["url"] = content
            elif property_name in OG_AUTHOR:
                result["author"] = normalize_authors(None, content)
        # og:locale
        # elif elem.get('property') == 'og:locale':
        #    pagelocale = elem.get('content')
    return result


def examine_meta(tree: HtmlElement) -> Document:
    """Search meta tags for relevant information"""
    # bootstrap from potential OpenGraph tags
    metadata = Document().from_dict(extract_opengraph(tree))

    tags, backup_sitename = [], None

    # iterate through meta tags
    for elem in tree.iterfind(".//head/meta[@content]"):
        # content
        content_attr = HTML_STRIP_TAGS.sub("", elem.get("content", "")).strip()
        if not content_attr:
            continue
        # todo: image info
        # ...
        # property
        if "property" in elem.attrib:
            property_attr = elem.get("property", "").lower()
            # no opengraph a second time
            if property_attr.startswith("og:"):
                continue
            if property_attr == "article:tag":
                tags.append(normalize_tags(content_attr))
            elif property_attr in PROPERTY_AUTHOR:
                metadata.author = normalize_authors(metadata.author, content_attr)
            elif property_attr == "article:publisher":
                metadata.sitename = metadata.sitename or content_attr
            elif property_attr in METANAME_IMAGE:
                metadata.image = metadata.image or content_attr
        # name attribute
        elif "name" in elem.attrib:
            name_attr = elem.get("name", "").lower()
            # author
            if name_attr in METANAME_AUTHOR:
                if name_attr == "citation_author":
                    content_attr = invert_citation_name(content_attr)
                metadata.author = normalize_authors(metadata.author, content_attr)
            # title
            elif name_attr in METANAME_TITLE:
                metadata.title = metadata.title or content_attr
            # description
            elif name_attr in METANAME_DESCRIPTION:
                metadata.description = metadata.description or content_attr
            # site name
            elif name_attr in METANAME_PUBLISHER:
                metadata.sitename = metadata.sitename or content_attr
            # image
            elif name_attr in METANAME_IMAGE:
                metadata.image = metadata.image or content_attr
            # twitter
            elif name_attr in TWITTER_ATTRS or "twitter:app:name" in name_attr:
                backup_sitename = content_attr
            # url
            elif name_attr == "twitter:url" and not metadata.url and is_valid_url(content_attr):
                metadata.url = content_attr
            # keywords
            elif name_attr in METANAME_TAG:  # 'page-topic'
                tags.append(normalize_tags(content_attr))
        elif "itemprop" in elem.attrib:
            itemprop_attr = elem.get("itemprop", "").lower()
            if itemprop_attr == "author":
                metadata.author = normalize_authors(metadata.author, content_attr)
            elif itemprop_attr == "description":
                metadata.description = metadata.description or content_attr
            elif itemprop_attr == "headline":
                metadata.title = metadata.title or content_attr
            # to verify:
            # elif itemprop_attr == 'name':
            #    if title is None:
            #        title = elem.get('content')
        # other types
        elif all(key not in elem.attrib for key in EXTRA_META):
            LOGGER.debug(
                "unknown attribute: %s",
                tostring(elem, pretty_print=False, encoding="unicode").strip(),
            )

    # backups
    metadata.sitename = metadata.sitename or backup_sitename
    # copy
    metadata.tags = tags
    # metadata.set_attributes(tags=tags)
    return metadata


def extract_metainfo(tree: HtmlElement, expressions: list[XPath], len_limit: int = 200) -> str | None:
    """Extract meta information"""
    # try all XPath expressions
    for expression in expressions:
        # examine all results
        results = expression(tree)
        for elem in results:
            content = trim(" ".join(elem.itertext()))
            if content and 2 < len(content) < len_limit:
                return content
        if len(results) > 1:
            LOGGER.debug("more than one invalid result: %s %s", expression, len(results))
    return None


def examine_title_element(
    tree: HtmlElement,
) -> tuple[str, str | None, str | None]:
    """Extract text segments out of main <title> element."""
    title = ""
    title_element = tree.find(".//head//title")
    if title_element is not None:
        title = trim(title_element.text_content())
        if match := HTMLTITLE_REGEX.match(title):
            return title, match[1], match[2]
    LOGGER.debug("no main title found")
    return title, None, None


def extract_title(tree: HtmlElement) -> str | None:
    """Extract the document title"""
    # only one h1-element: take it
    h1_results = tree.findall(".//h1")
    if len(h1_results) == 1 and (title := block_text(h1_results[0])):
        return title
    # extract using x-paths
    title = extract_metainfo(tree, TITLE_XPATHS) or ""
    if title:
        return title
    # extract using title tag
    title, first, second = examine_title_element(tree)
    for t in (first, second, title):
        if t and "." not in t:
            return t
    # take first non-empty h1-title
    for h1_result in h1_results:
        if title := block_text(h1_result):
            return title
    # take first h2-title
    if h2_results := tree.xpath(".//h2"):
        title = block_text(h2_results[0])
    else:
        LOGGER.debug("no h2 title found")
    return title or None


def byline_discards(tree: HtmlElement) -> set[HtmlElement]:
    "The elements a byline is not read from. Evaluated once per page: the byline and the story credit share them."
    return {elem for expression in AUTHOR_DISCARD_XPATHS for elem in expression(tree)}


def extract_author(tree: HtmlElement, discarded: set[HtmlElement]) -> str | None:
    """Extract the document author(s)"""
    # a copy holds its elements in the order of the original: the discarded ones leave the copy
    subtree = deepcopy(tree)
    for elem in [copy for original, copy in zip(tree.iter(), subtree.iter(), strict=True) if original in discarded]:
        delete_element(elem)  # the tail text is preserved
    author = extract_metainfo(subtree, AUTHOR_XPATHS, len_limit=120)
    if author:
        author = normalize_authors(None, author)
    # copyright?
    return author


# The credit a story opens with, between its headline and its first sentence: a "By" line ("By: Jane Doe"),
# or a full name over the dateline in the same block ("Jane Doe<br>09 Nov 2026, 04:06 GMT+10").
STORY_BY_LINE = re.compile(
    r"^(?:by|written by|words by|story by|words and pictures by|reported by|report by)\s*:?\s+(?=\S)", re.IGNORECASE
)
STORY_DATELINE = re.compile(r"\b(?:19|20)\d{2}\b|\b\d{1,2}:\d{2}\b")
# a dateline opens with the date: a number, or a month or weekday before the day ("Sep 12, 2025"); a label before
# a date ("Date 28.01.2022" under "Venue Townhall") makes the line above a label too
STORY_DATELINE_START = re.compile(r"^(?:\d|[^\W\d_]+\.?,?\s+\d{1,2}(?:st|nd|rd|th)?\b(?![.:/]\d))")
STORY_SENTENCE_END = re.compile(r"[.!?…][\"'’”)]?$")
STORY_SENTENCE_WORDS = 15
STORY_CREDIT_LINES = 12
# a name's word: a capital and letters, with apostrophes, hyphens or an initial's dot ("O'Neil", "J.")
NAME_WORD = re.compile(r"^[^\W\d_][^\W\d_'’.\-]*(?:['’.\-][^\W\d_]*)*\.?$")
NAME_PARTICLES = frozenset(
    {"al", "bin", "binti", "da", "das", "de", "del", "della", "den", "der", "di", "do", "dos", "du", "la", "le", "ter",
     "van", "von"}
)  # fmt: skip
NAMES_JOIN = re.compile(r"\s+(?:and|&)\s+|,\s+")
INLINE_TAGS = frozenset(
    {"a", "abbr", "b", "bdi", "bdo", "cite", "data", "del", "em", "font", "i", "img", "ins", "label", "mark", "q", "s",
     "small", "span", "strong", "sub", "sup", "time", "u", "wbr"}
)  # fmt: skip
STORY_SKIP_TAGS = frozenset(
    {"button", "figcaption", "figure", "form", "head", "iframe", "input", "link", "meta", "nav", "noscript", "option",
     "script", "select", "style", "svg", "template", "textarea", "title"}
)  # fmt: skip
HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


def _normalize_space(text: str) -> str:
    return " ".join(text.split())


def _is_story_sentence(line: str) -> bool:
    "Prose: a line that ends a sentence or runs long."
    return bool(STORY_SENTENCE_END.search(line)) or len(line.split()) >= STORY_SENTENCE_WORDS


def _is_dateline(line: str) -> bool:
    return (
        STORY_DATELINE_START.match(line) is not None
        and STORY_DATELINE.search(line) is not None
        and len(line.split()) <= 8
        and not _is_story_sentence(line)
    )


def _is_full_name(text: str) -> bool:
    "Two to five capitalised words (particles aside): a full name, not a label, a brand or a phrase."
    words = text.split()
    return 2 <= len(words) <= 5 and all(
        word.casefold() in NAME_PARTICLES or (NAME_WORD.match(word) is not None and word[0].isupper()) for word in words
    )


def _line_parts(elem: HtmlElement, discarded: set[HtmlElement], lines: list[tuple[list[str], list[str]]]) -> None:
    "Add the inline text of elem to the current line; a line break or a child block starts a new one."
    for child in elem:
        if not isinstance(child.tag, str) or child in discarded:
            lines[-1][0].append(child.tail or "")
        elif child.tag == "br":
            lines.append(([child.tail or ""], []))
        elif child.tag in INLINE_TAGS:
            # a profile link names the author; an in-page link ("#respond": "No Comments") does not
            if child.tag == "a":
                lines[-1][1].append(_normalize_space(child.text_content()) if "#" not in child.get("href", "#") else "")
            lines[-1][0].append(" " + (child.text or ""))
            _line_parts(child, discarded, lines)
            lines[-1][0].append(" " + (child.tail or ""))
        else:
            lines.append(([child.tail or ""], []))


def _story_lines(
    tree: HtmlElement, headline: HtmlElement, discarded: set[HtmlElement]
) -> list[tuple[str, int, str, list[str], bool]]:
    "The text lines after the headline, block by block: tag, block number, text, linked names, any links at all."
    result: list[tuple[str, int, str, list[str], bool]] = []
    started, block = False, 0
    for elem in tree.iter():
        if elem is headline:
            started = True
            continue
        if not started or not isinstance(elem.tag, str) or elem.tag in INLINE_TAGS or elem.tag in STORY_SKIP_TAGS:
            continue
        if elem in discarded or any(
            parent is headline or parent in discarded or parent.tag in STORY_SKIP_TAGS for parent in elem.iterancestors()
        ):
            continue
        lines: list[tuple[list[str], list[str]]] = [([elem.text or ""], [])]
        _line_parts(elem, discarded, lines)
        block += 1
        for parts, links in lines:
            text = _normalize_space("".join(parts))
            if text:
                result.append((elem.tag, block, text, [link for link in links if link], bool(links)))
        if len(result) >= STORY_CREDIT_LINES:
            break
    return result[:STORY_CREDIT_LINES]


def extract_story_credit(tree: HtmlElement, title: str | None, discarded: set[HtmlElement] | None = None) -> str | None:
    """The names a story's own credit gives, between its headline (the h1 or h2 that is its title) and its first
    sentence, read as extract_author reads the page (AUTHOR_DISCARD_XPATHS left out)."""
    if not title:
        return None
    wanted = _normalize_space(title).casefold()
    headline = next(
        (elem for elem in tree.iter("h1", "h2") if _normalize_space(elem.text_content()).casefold() == wanted), None
    )
    # no heading is the title: nothing to read, and no need to look for what to leave out
    if headline is None:
        return None
    if discarded is None:
        discarded = byline_discards(tree)
    lines = _story_lines(tree, headline, discarded)
    for index, (tag, block, line, links, linked) in enumerate(lines):
        if _is_story_sentence(line):
            return None
        if tag in HEADING_TAGS or line.casefold() == wanted:
            continue
        if match := STORY_BY_LINE.match(line):
            # a linked by-line names the people it links to, not what sits beside them ("Jdoe Community
            # Contributor"); one linking only within the page names no one
            names = links if linked else [part for part in NAMES_JOIN.split(line[match.end() :]) if part]
            if names and all(_is_full_name(name) for name in names):
                return "; ".join(links) if linked else line[match.end() :]
            continue
        if index + 1 < len(lines):
            _, next_block, next_line, _, _ = lines[index + 1]
            if next_block == block and _is_full_name(line) and _is_dateline(next_line):
                return line
    return None


def _holds_names(credit: str, names: str) -> bool:
    "Whether a credit holds every word of the names."
    held = {word.casefold() for word in re.findall(r"[^\W\d_]+", credit)}
    return all(word.casefold() in held for word in re.findall(r"[^\W\d_]+", names))


def extract_url(tree: HtmlElement, default_url: str | None = None) -> str | None:
    """Extract the URL from the canonical link"""
    for selector in URL_SELECTORS:
        element = tree.find(selector)
        url = element.attrib.get("href") if element is not None else None
        if url:
            break

    # protocol-relative URL: only the scheme is missing
    if url and url.startswith("//"):
        url = f"https:{url}"
    # fix relative URLs
    elif url and url.startswith("/"):
        for element in tree.iterfind(".//head//meta[@content]"):
            attrtype = element.get("name") or element.get("property") or ""
            if attrtype.startswith(("og:", "twitter:")):
                base_url = safe_base_url(element.attrib["content"])
                if base_url:
                    # prepend URL
                    url = base_url + url
                    break

    # do not return invalid URLs
    if url:
        validation_result, parsed_url = validate_url(url)
        url = normalize_url(parsed_url) if validation_result and parsed_url else None

    return url or default_url


def extract_sitename(tree: HtmlElement) -> str | None:
    """Extract the name of a site from the main title (if it exists)"""
    _, *parts = examine_title_element(tree)
    return next((part for part in parts if part and HOSTNAME_REGEX.fullmatch(part)), None)


def extract_catstags(metatype: str, tree: HtmlElement) -> list[str]:
    """Find category and tag information"""
    results: list[str] = []
    regexpr = "/" + metatype.rstrip("y") + "(?:y|ies|s)?/"
    xpath_expression = CATEGORIES_XPATHS if metatype == "category" else TAGS_XPATHS
    # search using custom expressions
    for catexpr in xpath_expression:
        results.extend(block_text(elem) for elem in catexpr(tree) if re.search(regexpr, elem.attrib["href"]))
        if results:
            break
    # category fallback
    if metatype == "category" and not results:
        results.extend(
            element.attrib["content"]
            for element in tree.xpath('.//head//meta[@property="article:section" or contains(@name, "subject")][@content]')
        )
        # optional: search through links
        # if not results:
        #    for elem in tree.xpath('.//a[@href]'):
        #        search for 'category'
    return [r for r in dict.fromkeys(line_processing(x) for x in results if x) if r]


def parse_license_element(element: HtmlElement, strict: bool = False) -> str | None:
    """Probe a link for identifiable free license cues.
    Parse the href attribute first and then the link text."""
    # look for Creative Commons elements
    match = LICENSE_REGEX.search(element.get("href", ""))
    if match:
        return f"CC {match[1].upper()} {match[2]}"
    text = block_text(element)
    if text:
        # check if it could be a CC license
        if strict:
            match = TEXT_LICENSE_REGEX.search(text)
            return match[0] if match else None
        return text
    return None


def extract_license(tree: HtmlElement) -> str | None:
    """Search the HTML code for license information and parse it."""
    # look for links labeled as license
    for element in tree.findall(".//a[@rel][@href]"):
        if "license" not in element.get("rel", "").lower().split():
            continue
        result = parse_license_element(element, strict=False)
        if result is not None:
            return result
    # probe footer elements for CC links
    for element in tree.xpath('.//footer//a[@href]|.//div[contains(@class, "footer") or contains(@id, "footer")]//a[@href]'):
        result = parse_license_element(element, strict=True)
        if result is not None:
            return result
    return None


def extract_metadata(
    filecontent: HtmlElement | str,
    default_url: str | None = None,
    date_config: dict[str, Any] | None = None,
    extensive: bool = True,
    author_blacklist: set[str] | None = None,
) -> Document:
    """Main process for metadata extraction.

    Args:
        filecontent: HTML code as string or parsed tree.
        default_url: Previously known URL of the downloaded document.
        date_config: Provide extraction parameters to htmldate as dict().
        extensive: Use extensive search for date extraction.
        author_blacklist: Provide a blacklist of Author Names as set() to filter out authors.

    Returns:
        A trafilatura.settings.Document containing the extracted metadata information.
        The Document class has .as_dict() method that will return a copy as a dict.
    """
    # init
    author_blacklist = author_blacklist or set()
    date_config = {**date_config} if date_config else set_date_params(extensive)

    # load contents
    tree = load_html(filecontent)
    if tree is None:
        return Document()

    # initialize dict and try to strip meta tags
    metadata = examine_meta(tree)

    # to check: remove it and replace with author_blacklist in test case
    if metadata.author and " " not in metadata.author:
        metadata.author = None

    # fix: try json-ld metadata and override
    try:
        metadata = extract_meta_json(tree, metadata)
    except Exception as err:  # bugs in json_metadata.py
        LOGGER.warning("error in JSON metadata extraction: %s", err)

    # title
    if not metadata.title:
        metadata.title = extract_title(tree)

    # check author in blacklist
    if metadata.author and author_blacklist:
        metadata.author = check_authors(metadata.author, author_blacklist)
    # author
    byline_author = None
    discarded = None
    if not metadata.author:
        discarded = byline_discards(tree)
        byline_author = extract_author(tree, discarded)
        # a blacklisted byline is none: the story credit may name the author
        if byline_author and author_blacklist:
            byline_author = check_authors(byline_author, author_blacklist)
        metadata.author = byline_author
    # The credit a story opens with names its writer. It fills a page without a byline, and it outranks a meta
    # or JSON-LD author naming someone else, such as the account that published the story; a byline the page
    # marks as one stays.
    if not byline_author and (credit := extract_story_credit(tree, metadata.title, discarded)):
        # normalize_authors must keep the whole credit: what it cuts was no name
        names = normalize_authors(None, credit)
        credit = names if names and _holds_names(names, credit.replace(" and ", " ")) else None
        if credit and author_blacklist:
            credit = check_authors(credit, author_blacklist)
        # an author and a credit where one holds the other's names keep the author (co-authors the meta adds)
        if credit and not (
            metadata.author and (_holds_names(metadata.author, credit) or _holds_names(credit, metadata.author))
        ):
            metadata.author = credit
    # recheck author in blacklist
    if metadata.author and author_blacklist:
        metadata.author = check_authors(metadata.author, author_blacklist)

    # url
    if not metadata.url:
        metadata.url = extract_url(tree, default_url)

    # hostname
    if metadata.url:
        metadata.hostname = extract_domain(metadata.url, fast=True)

    # extract date with external module htmldate
    date_config["url"] = metadata.url
    metadata.date = find_date(tree, **date_config)

    # sitename
    if not metadata.sitename:
        metadata.sitename = extract_sitename(tree)
    if metadata.sitename:
        # scrap Twitter ID
        metadata.sitename = metadata.sitename.lstrip("@")
        # capitalize
        if metadata.sitename and "." not in metadata.sitename and not metadata.sitename[0].isupper():
            metadata.sitename = metadata.sitename.title()
    # use URL
    elif metadata.url:
        mymatch = META_URL.match(metadata.url)
        if mymatch:
            metadata.sitename = mymatch[1]

    # categories
    if not metadata.categories:
        metadata.categories = extract_catstags("category", tree)

    # tags
    if not metadata.tags:
        metadata.tags = extract_catstags("tag", tree)

    # license
    metadata.license = extract_license(tree)

    # safety checks
    metadata.filedate = date_config.get("max_date")
    metadata.clean_and_trim()

    return metadata
