"""Tests for the Firecrawl response mapping."""
from __future__ import annotations

from fireclaw_search_mcp.firecrawl import (
    _map_search_response,
    _map_scrape_response,
    _map_credit_usage_response,
)


def test_empty_search_payload_returns_empty_results():
    out = _map_search_response({}, 8000)
    assert out["results"] == []
    assert out["credits_used_this_call"] == 0


def test_search_drops_entries_without_url():
    payload = {
        "success": True,
        "data": {
            "web": [
                {"title": "Has URL", "url": "https://example.com", "description": "x", "markdown": "y"},
                {"title": "No URL", "description": "noise", "markdown": "should be dropped"},
                "not-a-dict",
            ]
        },
        "creditsUsed": 5,
    }
    out = _map_search_response(payload, 8000)
    assert len(out["results"]) == 1
    assert out["results"][0]["title"] == "Has URL"
    assert out["credits_used_this_call"] == 5


def test_search_truncates_long_markdown():
    big = "x" * 12_000
    payload = {
        "data": {"web": [{"title": "Big", "url": "https://example.com", "description": "d", "markdown": big}]},
        "creditsUsed": 1,
    }
    out = _map_search_response(payload, 8000)
    r = out["results"][0]
    assert r["markdown"] != big
    assert len(r["markdown"]) <= 8000
    assert r["markdown_chars"] == 12_000
    assert r["markdown_truncated_chars"] < 12_000
    assert r["truncated"] is True
    # truncation marker is present
    assert "[…markdown truncated" in r["markdown"]


def test_search_keeps_short_markdown_verbatim():
    payload = {
        "data": {"web": [{"title": "Short", "url": "https://example.com", "description": "d", "markdown": "hello"}]},
        "creditsUsed": 1,
    }
    out = _map_search_response(payload, 8000)
    r = out["results"][0]
    assert r["markdown"] == "hello"
    assert r["markdown_chars"] == 5
    assert r["markdown_truncated_chars"] == 5
    assert r["truncated"] is False


def test_search_credits_used_falls_back_to_provider_costs():
    payload = {
        "data": {
            "web": [
                {"url": "https://a.com", "markdown": "x", "metadata": {"provider": {"creditsCost": 3}}},
                {"url": "https://b.com", "markdown": "y", "metadata": {"provider": {"creditsCost": 4}}},
            ]
        }
        # no top-level creditsUsed
    }
    out = _map_search_response(payload, 8000)
    assert out["credits_used_this_call"] == 7


def test_search_description_defaults_to_empty_string():
    payload = {
        "data": {"web": [{"title": "No description", "url": "https://example.com", "markdown": "x"}]}
    }
    out = _map_search_response(payload, 8000)
    assert out["results"][0]["description"] == ""


def test_scrape_basic():
    payload = {
        "success": True,
        "data": {
            "markdown": "Body",
            "metadata": {
                "title": "Example",
                "description": "An example page",
                "language": "en",
                "sourceURL": "https://example.com/",
                "url": "https://example.com/canonical",
                "statusCode": 200,
            },
        },
    }
    out = _map_scrape_response(payload, 8000)
    assert out["markdown"] == "Body"
    assert out["markdown_chars"] == 4
    assert out["markdown_truncated_chars"] == 4
    assert out["truncated"] is False
    assert out["title"] == "Example"
    assert out["description"] == "An example page"
    assert out["language"] == "en"
    assert out["source_url"] == "https://example.com/"
    assert out["final_url"] == "https://example.com/canonical"
    assert out["status_code"] == 200
    assert out["metadata"]["title"] == "Example"
    assert out["credits_used_this_call"] == 1


def test_scrape_truncates_long_markdown():
    big = "x" * 9_000
    payload = {
        "success": True,
        "data": {
            "markdown": big,
            "metadata": {"title": "Big", "statusCode": 200, "url": "https://example.com"},
        },
    }
    out = _map_scrape_response(payload, 8000)
    assert len(out["markdown"]) <= 8000
    assert out["markdown_chars"] == 9_000
    assert out["markdown_truncated_chars"] < 9_000
    assert out["truncated"] is True
    assert "[…markdown truncated" in out["markdown"]


def test_scrape_credits_used_honors_provider_cost():
    payload = {
        "success": True,
        "data": {
            "markdown": "Body",
            "metadata": {"provider": {"creditsCost": 5}, "statusCode": 200},
        },
        "creditsUsed": 99,
    }
    out = _map_scrape_response(payload, 8000)
    # top-level creditsUsed wins
    assert out["credits_used_this_call"] == 99


def test_scrape_metadata_title_list_flattens_to_first_string():
    payload = {
        "success": True,
        "data": {
            "markdown": "Body",
            "metadata": {"title": ["First", "Second"], "statusCode": 200},
        },
    }
    out = _map_scrape_response(payload, 8000)
    assert out["title"] == "First"


def test_scrape_missing_data_is_treated_as_empty():
    out = _map_scrape_response({}, 8000)
    assert out["markdown"] == ""
    assert out["markdown_chars"] == 0
    assert out["truncated"] is False
    assert out["credits_used_this_call"] == 1  # conservative default


def test_credit_usage_maps_along_camel_case_wire_shape():
    payload = {
        "success": True,
        "data": {
            "remainingCredits": 750,
            "planCredits": 1000,
            "billingPeriodStart": "2026-10-01T00:00:00Z",
            "billingPeriodEnd": "2026-11-01T00:00:00Z",
        },
    }
    out = _map_credit_usage_response(payload)
    assert out["remaining_credits"] == 750
    assert out["plan_credits"] == 1000
    assert out["billing_period_end"] == "2026-11-01T00:00:00Z"


def test_credit_usage_missing_data_returns_nones():
    out = _map_credit_usage_response({})
    assert out["remaining_credits"] is None
    assert out["plan_credits"] is None
