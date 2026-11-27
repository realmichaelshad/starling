""""Today" meant UTC's today, and the zone resolver made it worse.

Two layers, and the second is why the first was invisible.

LAYER 1. `timing._posts_today` and `opportunities._today_posting_note` compared
`created.astimezone(utc).date()` against `now.astimezone(utc).date()`. That number
gates the daily post budget, and it was wrong in BOTH directions inside his real
posting hours (measured peak 18:00 local, with a real post at 20:06):

  * a post at 20:06 on the 5th is 00:06 UTC on the 6th, so at 09:00 local the
    next morning it still counted as "today" — the gate told him to hold off on a
    day he had not posted at all.
  * a post at 19:00 on the 5th is 23:00 UTC the same day, so at 21:00 local
    (01:00 UTC the 6th) it counted as ZERO — the gate let him post over budget.

LAYER 2. Fixing layer 1 by resolving `_display_timezone` through the WRONG helper
changes nothing, and that is what happened first. `_display_timezone` returns the
machine's zone whenever the profile's timezone is unset or "UTC" — the state every
user is in before a profile is built — and on Windows that is a DISPLAY name,
"Eastern Daylight Time", which ZoneInfo cannot parse. `timing._zone` and a bare
`ZoneInfo(...)`/`except: utc` both answer UTC there, landing straight back on the
UTC calendar day.

`strategy._count_window` had the same broken resolver, so named months and days
were built in UTC for exactly the same users.

LAYER 3, found when both suites were first run inside the Linux image. "The machine's
own zone" was the right answer for a desktop app and is the WRONG one for a hosted
engine: the server's clock is UTC and says nothing about where the customer is, so
every customer without a stored zone was back on the UTC calendar day — the bug this
file exists to prevent. It was invisible because the developer's machine is not
on UTC, which made the fallback look correct, and because every test here but
one skipped itself on a UTC machine. The zone now comes from the one place that knows
it: `X-Starling-Time-Zone`, which the extension sends and the gateway forwards on every
request (`posting_hours.effective_timezone`). These tests bind that header rather than
asking the machine, so they mean the same thing on any machine.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import request_context, storage
from app.account_facts import _display_timezone, _zone
from app.opportunities import _today_posting_note
from app.timing import _posts_today


NY = ZoneInfo("America/New_York")


def as_the_customer(zone: str = "America/New_York"):
    """Serve the block as one request from a browser in `zone`, which is how production runs."""
    return request_context.bind(time_zone=zone)


def _row(when: datetime):
    return {"post_type": "post", "created_at": when.isoformat()}


# --------------------------------------------------------------------------- #
# layer 2 — the resolver
# --------------------------------------------------------------------------- #

def test_the_customers_own_zone_decides_not_the_servers():
    """The hosted path, and the one that matters: the server's clock is UTC everywhere.

    With no stored zone (the state every customer starts in) the answer must be the zone the
    browser reported for this request. If this ever falls back to the machine again, every
    calendar-boundary answer in the product silently reverts to UTC for most customers.
    """
    storage.initialize()
    with as_the_customer("America/New_York"):
        assert _display_timezone(storage.get_account_profile() or {}) == "America/New_York"
        resolved = _zone(_display_timezone(storage.get_account_profile() or {}))
        evening = datetime(2026, 8, 5, 20, 6, tzinfo=NY)
        assert evening.astimezone(resolved).date() == datetime(2026, 8, 5).date(), (
            "an evening post rolled over to the next day: the zone came from the server, not the customer")
    with as_the_customer("Asia/Tokyo"):
        assert _display_timezone(storage.get_account_profile() or {}) == "Asia/Tokyo"


def test_a_stored_zone_still_wins_over_the_browsers():
    """An explicit choice is a choice; the header is only the default."""
    storage.initialize()
    storage.set_account_profile({"timezone": "Europe/Berlin"})
    with as_the_customer("America/New_York"):
        assert _display_timezone(storage.get_account_profile() or {}) == "Europe/Berlin"


def test_a_name_that_will_not_parse_falls_back_to_the_customer_not_to_utc():
    """Windows reports zones as display names ("Eastern Daylight Time") that ZoneInfo cannot read.

    Answering UTC there relabels UTC clock times as the customer's zone, which asserts something
    false. The request's zone is the honest answer, on any machine.
    """
    with as_the_customer("America/New_York"):
        resolved = _zone("Eastern Daylight Time")
        assert getattr(resolved, "key", None) == "America/New_York"
        probe = datetime(2026, 8, 5, 20, 6, tzinfo=NY).astimezone(resolved)
        assert probe.date() == datetime(2026, 8, 5).date()


def test_an_iana_name_still_wins():
    assert _zone("America/New_York").key == "America/New_York"


def test_a_hosted_engine_that_knows_nothing_says_utc_rather_than_guessing(monkeypatch):
    """No stored zone and no header: UTC is honest. The SERVER's own zone never is."""
    monkeypatch.setenv("STARLING_HOSTED", "1")
    storage.initialize()
    assert _display_timezone(storage.get_account_profile() or {}) == "UTC"
    assert str(_zone("Eastern Daylight Time")) == "UTC"


# --------------------------------------------------------------------------- #
# layer 1 — the count that gates the budget
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("made, asked, expected, why", [
    (datetime(2026, 8, 5, 20, 6, tzinfo=NY), datetime(2026, 8, 6, 9, 0, tzinfo=NY), 0,
     "an evening post is not today's post the next morning"),
    (datetime(2026, 8, 5, 20, 6, tzinfo=NY), datetime(2026, 8, 5, 21, 0, tzinfo=NY), 1,
     "an evening post is today's post that same evening"),
    (datetime(2026, 8, 5, 19, 0, tzinfo=NY), datetime(2026, 8, 5, 21, 0, tzinfo=NY), 1,
     "19:00 is 23:00 UTC, and UTC arithmetic counted this as zero"),
    (datetime(2026, 8, 5, 9, 0, tzinfo=NY), datetime(2026, 8, 5, 12, 0, tzinfo=NY), 1,
     "a morning post on the same day"),
    (datetime(2026, 8, 5, 23, 30, tzinfo=NY), datetime(2026, 8, 6, 0, 30, tzinfo=NY), 0,
     "just past local midnight is a new day"),
])
def test_posts_today_counts_his_calendar_day(made, asked, expected, why):
    storage.initialize()
    with as_the_customer("America/New_York"):
        assert _posts_today([_row(made)], asked) == expected, why


def test_the_note_and_the_budget_gate_agree_about_today():
    """They read the same number for the same decision. When one used UTC and the
    other did not, the dock's note and the WAIT verdict disagreed."""
    storage.initialize()
    evening = datetime(2026, 8, 5, 20, 6, tzinfo=NY)
    next_morning = datetime(2026, 8, 6, 9, 0, tzinfo=NY)
    rows = [_row(evening), _row(evening), _row(evening)]
    with as_the_customer("America/New_York"):
        assert _posts_today(rows, next_morning) == 0
        note = _today_posting_note(rows, next_morning)
    assert note is None or "already dated today" not in note, (
        f"the note still thinks yesterday evening was today: {note}")
