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
from urllib.parse import urljoin

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
JSON_PUBLISHER_SCHEMA = {
    "educationalorganization",
    "governmentorganization",
    "newsmediaorganization",
    "ngo",
    "organization",
    "researchorganization",
    "webpage",
    "website",
}
JSON_SCHEMA_TYPES = JSON_ARTICLE_SCHEMA | JSON_OGTYPE_SCHEMA | JSON_PUBLISHER_SCHEMA | {"person"}
JSON_AUTHOR_1 = re.compile(r'"author":[^}[]+?"name?\\?": ?\\?"([^"\\]+)|"author"[^}[]+?"names?".+?"([^"]+)', re.DOTALL)
JSON_AUTHOR_2 = re.compile(r'"[Pp]erson"[^}]+?"names?".+?"([^"]+)', re.DOTALL)
JSON_AUTHOR_REMOVE = re.compile(
    r',?(?:"\w+":?[:|,\[])?{?"@type":"(?:[Ii]mageObject|[Oo]rganization|[Ww]eb[Pp]age)",[^}[]+}[\]|}]?'
)
JSON_PUBLISHER = re.compile(r'"publisher":[^}]+?"name?\\?": ?\\?"([^"\\]+)', re.DOTALL)
JSON_TYPE = re.compile(r'"@type"\s*:\s*"([^"]*)"', re.DOTALL)
JSON_CATEGORY = re.compile(r'"articleSection": ?"([^"\\]+)', re.DOTALL)
JSON_SCHEMA_ORG = re.compile(r"^https?://(?:www\.)?schema\.org", flags=re.IGNORECASE)
JSON_UNICODE_REPLACE = re.compile(r"\\u([0-9a-fA-F]{4})")

AUTHOR_ATTRS = ("givenName", "additionalName", "familyName")

JSON_NAME = re.compile(r'"@type":"[Aa]rticle", ?"name": ?"([^"\\]+)', re.DOTALL)
JSON_HEADLINE = re.compile(r'"headline": ?"([^"\\]+)', re.DOTALL)
JSON_SEQ = [('"name"', JSON_NAME), ('"headline"', JSON_HEADLINE)]

AUTHOR_PREFIX = re.compile(
    r"^([a-zäöüß]+(ed|t)|story|report|reporting|text|article|analysis)? ?(written by|words by|words|by|von|from|por|par|door) ",
    flags=re.IGNORECASE,
)
AUTHOR_VISUAL_CREDIT = re.compile(
    r"(?:\s*[|,;/]|\s+(?:and|with))?\s*\b(?:photography|photos?|pictures?|illustrations?|graphics?|video|visuals?)"
    r"\s+by\b.*$",
    flags=re.IGNORECASE,
)
AUTHOR_WORD_START = re.compile(r"(\w)(\w*)")
AUTHOR_REMOVE_NUMBERS = re.compile(r"\d.+?$")
# a decimal number with words after it, a radio station's frequency ("92.9 Voice FM"); a date has two
# dots ("18.10.2019") and a time no space before its letters ("2.30pm")
AUTHOR_FREQUENCY = re.compile(r"(?<![\d.])\d+\.\d+(?![.\d])\s+[^\W\d_]")
AUTHOR_TWITTER = re.compile(r"@[\w]+")
# a dot joins a handle's parts ("john.smith"), a decimal point stays ("Life 105.1")
AUTHOR_REPLACE_JOIN = re.compile(r"[_+]|(?<!\d)\.|\.(?!\d)")
# a nickname in quotes or brackets; an apostrophe inside a word ("CNN's Dana O'Neil") is no quote
AUTHOR_REMOVE_NICKNAME = re.compile(r'["“({\[][^"”]+?[‘’"”\')\]}]|(?<![^\W\d_])[‘’\'][^"”]+?[‘’"”\')\]}](?![^\W\d_])')
AUTHOR_REMOVE_SPECIAL = re.compile(r"[^\w]+$|[:()?*$#!%/<>{}~¿]")
AUTHOR_REMOVE_PREPOSITION = re.compile(r"\b\s+(am|on|for|at|in|to|from|of|via|with|—|-|–)\s+(.*)", flags=re.IGNORECASE)
# A date removed from a byline leaves its lead-in behind ("Jenny Smith on",
# "Isabella DiBiase am"). Lower case only: a name capitalizes its particles.
AUTHOR_TRAILING_PREPOSITION = re.compile(r"\s+(?:am|on|for|at|in|to|from|of|via|with|um|le|el|il)$")
AUTHOR_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
AUTHOR_SPLIT = re.compile(r"/|;|,|\||&|·|•|(?:^|\W)[ua]nd(?:$|\W)", flags=re.IGNORECASE)
# "with" joins two people too ("Reged Ahmad with Richard Luscombe"), but only before a person: what
# else follows it is no author ("In paid partnership with Vypr", "Jane Doe with Reuters") and goes
# with it, as after any preposition. At the start of a part it is a mere connector.
AUTHOR_WITH = re.compile(r"(?:(^|[;|/,&•·])\s*|\s+)with\s+", flags=re.IGNORECASE)
# "and" in Spanish, Portuguese, German and French wire credits ("Clara Preve e
# Isabel Debre"); only between two full names, since "e" also joins Portuguese
# surnames ("João e Silva").
AUTHOR_FOREIGN_AND = re.compile(r"(?<=[^\W\d_]) (?:y|e|und|et) (?=[A-ZÀ-Þ])")
AUTHOR_FULL_NAME = re.compile(r"[A-ZÀ-Þ][^\W\d_]*(?:\s+[A-ZÀ-Þ][^\W\d_]*)+")
# A byline is often a credit line rather than a list: "Presented by Reged
# Ahmad with Richard Luscombe. Produced by Taylah Strano with sound design and
# mixing from Jacob Round. The executive producer is Hannah Parkes". Its
# sentence breaks and credit phrases separate people.
AUTHOR_SENTENCE_BREAK = re.compile(r"(?<=[a-z]{2})\.\s+(?=[A-Z])")
AUTHOR_CREDIT_ROLE = (
    r"(?:presented|produced|written|read|edited|hosted|narrated|reported|compiled|researched|filmed|translated|"
    r"curated|interviews?|text|words|reporting|additional\s+reporting|sound\s+design|mixing|music|exclusive|"
    r"analysis|commentary)"
)
AUTHOR_CREDIT = re.compile(
    rf"\b(?:(?:photographs?|photos?|pictures?)\s+and\s+)?{AUTHOR_CREDIT_ROLE}(?:\s+and\s+{AUTHOR_CREDIT_ROLE})*"
    r"\s+(?:by|from)\b"
    r"|\b(?:the\s+)?(?:executive\s+|senior\s+|series\s+|supervising\s+|associate\s+)?"
    r"(?:producers?|editors?|presenters?|hosts?)\s+(?:is|was|are|were)\b"
    r"|\b(?:the\s+)?executive\s+producers?\b"
    # an author page link: "View all posts by Daniel Lemire"
    r"|\b(?:view|see|read)\s+(?:all|more)\s+(?:posts|articles|stories)\s+by\b",
    flags=re.IGNORECASE,
)
# "Steven Band, as told to Deborah Solomon": the writer is the one told.
AUTHOR_AS_TOLD_TO = re.compile(r"[^;|/]*\bas\s+told\s+to\b", flags=re.IGNORECASE)
# A dateline runs to the next person, commas included: "Adam Morton in Nadi,
# Fiji", "Jason Burke in London and Stephanie Kirchgaessner in Washington".
AUTHOR_DATELINE = re.compile(r"\s+(?:in|at|reporting\s+from)\s+[A-ZÀ-Þ][^;|/&]*?(?=\s+(?:and|with)\s+|\s*[;|/&]|$)")
AUTHOR_ROLE_WORDS = frozenset(
    {
        "editor", "correspondent", "reporter", "writer", "journalist", "producer", "columnist", "presenter",
        "host", "contributor", "critic", "photographer", "spokesperson", "spokesman", "spokeswoman",
        "commentator", "analyst", "anchor", "director", "manager", "professor", "fellow", "lecturer",
        "researcher", "historian", "founder", "officer", "economist", "scientist", "president", "chair",
        "adviser", "advisor", "consultant", "coordinator", "teacher", "ceo", "cmo", "cto", "cfo", "coo",
    }
)  # fmt: skip
# After a comma, a role describes the person before it: "Tracy Neal, Open
# Justice multimedia journalist", "Laurence Kelson, Census spokesperson". One
# person holds one role, so a plural is a surname ("Jane Doe, Taylah Fellows").
AUTHOR_DESCRIPTION = re.compile(rf"\b(?:{'|'.join(sorted(AUTHOR_ROLE_WORDS))})\b", flags=re.IGNORECASE)
# Post-nominal letters after a comma: "Chelsea Rae Bourgeois, RDN, LD", "Kate Dore, CFP®, EA".
AUTHOR_CREDENTIALS = frozenset(
    {
        "rdn", "rd", "ld", "ea", "cfp", "cpa", "cfa", "phd", "md", "rn", "oam", "am", "ao", "ac", "mba", "msc",
        "bsc", "jd", "esq", "mph", "dvm", "gaicd", "faicd", "fca",
    }
)  # fmt: skip
# A second person after a role: "Chief political correspondent and Paul Karp".
AUTHOR_JOINED_NAME = re.compile(r"\s+(?:and|with)\s+(?=[A-ZÀ-Þ][^\W\d_]*\s+[A-ZÀ-Þ])")
# A person's name ahead: two capitalised words ("Paul Karp", "Marie-Morgane LE MOEL", "Dana O'Neil").
AUTHOR_PERSON_AHEAD = re.compile(r"\s*([A-ZÀ-Þ](?:[^\W\d_]|['’-])*)\s+([A-ZÀ-Þ](?:[^\W\d_]|['’-])*)")
AUTHOR_CREDENTIAL_LIKE = re.compile(r"[A-Za-z]{2,5}\.?[®™]?")
# A programme part after a comma: "Andrew Whitelaw, Episode 3".
AUTHOR_EPISODE = re.compile(r"(?:episode|ep|part|series|season|chapter|vol(?:ume)?)\.?\s*\d+", flags=re.IGNORECASE)
# "Santosh Mehrotra <br> Higher School of Economics, Moscow"
AUTHOR_LINE_BREAK = re.compile(r"\s*<br\s*/?>\s*", flags=re.IGNORECASE)
# A byline that is a website, its top-level domain in lower case ("Australia.so", "mirror.co.uk"); a name
# with initials or a title ("J.R.R. Tolkien", "Dr.Smith") has a space or a capital after the dot.
AUTHOR_DOMAIN_BYLINE = re.compile(
    r"(?:www\.)?[\w-]+(?:\.[\w-]+)*\.(?:[a-z]{2}|com|net|org|info|news|media|online|site|biz|app|blog)"
)
# A website after a comma: "Robert Jennings, InnerSelf.com".
AUTHOR_DOMAIN = re.compile(r"[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|edu|gov|io|co|au|nz|uk)\b", flags=re.IGNORECASE)
# The words of a job title before its role: "Political editor", "Senior
# political correspondent", "Environment and climate correspondent".
AUTHOR_TITLE_WORDS = frozenset(
    {
        "and", "political", "senior", "chief", "deputy", "environment", "consumer", "technology", "diplomatic",
        "foreign", "business", "sports", "sport", "health", "science", "economics", "economic", "education",
        "arts", "culture", "music", "film", "food", "travel", "fashion", "property", "legal", "crime",
        "defence", "defense", "transport", "energy", "investigations", "investigative", "data", "digital",
        "national", "state", "regional", "rural", "world", "global", "europe", "asia", "pacific", "media",
        "social", "entertainment", "lifestyle", "finance", "markets", "managing", "executive", "associate",
        "assistant", "contributing", "special", "staff", "local", "community", "multimedia", "news",
        "features", "opinion", "politics", "climate", "medical",
    }
)  # fmt: skip
AUTHOR_SEGMENT = re.compile(r"[^;|/,&]+")
# Legal forms: a byline ending in one names a company, and the comma before one
# ("Healthy Humor, Inc.") does not separate two names.
AUTHOR_LEGAL_FORMS = frozenset(
    {"inc", "llc", "llp", "ltd", "pty", "plc", "gmbh", "corp", "corporation", "limited", "incorporated", "bhd", "ulc", "pte"}
)
AUTHOR_LEGAL_COMMA = re.compile(
    rf",\s*(?=(?:{'|'.join(sorted(AUTHOR_LEGAL_FORMS))})\b\.?\s*(?:[;|/&,]|$))", flags=re.IGNORECASE
)
AUTHOR_SPEECH_VERB = re.compile(r"\s+(?:say|says|said)$", flags=re.IGNORECASE)
# Two of these in one byline make it a sentence, not a name.
AUTHOR_SENTENCE_WORDS = frozenset(
    {
        "the", "is", "are", "was", "were", "be", "been", "will", "would", "can", "could", "has", "have", "had",
        "that", "this", "it", "its", "their", "they", "we", "you", "how", "why", "what", "when", "who", "not",
        "after", "before", "while", "into", "about", "than", "more", "most", "just", "all", "here", "there",
        "my", "your", "our", "his", "her", "him", "them", "she", "he", "do", "does", "did", "to", "for", "on",
        "out", "up", "over", "if", "or", "but", "so",
    }
)  # fmt: skip
# Some of them are personal names too ("Will Smith", "Anh Do", "Do My Linh", "He Jiankui"): in a byline
# written as a name they are no sign of a sentence. Three of them are ("So You Think You Can Dance").
AUTHOR_NAME_HOMOGRAPHS = frozenset({"will", "do", "my", "can", "he", "her", "you", "she", "so", "than"})
NAME_PARTICLES = frozenset(
    {"al", "bin", "binti", "da", "das", "de", "del", "della", "den", "der", "di", "do", "dos", "du", "la", "le", "ter",
     "van", "von"}
)  # fmt: skip
AUTHOR_WORD = re.compile(r"[^\W\d_]+")
# A column label in capitals that the publisher glued to the name ("CITY BEATChris
# Herde", "CNNNeil Connell"): the label goes when two name words follow it. A brand
# written the same way ("SUPERCentral", "CXFocus Editor", "CQUniversity Australia")
# leaves one word, or words that do not name a person.
AUTHOR_GLUED_LABEL = re.compile(
    r"(?:[A-Z]{2,}\s+)*[A-Z]{2,}(?=([A-Z](?:[a-z]|['’][A-Z])[\w'’-]*) ([A-Z](?:[a-z]|['’][A-Z])[\w'’-]*)$)"
)
AUTHOR_NOT_NAME_WORDS = frozenset(
    {
        "news", "desk", "team", "staff", "group", "media", "network", "solutions", "services", "systems",
        "labs", "university", "college", "school", "institute", "australia", "international", "online",
        "magazine", "radio", "sports", "football", "motoring",
    }
)  # fmt: skip
# what no word of a person's name is
AUTHOR_NOT_PERSON_WORDS = AUTHOR_NOT_NAME_WORDS | AUTHOR_ROLE_WORDS | AUTHOR_TITLE_WORDS | AUTHOR_LEGAL_FORMS
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


def _json_objects(parent: Any) -> list[dict[str, Any]]:
    "JSON-LD objects from a node list, flattening nested lists and skipping scalars or nulls."
    objects: list[dict[str, Any]] = []
    for item in as_list(parent):
        if isinstance(item, dict):
            objects.append(item)
        elif isinstance(item, list):
            objects.extend(_json_objects(item))
    return objects


def process_parent(parent: Any, metadata: Document, *, include_organization_authors: bool = True) -> Document:
    "Find and extract selected metadata from JSON parts."
    for content in _json_objects(parent):
        # publisher may be a bare string, not a dict
        publisher = content.get("publisher")
        if isinstance(publisher, dict) and is_plausible_sitename(metadata, publisher.get("name")):
            metadata.sitename = publisher["name"]

        if "@type" not in content or not content["@type"]:
            continue

        # Prefer the first recognized type, preserving order among recognized types.
        content_types = as_list(content["@type"])
        content_type = next(
            (
                schema_type
                for schema_type in content_types
                if isinstance(schema_type, str) and schema_type.lower() in JSON_SCHEMA_TYPES
            ),
            content_types[0],
        ).lower()

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
                    if not isinstance(author, dict):
                        continue
                    author_types = {
                        value.rsplit("/", 1)[-1].casefold()
                        for value in as_list(author.get("@type", []))
                        if isinstance(value, str)
                    }
                    allowed_types = {"person", "organization"} if include_organization_authors else {"person"}
                    if "@type" not in author or author_types & allowed_types:
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
                metadata.categories = [s for s in sections if isinstance(s, str) and s]

            # try to extract title; the article headline also replaces a meta
            # title that is that headline followed by site or section branding
            if "name" in content and content_type == "article":
                headline = content["name"]
            else:
                headline = content.get("headline")
            if (
                isinstance(headline, str)
                and headline.strip()
                and (not metadata.title or is_branded_headline(metadata.title, headline))
            ):
                metadata.title = headline
    return metadata


def is_schema_org_context(context: Any) -> bool:
    """Tell if a JSON-LD @context refers to schema.org: a string, an object
    with a schema.org @vocab, or a list containing one of these."""
    if isinstance(context, str):
        return bool(JSON_SCHEMA_ORG.match(context))
    if isinstance(context, dict):
        return is_schema_org_context(context.get("@vocab"))
    if isinstance(context, list):
        return any(is_schema_org_context(item) for item in context)
    return False


# a colon opens a subtitle ("Coinbase vs. Coinbase Pro: How do they compare?"), not the site's branding
TITLE_SUFFIX_SEPARATORS = frozenset("|-–—·•")


def is_branded_headline(title: str, headline: str) -> bool:
    """True when a meta title is the article headline plus a separated suffix."""
    title = " ".join(unescape(title).split())
    headline = " ".join(unescape(headline).split())
    if not headline or title == headline or not title.startswith(headline):
        return False
    return title[len(headline) :].lstrip()[:1] in TITLE_SUFFIX_SEPARATORS


def extract_json_image(value: Any, references: dict[str, Any]) -> str | None:
    """Read an article image without inferring one from publisher logos."""
    for item in as_list(value):
        if isinstance(item, dict):
            reference = item.get("@id")
            if not item.get("url") and not item.get("contentUrl") and isinstance(reference, str):
                item = references.get(reference, item)
            item = item.get("url") or item.get("contentUrl")
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def extract_json(
    schema: list[Any] | dict[str, str], metadata: Document, *, include_organization_authors: bool = True
) -> Document:
    """Parse and extract metadata from JSON-LD data.

    Note: baseline.py's `_walk_json` also walks JSON-LD, for page content rather than
    metadata, and is intentionally not shared with this function: this one flattens one
    level, gated per container, since metadata extraction should stay conservative, while
    `_walk_json` recurses unconditionally since content rescue is a last resort.
    """
    schema = as_list(schema)

    # collect content from every valid block, then process once (no short-circuit on a flat object)
    parents: list[Any] = []
    for parent in schema:
        context = parent.get("@context")

        if is_schema_org_context(context):
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
        references = {node["@id"]: node for node in parents if isinstance(node, dict) and isinstance(node.get("@id"), str)}
        for node in parents:
            if not isinstance(node, dict):
                continue
            kinds = {kind.rsplit("/", 1)[-1].casefold() for kind in as_list(node.get("@type", [])) if isinstance(kind, str)}
            if kinds & JSON_ARTICLE_SCHEMA:
                identity = node.get("url") or node.get("mainEntityOfPage") or node.get("@id")
                if isinstance(identity, dict):
                    identity = identity.get("@id") or identity.get("url")
                if metadata.url and isinstance(identity, str):
                    article_url = urljoin(metadata.url, identity).split("#", 1)[0].rstrip("/")
                    if article_url != metadata.url.split("#", 1)[0].rstrip("/"):
                        continue
                metadata.image = extract_json_image(node.get("image"), references)
                if metadata.image:
                    break
    return process_parent(parents, metadata, include_organization_authors=include_organization_authors)


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


def unescape_json_string(string: str) -> str:
    "Remove lone surrogates and unescape HTML entities"
    string = "".join(c for c in string if ord(c) < 0xD800 or ord(c) > 0xDFFF)
    return unescape(string)


def normalize_json(string: str) -> str:
    "Normalize unicode strings and trim the output"
    if "\\" in string:
        string = string.replace("\\n", "").replace("\\r", "").replace("\\t", "")
        string = JSON_UNICODE_REPLACE.sub(lambda match: chr(int(match[1], 16)), string)
        string = unescape_json_string(string)
    return trim(HTML_STRIP_TAGS.sub("", string))


def normalize_json_values(value: Any) -> Any:
    "Normalize all strings of a parsed JSON object."
    if isinstance(value, str):
        # JSON escapes are already decoded by the parser, entities and surrogates are not
        return trim(HTML_STRIP_TAGS.sub("", unescape_json_string(value)))
    if isinstance(value, list):
        return [normalize_json_values(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_json_values(item) for key, item in value.items()}
    return value


def _strip_job_title(segment: str) -> str:
    """Drop a job title after a name: "Tom McIlroy Political editor" -> "Tom McIlroy".

    From the third word on, the title is a role and the words before it that
    are lower case, "and" or a title word. Only the title goes: "Sarah Martin
    Chief political correspondent and Paul Karp" keeps Paul Karp.
    """
    tokens = segment.split()
    # the name starts after the credit words ("By Staff Writer" is a role, not a name; in "words: Jennie
    # Officer" the surname is no role): the ones AUTHOR_PREFIX removes later, colon aside
    prefix = AUTHOR_PREFIX.match(" ".join(tokens).replace(":", "") + " ")
    start = len(prefix.group().split()) if prefix else 0
    role = next((index for index, token in enumerate(tokens) if token.casefold() in AUTHOR_ROLE_WORDS), None)
    if role is None or role < start + 2:
        return segment
    # a name takes two words; after an outlet's possessive the title starts at once ("CNN's Chief Media
    # Analyst Brian Stelter")
    name_end = start + (1 if tokens[start].endswith(("'s", "’s")) else 2)
    end = role
    while end > name_end and (tokens[end - 1].islower() or tokens[end - 1].casefold() in AUTHOR_TITLE_WORDS):
        end -= 1
    return " " + " ".join(tokens[:end] + tokens[role + 1 :]) + " "


def _join_foreign_and(author_string: str) -> str:
    """Separate two full names joined by a foreign "and"; keep a compound surname."""
    parts = AUTHOR_FOREIGN_AND.split(author_string)
    joined = parts[0]
    for connector, part in zip(AUTHOR_FOREIGN_AND.findall(author_string), parts[1:], strict=True):
        left = re.split(r"[;|/,&]", joined)[-1].strip()
        right = re.split(r"[;|/,&]", part)[0].strip()
        full = AUTHOR_FULL_NAME.fullmatch(left) and AUTHOR_FULL_NAME.fullmatch(right)
        joined += ("; " if full else connector) + part
    return joined


def _is_person_detail(segment: str) -> bool:
    """Whether a comma segment after a name describes that person rather than naming another."""
    text = segment.strip()
    if text.strip(".®™ ").casefold() in AUTHOR_CREDENTIALS:
        return True
    if AUTHOR_EPISODE.fullmatch(text) or AUTHOR_DOMAIN.fullmatch(text):
        return True
    words = {word.casefold() for word in AUTHOR_WORD.findall(text)}
    return bool(words & (AUTHOR_NOT_NAME_WORDS | AUTHOR_LEGAL_FORMS))


def _is_person_ahead(text: str) -> bool:
    """Whether a text opens with a person's name: two capitalised words, none of them a role, a word
    of a job title or of an organisation ("Paul Karp", but not "Beauty Editor" or "Fox Sports")."""
    match = AUTHOR_PERSON_AHEAD.match(text)
    if match is None:
        return False
    words = {word.casefold() for name in match.groups() for word in AUTHOR_WORD.findall(name)}
    return not words & AUTHOR_NOT_PERSON_WORDS


def _drop_person_details(author_string: str) -> str:
    """Drop what follows a name after a comma when it describes the person.

    "Lydia Edwards, Fashion historian, Edith Cowan University" is one person: a
    role ends the name, and the rest of that credit is the role's organisation
    ("Sean Crawford, Managing Director, North America, SMG"). Credentials ("RDN"),
    an organisation ("McGill University"), a website ("InnerSelf.com") or an
    episode ("Episode 3") go on their own. A list of names stays a list, and a
    name joined with "and" starts a new credit.
    """
    groups = re.split(r"([;|/&])", author_string)
    for index in range(0, len(groups), 2):
        segments = groups[index].split(",")
        kept, in_role = segments[:1], False
        for segment in segments[1:]:
            if re.match(r"\s*(?:and|with)\s", segment, flags=re.IGNORECASE):
                in_role = False
            elif in_role:
                continue
            elif AUTHOR_DESCRIPTION.search(segment):
                in_role = True
                # "Sarah Martin, Chief political correspondent and Paul Karp" credits Paul Karp too
                # "Lesley Thomas, Weekend and Beauty Editor" credits no one else: the title goes on
                joined = AUTHOR_JOINED_NAME.split(segment, maxsplit=1)
                if len(joined) == 1 or not _is_person_ahead(joined[1]):
                    continue
                segment, in_role = " and " + joined[1], False
            elif _is_person_detail(segment):
                # an organisation ends the credit too: "Higher School of Economics, Moscow"
                in_role = not AUTHOR_CREDENTIAL_LIKE.fullmatch(segment.strip())
                continue
            kept.append(segment)
        groups[index] = ",".join(kept)
    return "".join(groups)


def _separate_credits(author_string: str) -> str:
    """Turn a credit line into a list of people the split below can read."""
    author_string = _join_foreign_and(author_string)
    author_string = AUTHOR_SENTENCE_BREAK.sub("; ", author_string)
    author_string = AUTHOR_CREDIT.sub("; ", author_string)
    author_string = AUTHOR_AS_TOLD_TO.sub("", author_string)
    author_string = AUTHOR_DATELINE.sub("", author_string)
    author_string = AUTHOR_LEGAL_COMMA.sub(" ", author_string)
    author_string = _drop_person_details(author_string)
    return AUTHOR_SEGMENT.sub(lambda match: _strip_job_title(match.group()), author_string)


def _separate_with(author_string: str) -> str:
    """Separate the person a "with" joins to the one before: "Michael Mathes with Brendan Smialowski"."""

    def separate(match: re.Match[str]) -> str:
        if match[1] is not None:
            return match[1] + " "
        return "; " if _is_person_ahead(author_string[match.end() :]) else match.group()

    return AUTHOR_WITH.sub(separate, author_string)


def _is_name(author: str) -> bool:
    """Whether a cleaned byline part can be a name: not a company, not a sentence."""
    tokens = AUTHOR_WORD.findall(author)
    words = [token.casefold() for token in tokens]
    if not words or words[-1] in AUTHOR_LEGAL_FORMS:
        return False
    sentence = sum(word in AUTHOR_SENTENCE_WORDS for word in words)
    # written as a name: every word capitalised, a particle ("van der") or a possessive ("CNN's")
    if all(token[0].isupper() or word in NAME_PARTICLES or word == "s" for token, word in zip(tokens, words, strict=True)):
        names = sum(word in AUTHOR_NAME_HOMOGRAPHS for word in words)
        if names >= 3:
            return False
        sentence -= names
    return sentence < 2 and not (sentence and len(words) >= 4)


def _drop_glued_label(author: str) -> str:
    """Drop a capitals label glued to a two-word name: "CITY BEATChris Herde" -> "Chris Herde"."""
    match = AUTHOR_GLUED_LABEL.match(author)
    if match is None:
        return author
    words = {match[1].casefold(), match[2].casefold()}
    if words & AUTHOR_NOT_PERSON_WORDS:
        return author
    return author[match.end() :]


def _name_key(author: str) -> str:
    """The letters of a name: "Ben Grubb" and "Bengrubb" are one name."""
    return "".join(AUTHOR_WORD.findall(author.translate(str.maketrans("‘’", "''")))).casefold()


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
    # remove html tags; a line break separates the parts of a credit
    author_string = HTML_STRIP_TAGS.sub("", AUTHOR_LINE_BREAK.sub(", ", author_string))
    # a byline may also credit visual contributors ("| Photography by X for WSJ")
    author_string = AUTHOR_VISUAL_CREDIT.sub("", author_string)
    author_string = _separate_with(_separate_credits(author_string))
    # examine names
    for author in AUTHOR_SPLIT.split(author_string):
        author = trim(author)
        # remove emoji
        author = AUTHOR_EMOJI_REMOVE.sub("", author)
        # remove @username
        author = AUTHOR_TWITTER.sub("", author)
        # a website is no person: "Australia.so", "mirror.co.uk", "cricket.com.au"
        if AUTHOR_DOMAIN_BYLINE.fullmatch(author):
            continue
        # replace special characters with space
        author = trim(AUTHOR_REPLACE_JOIN.sub(" ", author))
        author = trim(AUTHOR_REMOVE_NICKNAME.sub("", author))
        # remove special characters
        author = AUTHOR_REMOVE_SPECIAL.sub("", author)
        author = AUTHOR_PREFIX.sub("", author)
        author = _drop_glued_label(author)
        # a number ends the name ("Ross Guest 1 October 2025"), but a frequency followed by words
        # belongs to a station's name ("Life 105.1 Contributors" gave "Life")
        if not AUTHOR_FREQUENCY.search(author):
            author = AUTHOR_REMOVE_NUMBERS.sub("", author)
        author = AUTHOR_REMOVE_PREPOSITION.sub("", author)
        author = AUTHOR_TRAILING_PREPOSITION.sub("", author)
        # a reported-speech verb scraped with the name ("Charlotte Trueman say")
        author = trim(AUTHOR_SPEECH_VERB.sub("", author))
        # skip empty or improbably long strings, companies and sentences
        # simple heuristics, regex or vowel tests also possible
        if not author or (len(author) >= 50 and " " not in author and "-" not in author) or not _is_name(author):
            continue
        # title case; an all-capitals byline ("VANESSA PAIGE CHELVAN") is a style. The first letter
        # decides: a station's frequency leads "92.9 Voice FM Contributors"
        if not AUTHOR_WORD.findall(author)[0][0].isupper():
            author = author.title()
        elif " " in author and author.isupper():
            author = AUTHOR_WORD_START.sub(lambda m: m[1] + m[2].lower(), author)
        # Publishers may mix typographic and ASCII apostrophes, spaces and
        # hyphens across metadata fields ("Ben Grubb", "Bengrubb"). Keep the
        # first display spelling without duplicating the byline.
        author_key = _name_key(author)
        if not any(_name_key(name) == author_key for name in new_authors):
            new_authors.append(author)
    # keep only the fullest form of each name (drop names contained in another)
    new_authors = [n for n in new_authors if not any(n != m and n in m for m in new_authors)]
    if len(new_authors) == 0:
        return current_authors
    return "; ".join(new_authors).strip("; ")
