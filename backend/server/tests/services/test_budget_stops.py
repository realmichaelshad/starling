"""A budget raised in the configuration clears a stale stop when the services process starts."""
from __future__ import annotations

from starling_server.services.metering import clear_stale_trips, day_key, free_trip_limit, month_key

NOW = 1_789_000_000


def _insert(database, scope, window, cost, limit, tripped_at):
    database.write(lambda conn: conn.execute(
        "INSERT INTO budget_counters (scope, window_key, cost_micros, limit_micros, tripped_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (scope, window_key) DO UPDATE SET cost_micros = excluded.cost_micros, "
        "limit_micros = excluded.limit_micros, tripped_at = excluded.tripped_at",
        (scope, window, cost, limit, tripped_at),
    ))


def _trips(database):
    with database.read() as conn:
        return {(r["scope"], r["window_key"]): r["tripped_at"] for r in conn.execute("SELECT * FROM budget_counters")}


def test_raised_limits_clear_stale_stops_but_real_spend_keeps_them(database, settings):
    day, month = day_key(NOW), month_key(NOW)
    daily, monthly, free = settings.ai_daily_budget_micros, settings.ai_monthly_budget_micros, free_trip_limit(settings)
    _insert(database, "global_day", day, daily // 2, daily // 4, NOW - 60)  # tripped under an older, lower limit
    _insert(database, "global_month", month, monthly, monthly, NOW - 60)  # the budget really is spent
    _insert(database, "free_day", day, 0, free // 4, NOW - 60)  # today's spend is below the free share now
    _insert(database, "global_day", "2020-01-01", 5, 1, 1)  # another window is never touched

    assert database.write(lambda conn: clear_stale_trips(conn, settings, NOW)) == 2
    trips = _trips(database)
    assert trips[("global_day", day)] is None
    assert trips[("free_day", day)] is None
    assert trips[("global_month", month)] == NOW - 60
    assert trips[("global_day", "2020-01-01")] == 1
    with database.read() as conn:
        limit = conn.execute(
            "SELECT limit_micros FROM budget_counters WHERE scope = 'global_day' AND window_key = ?", (day,)
        ).fetchone()[0]
    assert limit == daily

    # Negative control: once today's spend reaches the free share, the free stop is kept.
    _insert(database, "global_day", day, free, daily, None)
    _insert(database, "free_day", day, 0, free, NOW - 30)
    assert database.write(lambda conn: clear_stale_trips(conn, settings, NOW)) == 0
    assert _trips(database)[("free_day", day)] == NOW - 30
