"""Input normalisation, slug, and FNV-1a 64-bit hash for cache keying.

Per SPECS §6: search-query cache keys are the trimmed, inner-whitespace-
collapsed, lowercased query. Scrape cache keys are the URL itself (no
normalisation needed — Firecrawl canonicalises via redirects and we record
the final URL post-fetch). File names use `<slug>-<fnv1a64>.json` so a
human can browse the cache directory; the hash disambiguates inputs that
slugify identically.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_INNER_WS = re.compile(r"\s+")
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_FNV_MASK = 0xFFFFFFFFFFFFFFFF


def normalize_query(q: str) -> str:
    if not isinstance(q, str):
        q = str(q)
    return _INNER_WS.sub(" ", q).strip().lower()


def normalize_url(url: str) -> str:
    """Canonicalise a URL for cache-key purposes.

    Scheme and host are lowercased; the path/query/fragment are kept verbatim.
    Firecrawl normalises follow-redirects internally and exposes the final URL
    on `metadata.url`; we key on the *input* URL so a request for the same
    canonical address re-hits the cache, and we surface the final URL in the
    response so the model can see post-redirect destinations.
    """
    if not isinstance(url, str):
        url = str(url)
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return url.strip()
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def slugify(normalized: str, max_len: int = 80) -> str:
    slug = _NON_SLUG.sub("-", normalized).strip("-")
    if not slug:
        slug = "empty"
    return slug[:max_len]


def fnv1a64(s: str) -> int:
    h = _FNV_OFFSET
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * _FNV_PRIME) & _FNV_MASK
    return h


def cache_path_for(input_key: str, base_dir: Path) -> Path:
    """Map an arbitrary cache input to a stable on-disk path."""
    if input_key.startswith(("http://", "https://")):
        normalized = normalize_url(input_key)
        slug_source = normalized
    else:
        normalized = normalize_query(input_key)
        slug_source = normalized
    return base_dir / f"{slugify(slug_source)}-{fnv1a64(normalized):016x}.json"
