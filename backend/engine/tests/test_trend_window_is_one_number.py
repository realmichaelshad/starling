"""The trend freshness gate and its consumers cannot disagree.

`_has_trends()` asked for a trend inside 2 HOURS while every consumer read 48 —
including the consumer three lines below it in the same `if`.  On 3-hour-old
data the gate returned False and the 48-hour read that would have succeeded
never ran, so `_account_grounding` silently dropped trend names the rest of the
product was using.

`main.py:1258` records the identical defect being found and fixed for the chat
consumer — *"one of them was lying, and it was this one"*.  The gate was the
surviving half of that bug.

MEASURED on his live trend corpus (385 captures over 17.5 days, sampled every
10 minutes): the consumers had data 94.4% of elapsed time, the gate said
"no trends" for 88.5%, and the two DISAGREED for **82.9%** of the span.  The
gate discarding usable data was its normal state, not an edge case.

The cure is structural, not arithmetic: ONE constant, read by both.  Two aligned
literals would pass today and drift on the next edit — the registries-must-agree
failure this project has now hit in four places.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.storage import TREND_FRESH_HOURS, list_trend_snapshots

MAIN = Path(__file__).resolve().parents[1] / "app" / "main.py"


def test_the_constant_is_the_default():
    """A caller that omits `hours` must get the shared window, not 2.0."""
    import inspect

    signature = inspect.signature(list_trend_snapshots)
    assert signature.parameters["hours"].default == TREND_FRESH_HOURS


def test_no_call_site_hardcodes_a_window():
    """THE POINT OF THIS FILE.  Every `list_trend_snapshots(` call in main.py
    either omits `hours=` or passes the symbol — never a number.  A numeric
    literal here is how the gate and its consumers drifted apart the first
    time."""
    source = MAIN.read_text(encoding="utf-8")
    bad = re.findall(r"list_trend_snapshots\(\s*hours\s*=\s*([0-9][^,)]*)", source)
    assert not bad, f"hardcoded trend windows in main.py: {bad}"


def test_the_gate_and_its_own_consumer_agree():
    """`_account_grounding` gates on `_has_trends()` and then reads with its own
    window three lines later.  Those two must be the same number — that pair IS
    the bug."""
    source = MAIN.read_text(encoding="utf-8")
    block = source[source.index("def _account_grounding"):]
    block = block[:block.index("def ", 10)]
    assert "_has_trends()" in block
    assert "hours=TREND_FRESH_HOURS" in block or re.search(
        r"list_trend_snapshots\(\s*limit", block), block[-400:]


def test_the_gate_reads_the_shared_constant():
    """The gate passes the symbol, not a number.

    The first version of this test asserted `"2" not in <gate source>` — which
    failed on its own explanatory comment, because that comment cites the 2-hour
    window it replaced and the 82.9% it cost. An absence assertion over prose is
    not a check on code; assert the positive form instead.
    """
    source = MAIN.read_text(encoding="utf-8")
    gate = source[source.index("def _has_trends"):]
    gate = gate[:gate.index("def ", 10)]
    call = re.search(r"list_trend_snapshots\(([^)]*)\)", gate)
    assert call, gate
    assert "hours=TREND_FRESH_HOURS" in call.group(1), call.group(1)
