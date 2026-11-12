"""Unit tests for event selection and per-condition deduplication."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/domres"
PACKAGE_NAME = "_domres_events_test_package"
_test_package = types.ModuleType(PACKAGE_NAME)
_test_package.__path__ = [str(COMPONENT)]
sys.modules.setdefault(PACKAGE_NAME, _test_package)


def _load_module(name: str):
    qualified_name = f"{PACKAGE_NAME}.{name}"
    if qualified_name in sys.modules:
        return sys.modules[qualified_name]
    spec = importlib.util.spec_from_file_location(qualified_name, COMPONENT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified_name] = module
    spec.loader.exec_module(module)
    return module


_load_module("data")
_constants = _load_module("const")
_events = _load_module("events")
normalize_deadline_days = _constants.normalize_deadline_days
DomResEventTracker = _events.DomResEventTracker
EVENT_READING_DEADLINE = _events.EVENT_READING_DEADLINE
EVENT_UNPAID_BILL = _events.EVENT_UNPAID_BILL


def snapshot(
    *,
    account_id: str = "account-1",
    days: int | None = 20,
    balance: float | None = 0,
    available: bool = True,
    period_from: str | None = "01.11.2026",
    period_to: str | None = "25.11.2026",
) -> dict:
    availability = None
    if days is not None:
        availability = {
            "days": days,
            "indicationAvailability": available,
            "title": "Период передачи показаний",
        }
    balance_data = None
    if balance is not None:
        balance_data = {"balance": {"value": balance}, "fine": {"value": 1.5}}
    indication_status = None
    if period_from is not None and period_to is not None:
        indication_status = {
            "status": "NOT_DECLARED",
            "datePeriodFrom": period_from,
            "datePeriodTo": period_to,
        }
    return {
        "account_id": account_id,
        "name": "Тестовый лицевой счёт",
        "availability": availability,
        "indication_status": indication_status,
        "balance": balance_data,
    }


class ReadingDeadlineEventTests(unittest.TestCase):
    def test_event_fires_once_in_threshold_and_rearms_after_period_resets(self) -> None:
        tracker = DomResEventTracker(deadline_days=3)
        self.assertEqual(tracker.update([snapshot(days=4)]), [])
        events = tracker.update([snapshot(days=3)])
        self.assertEqual([name for name, _ in events], [EVENT_READING_DEADLINE])
        self.assertEqual(events[0][1]["days_remaining"], 3)
        self.assertEqual(events[0][1]["deadline_days"], 3)
        self.assertEqual(tracker.update([snapshot(days=2)]), [])
        self.assertEqual(tracker.update([snapshot(days=0)]), [])

        self.assertEqual(tracker.update([snapshot(days=4)]), [])
        next_period = tracker.update([snapshot(days=3)])
        self.assertEqual([name for name, _ in next_period], [EVENT_READING_DEADLINE])

    def test_unavailable_period_does_not_raise_deadline_event(self) -> None:
        tracker = DomResEventTracker(deadline_days=3)
        self.assertEqual(tracker.update([snapshot(days=2, available=False)]), [])
        events = tracker.update([snapshot(days=2, available=True)])
        self.assertEqual([name for name, _ in events], [EVENT_READING_DEADLINE])


class UnpaidBillEventTests(unittest.TestCase):
    def test_event_fires_once_per_period_and_not_again_after_balance_clears(self) -> None:
        tracker = DomResEventTracker(deadline_days=3)
        events = tracker.update([snapshot(days=20, balance=125.5)])
        self.assertEqual([name for name, _ in events], [EVENT_UNPAID_BILL])
        self.assertEqual(events[0][1]["balance"], 125.5)
        self.assertEqual(events[0][1]["fine"], 1.5)
        self.assertEqual(events[0][1]["period_from"], "2026-11-01")
        self.assertEqual(events[0][1]["period_to"], "2026-11-25")
        self.assertEqual(tracker.update([snapshot(days=20, balance=125.5)]), [])
        self.assertEqual(tracker.update([snapshot(days=20, balance=0)]), [])
        self.assertEqual(tracker.update([snapshot(days=20, balance=1)]), [])

        new_period = snapshot(
            days=20,
            balance=1,
            period_from="01.12.2026",
            period_to="25.12.2026",
        )
        next_period_events = tracker.update([new_period])
        self.assertEqual([name for name, _ in next_period_events], [EVENT_UNPAID_BILL])
        self.assertEqual(next_period_events[0][1]["period_to"], "2026-12-25")

    def test_stored_period_suppresses_duplicate_after_restart(self) -> None:
        tracker = DomResEventTracker(deadline_days=3)
        self.assertEqual(
            [name for name, _ in tracker.update([snapshot(days=20, balance=10)])],
            [EVENT_UNPAID_BILL],
        )
        restored = DomResEventTracker(deadline_days=3, stored_state=tracker.stored_state)
        self.assertEqual(restored.update([snapshot(days=20, balance=10)]), [])
        self.assertEqual(
            [
                name
                for name, _ in restored.update(
                    [snapshot(days=20, balance=10, period_from="01.12.2026", period_to="25.12.2026")]
                )
            ],
            [EVENT_UNPAID_BILL],
        )

    def test_missing_period_or_balance_does_not_create_or_clear_unpaid_state(self) -> None:
        tracker = DomResEventTracker(deadline_days=3)
        no_period = snapshot(days=20, balance=10, period_from=None, period_to=None)
        self.assertEqual(tracker.update([no_period]), [])
        self.assertEqual(tracker.update([snapshot(days=20, balance=None)]), [])
        self.assertEqual(
            [name for name, _ in tracker.update([snapshot(days=20, balance=10)])],
            [EVENT_UNPAID_BILL],
        )
        self.assertEqual(tracker.update([snapshot(days=20, balance=None)]), [])
        self.assertEqual(tracker.update([snapshot(days=20, balance=10)]), [])

    def test_invalid_period_dates_are_rejected(self) -> None:
        tracker = DomResEventTracker(deadline_days=3)
        bad_period = snapshot(
            days=20,
            balance=10,
            period_from="26.11.2026",
            period_to="25.11.2026",
        )
        self.assertEqual(tracker.update([bad_period]), [])

    def test_deadline_threshold_is_limited_to_one_through_seven_days(self) -> None:
        self.assertEqual(DomResEventTracker(deadline_days=1).deadline_days, 1)
        self.assertEqual(DomResEventTracker(deadline_days=7).deadline_days, 7)
        with self.assertRaises(ValueError):
            DomResEventTracker(deadline_days=0)
        with self.assertRaises(ValueError):
            DomResEventTracker(deadline_days=8)

    def test_legacy_deadline_options_are_clamped_into_supported_range(self) -> None:
        self.assertEqual(normalize_deadline_days(0), 1)
        self.assertEqual(normalize_deadline_days(8), 7)
        self.assertEqual(normalize_deadline_days(31), 7)
        self.assertEqual(normalize_deadline_days("invalid"), 3)
        self.assertEqual(normalize_deadline_days(None), 3)
