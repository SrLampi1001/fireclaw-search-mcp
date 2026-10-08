"""In-memory credit counter for the Firecrawl budget.

Per SPECS §7: the counter is best-effort local. `remaining_credits` is
synced once at startup via `/v2/team/credit-usage` (a metadata endpoint),
then decremented by each call's `creditsUsed` so the running balance stays
accurate without re-probing on every tool invocation. Two server processes
sharing one API key will each see their own local counter drift; for a
single-process deployment this is accurate enough.

The counter is intentionally not a connection-keyed singleton — sessions
are gone in MCP 2026-07-28 — but its state lives in the lifespan context so
it is shut down with the FastMCP server rather than leaking between
restarts.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class CreditTracker:
    """Thread-safe wrapper around the local snapshot."""

    remaining_credits: int | None = None
    plan_credits: int | None = None
    billing_period_start: str | None = None
    billing_period_end: str | None = None
    last_sync_unix: float = 0.0
    sync_ok: bool = False
    initial_probe_note: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def hydrate(self, snapshot: dict[str, Any]) -> None:
        """Apply the result of a `/v2/team/credit-usage` call."""
        with self._lock:
            self.remaining_credits = snapshot.get("remaining_credits")
            self.plan_credits = snapshot.get("plan_credits")
            self.billing_period_start = snapshot.get("billing_period_start")
            self.billing_period_end = snapshot.get("billing_period_end")
            self.last_sync_unix = _now()
            self.sync_ok = self.remaining_credits is not None
            if self.sync_ok:
                self.initial_probe_note = ""

    def mark_probe_failed(self, error: str) -> None:
        """Record that the startup probe failed; the tool will surface a note."""
        with self._lock:
            self.sync_ok = False
            if not self.initial_probe_note:
                self.initial_probe_note = (
                    f"credits unknown — startup probe failed: {error}. "
                    f"The first tool call will proceed but cannot be guarded."
                )

    def charge(self, used: int) -> None:
        """Decrement the counter after a successful upstream call."""
        if used <= 0:
            return
        with self._lock:
            if self.remaining_credits is not None:
                self.remaining_credits = max(0, self.remaining_credits - used)

    def guard(self, min_credits: int) -> None:
        """Raise a model-friendly error if the local counter is too low.

        Skips the check entirely if the counter is `None` (probe never
        succeeded); the real API error from the upstream call will surface
        and the model can react then.
        """
        if min_credits <= 0:
            return
        with self._lock:
            r = self.remaining_credits
            resets = self.billing_period_end
        if r is None:
            return
        if r <= 0:
            raise ToolBudgetError(
                f"Firecrawl is out of credits for this billing period "
                f"(resets {resets or 'next cycle'}). Switch to a free sibling "
                f"search tool (e.g. a DuckDuckGo MCP) and ask the user to "
                f"top up the Firecrawl team or wait for the reset."
            )
        if r < min_credits:
            raise ToolBudgetError(
                f"Firecrawl has only {r} credits left (the configured "
                f"minimum is {min_credits}). Switch to a free sibling search "
                f"tool or wait for the billing period to reset on "
                f"{resets or 'the next cycle'}."
            )

    def snapshot(self, *, source: str) -> dict[str, Any]:
        """Read-only view returned to the model in every call.

        `source` is `"live"` when the snapshot was just fetched, `"cached"`
        when this is the in-memory counter.
        """
        with self._lock:
            return {
                "remaining_credits": self.remaining_credits,
                "plan_credits": self.plan_credits,
                "billing_period_start": self.billing_period_start,
                "billing_period_end": self.billing_period_end,
                "source": source,
                "probe_note": self.initial_probe_note,
            }


def _now() -> float:
    import time
    return time.time()


class ToolBudgetError(RuntimeError):
    """Raised when the credit guard refuses a call. Surfaces as a ToolError."""
