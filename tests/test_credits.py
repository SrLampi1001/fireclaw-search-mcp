"""Tests for the in-memory credit tracker."""
from __future__ import annotations

import pytest

from fireclaw_search_mcp.credits import CreditTracker, ToolBudgetError


def test_hydrate_sets_remaining_and_plan():
    t = CreditTracker()
    t.hydrate(
        {
            "remaining_credits": 750,
            "plan_credits": 1000,
            "billing_period_start": "2026-10-01T00:00:00Z",
            "billing_period_end": "2026-11-01T00:00:00Z",
        }
    )
    snap = t.snapshot(source="live")
    assert snap["remaining_credits"] == 750
    assert snap["plan_credits"] == 1000
    assert snap["billing_period_end"] == "2026-11-01T00:00:00Z"
    assert snap["source"] == "live"
    assert snap["probe_note"] == ""


def test_charge_decrements_remaining():
    t = CreditTracker()
    t.hydrate({"remaining_credits": 100, "plan_credits": 1000,
               "billing_period_start": None, "billing_period_end": None})
    t.charge(7)
    assert t.snapshot(source="cached")["remaining_credits"] == 93


def test_charge_floors_at_zero():
    t = CreditTracker()
    t.hydrate({"remaining_credits": 5, "plan_credits": 1000,
               "billing_period_start": None, "billing_period_end": None})
    t.charge(99)
    assert t.snapshot(source="cached")["remaining_credits"] == 0


def test_charge_with_non_positive_is_noop():
    t = CreditTracker()
    t.hydrate({"remaining_credits": 50, "plan_credits": 1000,
               "billing_period_start": None, "billing_period_end": None})
    t.charge(0)
    t.charge(-3)
    assert t.snapshot(source="cached")["remaining_credits"] == 50


def test_guard_raises_when_no_credits():
    t = CreditTracker()
    t.hydrate({"remaining_credits": 0, "plan_credits": 1000,
               "billing_period_start": None, "billing_period_end": "2026-11-01T00:00:00Z"})
    with pytest.raises(ToolBudgetError) as ei:
        t.guard(4)
    assert "out of credits" in str(ei.value).lower()
    assert "2026-11-01" in str(ei.value)


def test_guard_raises_when_below_min():
    t = CreditTracker()
    t.hydrate({"remaining_credits": 3, "plan_credits": 1000,
               "billing_period_start": None, "billing_period_end": "2026-11-01T00:00:00Z"})
    with pytest.raises(ToolBudgetError) as ei:
        t.guard(4)
    assert "3 credits" in str(ei.value)
    assert "minimum is 4" in str(ei.value)


def test_guard_passes_when_enough_credits():
    t = CreditTracker()
    t.hydrate({"remaining_credits": 100, "plan_credits": 1000,
               "billing_period_start": None, "billing_period_end": None})
    t.guard(4)  # no raise


def test_guard_skips_when_remaining_is_none():
    t = CreditTracker()  # never hydrated
    t.guard(4)  # no raise, because we don't know


def test_guard_skips_when_min_is_zero():
    t = CreditTracker()
    t.hydrate({"remaining_credits": 0, "plan_credits": 1000,
               "billing_period_start": None, "billing_period_end": None})
    t.guard(0)  # no raise, because guard is disabled


def test_mark_probe_failed_sets_note():
    t = CreditTracker()
    t.mark_probe_failed("network unreachable")
    snap = t.snapshot(source="cached")
    assert snap["probe_note"].startswith("credits unknown")
    assert "network unreachable" in snap["probe_note"]
