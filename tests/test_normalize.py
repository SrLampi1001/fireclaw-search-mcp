"""Pure-function tests for query / URL normalisation, slug, and FNV-1a 64-bit hash."""
from __future__ import annotations

from fireclaw_search_mcp.normalize import (
    cache_path_for,
    fnv1a64,
    normalize_query,
    normalize_url,
    slugify,
)


def test_normalize_trims_and_lowercases():
    assert normalize_query("  Rust   Programming  Language  ") == "rust programming language"


def test_normalize_collapses_inner_whitespace():
    assert normalize_query("a\t\t b\n\nc") == "a b c"


def test_normalize_strips_edges():
    assert normalize_query("   hello   ") == "hello"


def test_normalize_url_lowercases_scheme_and_host():
    assert normalize_url("HTTPS://Example.COM/path?q=1") == "https://example.com/path?q=1"


def test_normalize_url_strips_fragment():
    # Fragments are client-side and shouldn't be part of the cache key.
    assert normalize_url("https://example.com/page#section") == "https://example.com/page"


def test_slugify_handles_specials():
    assert slugify("c++") == "c"
    assert slugify("c#") == "c"
    # the hash disambiguates them
    assert fnv1a64("c++") != fnv1a64("c#")


def test_slugify_empty_falls_back():
    assert slugify("") == "empty"
    assert slugify("---") == "empty"


def test_slugify_truncates():
    long = "a" * 200
    assert len(slugify(long)) == 80


def test_fnv1a64_deterministic():
    a = fnv1a64("hello world")
    b = fnv1a64("hello world")
    assert a == b
    assert isinstance(a, int)
    assert 0 <= a < 2**64


def test_fnv1a64_changes_with_input():
    assert fnv1a64("a") != fnv1a64("b")


def test_cache_path_for_query(tmp_path):
    p = cache_path_for("  Rust Language  ", tmp_path)
    assert p.parent == tmp_path
    assert p.name.startswith("rust-language-")
    assert p.name.endswith(".json")
    assert len(p.stem.split("-")[-1]) == 16  # 16 hex chars = 64-bit hash


def test_cache_path_for_url(tmp_path):
    p = cache_path_for("https://Example.com/path?q=1", tmp_path)
    assert p.parent == tmp_path
    assert p.name.startswith("https-example-com-")
    assert p.name.endswith(".json")
