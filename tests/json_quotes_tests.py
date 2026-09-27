"""Regression cases for JSON quote corruption and publication time precision."""

import json

import pytest

from trafilatura.json_metadata import normalize_authors
from trafilatura.metadata import extract_metadata

URL = "https://example.org/story"


def script(value):
    return '<script type="application/ld+json">' + json.dumps(value) + "</script>"


def metadata(content, **config):
    return extract_metadata(
        "<html><head>" + content + "</head><body/></html>",
        default_url=URL,
        date_config={"max_date": "2026-09-27", **config},
    )


@pytest.mark.parametrize("description", ['"Quoted" news', "&quot;Quoted&quot; news", "News \\u0022 and café"])
def test_valid_json_author_survives_description_quotes(description):
    content = script(
        {
            "@context": "https://schema.org",
            "@graph": [
                {
                    "@type": "NewsArticle",
                    "description": description,
                    "author": {"@type": "Person", "name": "CNA"},
                }
            ],
        }
    )
    assert metadata(content).author == "CNA"


def test_duplicate_apostrophe_spellings_keep_first_display_name():
    assert normalize_authors(None, "James O’Doherty; James O'Doherty") == "James O’Doherty"
    assert normalize_authors("James O'Doherty", "James O’Doherty") == "James O'Doherty"
    assert normalize_authors(None, "James O’Doherty; Jane O’Doherty") == "James O’Doherty; Jane O’Doherty"
