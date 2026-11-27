"""A randomized run of 1,000 metered requests reconciles exactly (CONTRACTS section 7).

Requests run on 8 threads against 5 accounts (some with tight allowances, so refusals happen
while others commit). The test keeps its own model of which attempts are charged; afterwards
the counters, the ledger and the reservations must all agree with that model.
"""
from __future__ import annotations

import random
import threading
from concurrent.futures import ThreadPoolExecutor

from starling_server.usage import grants, service
from starling_server.usage.service import Replay, Reservation, UsageError

from .conftest import NOW, body_hash, counters, make_account, new_key, record_ai_call

REQUESTS = 1_000
SEED = 20260914
ALLOWANCES = (10_000, 10_000, 150, 60, 20)
RESULT = '{"ok": true, "answer": "A draft"}'
SCENARIOS = (
    "success", "stream_success", "no_ai", "failure", "unusable", "cancel_before",
    "cancel_after", "fail_after_exposure", "error_after_exposure", "stop_after_exposure",
    "abandon_exposed", "abandon_silent",
)
EXPECTED_REFUSALS = {"limit_reached", "duplicate_request"}


class Run:
    def __init__(self, db, settings, accounts):
        self.db, self.s, self.accounts = db, settings, accounts
        self.lock = threading.Lock()
        self.charged = 0
        self.tally: dict[str, int] = {}
        self.unexpected: list[str] = []

    def count(self, name: str, charged: bool = False) -> None:
        with self.lock:
            self.tally[name] = self.tally.get(name, 0) + 1
            self.charged += int(charged)

    def spend(self, rng, attempt, now) -> None:
        record_ai_call(self.db, attempt.id, cost_micros=rng.randint(1, 5_000), now=now)

    def settle(self, rng, attempt: Reservation, scenario: str, now: int) -> bool:
        """Drive a reserved attempt through `scenario`; return whether the contract charges it."""
        db, s = self.db, self.s
        service.activate(db, attempt.id, now)
        if scenario == "success":
            for _ in range(rng.randint(1, 3)):
                self.spend(rng, attempt, now)
            service.finish(db, s, attempt.id, True, RESULT, now)
            return True
        if scenario == "stream_success":
            self.spend(rng, attempt, now)
            service.mark_exposed(db, attempt.id, now)
            service.finish(db, s, attempt.id, True, RESULT, now)
            return True
        if scenario == "no_ai":
            service.finish(db, s, attempt.id, True, RESULT, now)
            return False
        if scenario in ("failure", "cancel_before"):
            if rng.random() < 0.5:
                self.spend(rng, attempt, now)
            service.release(db, s, attempt.id, "error" if scenario == "failure" else "cancelled", now)
            return False
        if scenario == "unusable":
            self.spend(rng, attempt, now)
            service.finish(db, s, attempt.id, False, None, now)
            return False
        if scenario == "cancel_after":
            self.spend(rng, attempt, now)
            service.mark_exposed(db, attempt.id, now)
            service.release(db, s, attempt.id, "cancelled", now)
            return True
        if scenario == "fail_after_exposure":  # a preview, then withheld
            service.mark_exposed(db, attempt.id, now)
            service.finish(db, s, attempt.id, False, None, now)
            return False
        if scenario == "error_after_exposure":
            self.spend(rng, attempt, now)
            service.mark_exposed(db, attempt.id, now)
            service.release(db, s, attempt.id, rng.choice(["engine_error", "timeout", "server_error"]), now)
            return False
        if scenario == "stop_after_exposure":
            service.mark_exposed(db, attempt.id, now)
            service.finish(db, s, attempt.id, False, None, now, customer_stopped=True)
            return True
        if scenario == "abandon_exposed":
            service.mark_exposed(db, attempt.id, now)
            return False  # the final sweep releases it: it died on Starling's side
        self.spend(rng, attempt, now)  # abandon_silent: the final sweep releases it
        return False

    def attempt(self, rng, account, key, body, scenario, now):
        try:
            result = service.reserve(self.db, self.s, account, key, body, "/api/v1/assist/stream", "chat", now)
        except UsageError as exc:
            if exc.code not in EXPECTED_REFUSALS:
                raise
            self.count(exc.code)
            return exc
        if isinstance(result, Replay):
            assert result.result_json == RESULT
            self.count("replay")
            return result
        self.count(scenario, self.settle(rng, result, scenario, now))
        if not scenario.startswith("abandon") and rng.random() < 0.2:
            service.finish(self.db, self.s, result.id, True, RESULT, now)  # repeats must change nothing
            service.release(self.db, self.s, result.id, "error", now)
            self.count("repeat_finish")
        return result

    def retry(self, rng, account, key, body, scenario, now) -> None:
        first = self.attempt(rng, account, key, body, scenario, now)
        second = self.attempt(rng, account, key, body, rng.choice(SCENARIOS), now + 1)
        if isinstance(first, Reservation):
            if scenario in ("success", "stream_success"):
                assert isinstance(second, Replay), (scenario, second)
            elif scenario.startswith("abandon"):
                assert isinstance(second, UsageError) and second.code == "duplicate_request", (scenario, second)
            else:
                assert not isinstance(second, Replay), (scenario, second)

    def duplicates(self, rng, account, key, body, scenario, now) -> None:
        barrier = threading.Barrier(2)
        results: list[object] = []

        def contender() -> None:
            barrier.wait()
            try:
                results.append(service.reserve(self.db, self.s, account, key, body, "/api/v1/assist/stream", "chat", now))
            except UsageError as exc:
                results.append(exc)

        threads = [threading.Thread(target=contender) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        winners = [r for r in results if isinstance(r, Reservation)]
        codes = sorted(r.code for r in results if isinstance(r, UsageError))
        assert len(results) == 2 and len(winners) <= 1
        for code in codes:
            self.count(code)
        if winners:
            assert codes == ["duplicate_request"]
            self.count(scenario, self.settle(rng, winners[0], scenario, now))
        else:
            assert codes == ["limit_reached", "limit_reached"]

    def flow(self, index: int) -> None:
        rng = random.Random(SEED + index)
        account = rng.choice(self.accounts)
        now = NOW + index % 200  # nothing expires and every stored result stays replayable during the run
        key, body, scenario = new_key(), body_hash({"request": index}), rng.choice(SCENARIOS)
        try:
            pick = rng.random()
            if pick < 0.15:
                self.retry(rng, account, key, body, scenario, now)
            elif pick < 0.22:
                self.duplicates(rng, account, key, body, scenario, now)
            elif pick < 0.24:
                grants.grant_credits(self.db, self.s, account, rng.randint(1, 3), "admin-1", "Randomized grant", now)
                self.count("grant")
            else:
                self.attempt(rng, account, key, body, scenario, now)
        except Exception as exc:  # collected and asserted empty below
            with self.lock:
                self.unexpected.append(f"request {index}: {exc!r}")


def test_randomized_requests_reconcile(database, roomy_settings, fake_entitlements):
    db, s = database, roomy_settings
    accounts = [make_account(db, s) for _ in ALLOWANCES]
    for account, allowance in zip(accounts, ALLOWANCES):
        fake_entitlements.set(account, allowance=allowance, cost_ceiling_micros=10**12)
    run = Run(db, s, accounts)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run.flow, range(REQUESTS)))
    assert run.unexpected == []

    expired = service.sweep_expired(db, s, NOW + 10_000)
    assert len(expired) == run.tally.get("abandon_exposed", 0) + run.tally.get("abandon_silent", 0)

    periods = [fake_entitlements.period_id(account) for account in accounts]
    totals = [counters(db, account, period) for account, period in zip(accounts, periods)]
    with db.read() as conn:
        ledger_rows, ledger_credits = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM credit_ledger WHERE kind = 'commit'").fetchone()
        charged_rows = conn.execute(
            "SELECT COUNT(*) FROM usage_reservations WHERE state = 'committed' OR (state = 'expired' AND release_reason IS NULL)"
        ).fetchone()[0]
        live_rows = conn.execute("SELECT COUNT(*) FROM usage_reservations WHERE state IN ('reserved', 'active')").fetchone()[0]
    assert run.charged > 0
    assert sum(total["committed"] for total in totals) == run.charged
    assert (ledger_rows, ledger_credits, charged_rows) == (run.charged, run.charged, run.charged)
    assert sum(total["reserved"] for total in totals) == 0 and live_rows == 0
    assert service.verify_counters(db) == []
    for name in SCENARIOS + ("replay", "duplicate_request", "limit_reached", "grant", "repeat_finish"):
        assert run.tally.get(name, 0) > 0, f"the run never exercised {name}: {run.tally}"
