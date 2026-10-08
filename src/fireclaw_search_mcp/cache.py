"""On-disk TTL cache for Firecrawl responses (search + scrape).

Per SPECS §6: a missing, stale, corrupt, or unwritable cache entry must NEVER
fail the call. The tool logs a warning and degrades to a plain uncached call.

Cache stores the full, untruncated response. Truncation to
`FIRECRAWL_MAX_MARKDOWN_CHARS` is a presentation-time concern that the tools
apply at response time, not at storage time. The credit snapshot is NOT
cached — every call returns a fresh view, but a cache hit does NOT decrement
the credit counter.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .normalize import cache_path_for

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CachedResponse:
    kind: str               # "search" or "scrape"
    input: dict[str, Any]   # {"query": "..."} or {"url": "..."}
    response: dict[str, Any]


class DiskCache:
    """Filesystem TTL cache. `ttl_seconds <= 0` disables the cache entirely."""

    def __init__(self, base_dir: Path, ttl_seconds: int):
        self._dir = Path(base_dir)
        self._ttl = max(0, int(ttl_seconds))

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    def get(self, input_value: str) -> CachedResponse | None:
        if not self.enabled:
            return None
        path = self._path_for(input_value)
        try:
            if not path.exists():
                return None
            envelope = json.loads(path.read_text(encoding="utf-8"))
            fetched_at = float(envelope.get("fetched_at_unix", 0))
            if (time.time() - fetched_at) > self._ttl:
                return None
            kind = envelope.get("kind")
            input_block = envelope.get("input")
            response = envelope.get("response")
            if kind not in {"search", "scrape"}:
                log.warning("cache: %s has unknown kind %r; ignoring", path, kind)
                return None
            if not isinstance(input_block, dict) or not isinstance(response, dict):
                log.warning("cache: %s has malformed input/response; ignoring", path)
                return None
            return CachedResponse(kind=kind, input=input_block, response=response)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            log.warning("cache: read failed for %r (%s); falling through", input_value, e)
            return None

    def set(self, input_value: str, kind: str, input_block: dict[str, Any], response: dict[str, Any]) -> None:
        if not self.enabled:
            return
        path = self._path_for(input_value)
        envelope = {
            "fetched_at_unix": time.time(),
            "kind": kind,
            "input": input_block,
            "response": response,
        }
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            log.warning("cache: write failed for %r (%s); continuing without cache", input_value, e)

    def invalidate(self, input_value: str) -> None:
        if not self.enabled:
            return
        try:
            self._path_for(input_value).unlink(missing_ok=True)
        except OSError as e:
            log.warning("cache: invalidate failed for %r (%s)", input_value, e)

    def _path_for(self, input_value: str) -> Path:
        return cache_path_for(input_value, self._dir)
