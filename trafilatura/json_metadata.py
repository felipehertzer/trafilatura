"""
Functions needed to scrape metadata from JSON-LD format.
For reference, here is the list of all JSON-LD types: https://schema.org/docs/full.html
"""

import json
import logging
import re
from html import unescape
from re import Pattern
from typing import Any
from urllib.parse import urljoin, urlsplit

from .settings import Document
from .utils import HTML_STRIP_TAGS, as_list, trim

LOGGER = logging.getLogger(__name__)


JSON_ARTICLE_SCHEMA = {
    "article",
    "backgroundnewsarticle",
    "blogposting",
    "medicalscholarlyarticle",
    "newsarticle",
    "opinionnewsarticle",
    "reportagenewsarticle",
    "scholarlyarticle",
    "socialmediaposting",
    "liveblogposting",
}
JSON_OGTYPE_SCHEMA = {
    "aboutpage",
    "checkoutpage",
    "collectionpage",
    "contactpage",
    "faqpage",
    "itempage",
    "medicalwebpage",
    "profilepage",
    "qapage",
    "realestatelisting",
    "searchresultspage",
    "webpage",
    "website",
    "article",
    "advertisercontentarticle",
    "newsarticle",
    "analysisnewsarticle",
    "askpublicnewsarticle",
    "backgroundnewsarticle",
    "opinionnewsarticle",
    "reportagenewsarticle",
    "reviewnewsarticle",
    "report",
    "satiricalarticle",
    "scholarlyarticle",
    "medicalscholarlyarticle",
    "socialmediaposting",
    "blogposting",
    "liveblogposting",
    "discussionforumposting",
    "techarticle",
    "blog",
    "jobposting",
}
JSON_PUBLISHER_SCHEMA = {"newsmediaorganization", "organization", "webpage", "website"}
JSON_SCHEMA_TYPES = JSON_ARTICLE_SCHEMA | JSON_OGTYPE_SCHEMA | JSON_PUBLISHER_SCHEMA | {"person"}
JSON_AUTHOR_1 = re.compile(r'"author":[^}[]+?"name?\\?": ?\\?"([^"\\]+)|"author"[^}[]+?"names?".+?"([^"]+)', re.DOTALL)
JSON_AUTHOR_2 = re.compile(r'"[Pp]erson"[^}]+?"names?".+?"([^"]+)', re.DOTALL)
JSON_AUTHOR_REMOVE = re.compile(
    r',?(?:"\w+":?[:|,\[])?{?"@type":"(?:[Ii]mageObject|[Oo]rganization|[Ww]eb[Pp]age)",[^}[]+}[\]|}]?'
)
JSON_PUBLISHER = re.compile(r'"publisher":[^}]+?"name?\\?": ?\\?"([^"\\]+)', re.DOTALL)
JSON_TYPE = re.compile(r'"@type"\s*:\s*"([^"]*)"', re.DOTALL)
JSON_CATEGORY = re.compile(r'"articleSection": ?"([^"\\]+)', re.DOTALL)
JSON_SCHEMA_ORG = re.compile(r"^https?://schema\.org", flags=re.IGNORECASE)
JSON_UNICODE_REPLACE = re.compile(r"\\u([0-9a-fA-F]{4})")
JSON_LONE_SURROGATE = re.compile("[\ud800-\udfff]")

AUTHOR_ATTRS = ("givenName", "additionalName", "familyName")

JSON_NAME = re.compile(r'"@type":"[Aa]rticle", ?"name": ?"([^"\\]+)', re.DOTALL)
JSON_HEADLINE = re.compile(r'"headline": ?"([^"\\]+)', re.DOTALL)
JSON_SEQ = [('"name"', JSON_NAME), ('"headline"', JSON_HEADLINE)]

AUTHOR_PREFIX = re.compile(r"^([a-zäöüß]+(ed|t))? ?(written by|words by|words|by|von|from|por|par|door) ", flags=re.IGNORECASE)
AUTHOR_REMOVE_NUMBERS = re.compile(r"\d.+?$")
AUTHOR_TWITTER = re.compile(r"@[\w]+")
AUTHOR_REPLACE_JOIN = re.compile(r"[._+]")
AUTHOR_REMOVE_NICKNAME = re.compile(r'["“‘({\[’\'][^"”]+?[‘’"”\')\]}]')
AUTHOR_REMOVE_SPECIAL = re.compile(r"[^\w]+$|[:()?*$#!%/<>{}~¿]")
AUTHOR_REMOVE_PREPOSITION = re.compile(r"\b\s+(am|on|for|at|in|to|from|of|via|with|—|-|–)\s+(.*)", flags=re.IGNORECASE)
AUTHOR_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
AUTHOR_SPLIT = re.compile(r"/|;|,|\||&|·|•|(?:^|\W)[ua]nd(?:$|\W)", flags=re.IGNORECASE)
AUTHOR_EMOJI_REMOVE = re.compile(
    "["
    "\U00002700-\U000027be"  # Dingbats
    "\U0001f600-\U0001f64f"  # Emoticons
    "\U00002600-\U000026ff"  # Miscellaneous Symbols
    "\U0001f300-\U0001f5ff"  # Miscellaneous Symbols And Pictographs
    "\U0001f900-\U0001f9ff"  # Supplemental Symbols and Pictographs
    "\U0001fa70-\U0001faff"  # Symbols and Pictographs Extended-A
    "\U0001f680-\U0001f6ff"  # Transport and Map Symbols
    "]+",
    flags=re.UNICODE,
)


def is_plausible_sitename(metadata: Document, candidate: Any, content_type: str | None = None) -> bool:
    """Determine if the candidate should be used as sitename."""
    if candidate and isinstance(candidate, str):
        if not metadata.sitename or (len(metadata.sitename) < len(candidate) and content_type != "webpage"):
            return True
        if metadata.sitename and metadata.sitename.startswith("http") and not candidate.startswith("http"):
            return True
    return False


def json_objects(value: Any) -> list[dict[str, Any]]:
    "The JSON-LD nodes of a value: nested node lists are flattened, nulls and scalars skipped."
    objects: list[dict[str, Any]] = []
    for item in as_list(value):
        if isinstance(item, dict):
            objects.append(item)
        elif isinstance(item, list):
            objects.extend(json_objects(item))
    return objects


def schema_types(value: Any) -> list[str]:
    "Lower-cased schema.org types of a node, full IRIs (https://schema.org/NewsArticle) included."
    return [kind.rsplit("/", 1)[-1].rsplit("#", 1)[-1].lower() for kind in as_list(value) if isinstance(kind, str) and kind]


TITLE_BRANDING_SEPARATORS = frozenset("|-–—·•")


def is_branded_headline(title: str, headline: str) -> bool:
    "Whether a meta title is this headline followed by a separated suffix."
    title, headline = " ".join(unescape(title).split()), " ".join(unescape(headline).split())
    if not headline or title == headline or not title.startswith(headline):
        return False
    return title[len(headline) :].lstrip()[:1] in TITLE_BRANDING_SEPARATORS


def process_parent(parent: Any, metadata: Document) -> Document:
    "Find and extract selected metadata from JSON parts."
    for content in json_objects(parent):
        # publisher may be a bare string, not a dict
        publisher = content.get("publisher")
        if isinstance(publisher, dict) and is_plausible_sitename(metadata, publisher.get("name")):
            metadata.sitename = publisher["name"]

        content_types = schema_types(content.get("@type"))
        if not content_types:
            continue

        # Prefer the first recognized type, preserving order among recognized types.
        content_type = next(
            (schema_type for schema_type in content_types if schema_type in JSON_SCHEMA_TYPES),
            content_types[0],
        )

        # The "pagetype" should only be returned if the page is some kind of an article, category, website...
        if content_type in JSON_OGTYPE_SCHEMA and not metadata.pagetype:
            metadata.pagetype = normalize_json(content_type)

        if content_type in JSON_PUBLISHER_SCHEMA:
            candidate = content.get("name") or content.get("legalName") or content.get("alternateName")
            if is_plausible_sitename(metadata, candidate, content_type):
                metadata.sitename = candidate

        elif content_type == "person":
            if isinstance(content.get("name"), str) and not content["name"].startswith("http"):
                metadata.author = normalize_authors(metadata.author, content["name"])

        elif content_type in JSON_ARTICLE_SCHEMA:
            # author and person
            if "author" in content:
                list_authors = content["author"]
                if isinstance(list_authors, str):
                    # try to convert to json object
                    try:
                        list_authors = json.loads(list_authors)
                    except json.JSONDecodeError:
                        # it is a normal string
                        metadata.author = normalize_authors(metadata.author, list_authors)

                for author in as_list(list_authors):
                    if isinstance(author, str):
                        author = {"name": author}
                    elif not isinstance(author, dict):
                        continue
                    if "@type" not in author or "person" in schema_types(author["@type"]):
                        author_name = None
                        # error thrown: author['name'] can be a list (?)
                        if "name" in author:
                            author_name = author.get("name")
                            if isinstance(author_name, list):
                                author_name = "; ".join(author_name).strip("; ")
                            elif isinstance(author_name, dict) and "name" in author_name:
                                author_name = author_name["name"]
                        elif "givenName" in author and "familyName" in author:
                            author_name = " ".join(author[x] for x in AUTHOR_ATTRS if x in author)
                        # additional check to prevent bugs
                        if isinstance(author_name, str):
                            metadata.author = normalize_authors(metadata.author, author_name)

            # category
            if not metadata.categories and "articleSection" in content:
                sections = as_list(content["articleSection"])
                metadata.categories = [section for section in sections if isinstance(section, str) and section]

            # try to extract title; the article's headline also replaces a meta title
            # that is this headline followed by branding ("Headline | Site")
            headline = content.get("headline")
            if not metadata.title:
                if "name" in content and content_type == "article":
                    metadata.title = content["name"]
                elif "headline" in content:
                    metadata.title = headline
            elif isinstance(headline, str) and is_branded_headline(metadata.title, headline):
                metadata.title = headline
    return metadata


def _page_key(url: str) -> str:
    "Host without www. and path without trailing slash: the same page across scheme, query and fragment."
    parts = urlsplit(url)
    return f"{parts.netloc.lower().removeprefix('www.')}{parts.path.rstrip('/')}"


def _node_url(node: dict[str, Any]) -> str | None:
    "The page a JSON-LD node describes."
    for key in ("url", "mainEntityOfPage", "@id"):
        value = node.get(key)
        if isinstance(value, dict):
            value = value.get("@id") or value.get("url")
        if isinstance(value, str) and value:
            return value
    return None


def extract_json_image(nodes: list[dict[str, Any]], page_url: str | None) -> str | None:
    """The image of the page's article node (schema.org image or thumbnailUrl).

    With several article nodes, only the one describing the page counts, so that the
    image of a listed or related article is not taken for the page's.
    """
    articles = [node for node in nodes if JSON_ARTICLE_SCHEMA.intersection(schema_types(node.get("@type")))]
    if len(articles) > 1:
        if not page_url:
            return None
        page = _page_key(page_url)
        articles = [node for node in articles if (url := _node_url(node)) and _page_key(urljoin(page_url, url)) == page]
    references = {node["@id"]: node for node in nodes if isinstance(node.get("@id"), str)}
    for article in articles:
        for item in as_list(article.get("image") or article.get("thumbnailUrl")):
            if isinstance(item, dict):
                if not (item.get("url") or item.get("contentUrl")) and isinstance(item.get("@id"), str):
                    item = references.get(item["@id"], item)
                item = item.get("url") or item.get("contentUrl")
            if isinstance(item, str) and item.strip():
                return urljoin(page_url, item.strip()) if page_url else item.strip()
    return None


def extract_json(schema: list[Any] | dict[str, str], metadata: Document) -> Document:
    """Parse and extract metadata from JSON-LD data.

    Note: baseline.py's `_walk_json` also walks JSON-LD, for page content rather than
    metadata, and is intentionally not shared with this function: this one flattens one
    level, gated per container, since metadata extraction should stay conservative, while
    `_walk_json` recurses unconditionally since content rescue is a last resort.
    """
    schema = as_list(schema)

    # collect content from every valid block, then process once (no short-circuit on a flat object)
    parents: list[Any] = []
    for parent in json_objects(schema):
        context = parent.get("@context")

        if context and isinstance(context, str) and JSON_SCHEMA_ORG.match(context):
            if "@graph" in parent:
                parents.extend(as_list(parent["@graph"]))
            elif (
                "@type" in parent
                and isinstance(parent["@type"], str)
                and "liveblogposting" in parent["@type"].lower()
                and "liveBlogUpdate" in parent
            ):
                parents.extend(as_list(parent["liveBlogUpdate"]))
            else:
                parents.append(parent)

    if not metadata.image:
        metadata.image = extract_json_image(json_objects(parents), metadata.url)
    return process_parent(parents, metadata)


def extract_json_author(elemtext: str, regular_expression: Pattern[str]) -> str | None:
    """Crudely extract author names from JSON-LD data"""
    authors = None
    mymatch = regular_expression.search(elemtext)
    while mymatch:
        # first matching group (JSON_AUTHOR_1 has two)
        name = next(filter(None, mymatch.groups()), None)
        if not name or " " not in name:
            break
        authors = normalize_authors(authors, name)
        elemtext = regular_expression.sub(r"", elemtext, count=1)
        mymatch = regular_expression.search(elemtext)
    return authors or None


def extract_json_parse_error(elem: str, metadata: Document) -> Document:
    """Crudely extract metadata from JSON-LD data"""
    # author info
    element_text_author = JSON_AUTHOR_REMOVE.sub("", elem)
    author = extract_json_author(element_text_author, JSON_AUTHOR_1) or extract_json_author(element_text_author, JSON_AUTHOR_2)
    if author:
        metadata.author = author

    # try to extract page type as an alternative to og:type
    if "@type" in elem:
        mymatch = JSON_TYPE.search(elem)
        if mymatch:
            candidate = normalize_json(mymatch[1].lower())
            if candidate in JSON_OGTYPE_SCHEMA:
                metadata.pagetype = candidate

    # try to extract publisher
    if '"publisher"' in elem:
        mymatch = JSON_PUBLISHER.search(elem)
        if mymatch and "," not in mymatch[1]:
            candidate = normalize_json(mymatch[1])
            if is_plausible_sitename(metadata, candidate):
                metadata.sitename = candidate

    # category
    if '"articleSection"' in elem:
        mymatch = JSON_CATEGORY.search(elem)
        if mymatch:
            metadata.categories = [normalize_json(mymatch[1])]

    # try to extract title
    for key, regex in JSON_SEQ:
        if key in elem and not metadata.title:
            mymatch = regex.search(elem)
            if mymatch:
                metadata.title = normalize_json(mymatch[1])
                break

    return metadata


def normalize_json_strings(value: Any) -> Any:
    "Clean every string of decoded JSON as normalize_json cleans raw text: entities, markup, whitespace."
    if isinstance(value, str):
        return trim(HTML_STRIP_TAGS.sub("", unescape(JSON_LONE_SURROGATE.sub("", value))))
    if isinstance(value, list):
        return [normalize_json_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_json_strings(item) for key, item in value.items()}
    return value


def normalize_json(string: str) -> str:
    "Normalize unicode strings and trim the output"
    if "\\" in string:
        string = string.replace("\\n", "").replace("\\r", "").replace("\\t", "")
        string = JSON_UNICODE_REPLACE.sub(lambda match: chr(int(match[1], 16)), string)
        string = "".join(c for c in string if ord(c) < 0xD800 or ord(c) > 0xDFFF)
        string = unescape(string)
    return trim(HTML_STRIP_TAGS.sub("", string))


def name_key(name: str) -> str:
    "A name's letters, case-folded."
    return "".join(char for char in name.casefold() if char.isalpha())


def normalize_authors(current_authors: str | None, author_string: str) -> str | None:
    """Normalize author info to focus on author names only"""
    new_authors = []
    if author_string.lower().startswith("http") or AUTHOR_EMAIL.match(author_string):
        return current_authors
    if current_authors is not None:
        new_authors = current_authors.split("; ")
    # fix to code with unicode
    if "\\u" in author_string:
        try:
            author_string = author_string.encode().decode("unicode_escape")
        except UnicodeDecodeError:
            LOGGER.debug("invalid unicode escape in author: %s", author_string)
    # fix html entities
    if "&#" in author_string or "&amp;" in author_string:
        author_string = unescape(author_string)
    # remove html tags
    author_string = HTML_STRIP_TAGS.sub("", author_string)
    # examine names
    for author in AUTHOR_SPLIT.split(author_string):
        author = trim(author)
        # remove emoji
        author = AUTHOR_EMOJI_REMOVE.sub("", author)
        # remove @username
        author = AUTHOR_TWITTER.sub("", author)
        # replace special characters with space
        author = trim(AUTHOR_REPLACE_JOIN.sub(" ", author))
        author = trim(AUTHOR_REMOVE_NICKNAME.sub("", author))
        # remove special characters
        author = AUTHOR_REMOVE_SPECIAL.sub("", author)
        author = AUTHOR_PREFIX.sub("", author)
        author = AUTHOR_REMOVE_NUMBERS.sub("", author)
        author = AUTHOR_REMOVE_PREPOSITION.sub("", author)
        # skip empty or improbably long strings
        # simple heuristics, regex or vowel tests also possible
        if not author or (len(author) >= 50 and " " not in author and "-" not in author):
            continue
        # title case
        if not author[0].isupper():
            author = author.title()
        # one person however the name is spelled (apostrophes, spaces, case): Ben Grubb / Bengrubb
        if all(name_key(author) != name_key(name) for name in new_authors):
            new_authors.append(author)
    # keep only the fullest form of each name (drop names contained in another)
    new_authors = [n for n in new_authors if not any(n != m and n in m for m in new_authors)]
    if len(new_authors) == 0:
        return current_authors
    return "; ".join(new_authors).strip("; ")
