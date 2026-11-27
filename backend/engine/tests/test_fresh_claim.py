"""FRESH-CLAIM-1 — the product stops asserting a date it does not have."""
import time

from app.radar import _timeliness


def test_no_date_means_no_time_claim():
    """The 92% case: RSS sent nothing, so we say nothing about when."""
    line = _timeliness({"source": "Together AI", "created_at": 0})
    assert line == "From Together AI."
    for word in ("today", "hour", "day", "ago", "recent"):
        assert word not in line.lower()


def test_a_real_timestamp_becomes_a_real_age():
    six_hours = time.time() - 6 * 3600
    assert _timeliness({"source": "Hacker News", "created_at": six_hours}) == \
        "Published 6 hours ago on Hacker News."


def test_a_day_old_story_is_not_called_today():
    """The measured failure: 2 of 9 HN stories were already over 24 hours."""
    line = _timeliness({"source": "Hacker News", "created_at": time.time() - 34 * 3600})
    assert "1 day ago" in line
    assert "today" not in line.lower()


def test_an_unparsable_stamp_is_unknown_not_recent():
    assert _timeliness({"source": "X", "created_at": "not a date"}) == "From X."


def test_the_hardcoded_claim_is_gone_from_the_source():
    """It was asserted on 24 of 24 candidates; it must not come back.

    ASSERTS ON EMITTABLE STRING LITERALS, NOT RAW TEXT. The first version of
    this test checked the whole file and failed after the fix — because the
    docstrings EXPLAINING the removal contain the sentence. A guard that
    forbids a string cannot tell code from prose about the code, and the prose
    is the part a future reader needs most.
    """
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1] / "app" / "radar.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if ast.get_docstring(node, clean=False) is not None and node.body:
                docstrings.add(id(node.body[0].value))
    emittable = [n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and id(n) not in docstrings]
    offenders = [text for text in emittable if "Published today" in text]
    assert not offenders, f"the unfounded date claim can still be emitted: {offenders}"


# ---------------------------------------------------------------------------
# FRESH-CLAIM-2 — the same defect class, widened from one sentence to the FIELD.
#
# "Published today on X." was one instance. The rule is that a candidate's `why` must
# report an OBSERVED time or say nothing, so the guard belongs on the field, not on a
# sentence. Two more were live when this was written:
#     radar.py  "Live on the web right now, in the lane he writes about."
#     radar.py  "It shipped today - this is the live conversation."
# and neither path carries a timestamp at all (`recent_releases` returns none).
#
# WHY NOT THE OBVIOUS GUARD. Forbidding /today|right now|.../ across every emittable
# literal was tried first and rejected by measurement: it flags 21 strings across
# radar.py and main.py, and almost all are legitimate - conversational copy that ASKS
# the user something ("what's worth posting about right now?"), the STOPWORDS blob, and
# two SCRUBBING regexes whose entire job is removing these claims. A guard that forbids
# its own fix is worse than no guard. So this matches the two shapes that actually reach
# a reader as a claim about a story's age.
# ---------------------------------------------------------------------------

_TIME_CLAIM = __import__("re").compile(
    r"\b(today|right now|just (shipped|dropped|launched)|trending now)\b",
    __import__("re").IGNORECASE)


def _why_time_claims(source: str) -> list[tuple[int, str]]:
    """String literals that become a candidate's `why` AND assert a time.

    shape 1   {"why": "<literal>"}                 - the candidate constructors
    shape 2   <expr>.get("why") or "<literal>"     - the fallback fed to the writer
    """
    import ast

    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "why"
                        and isinstance(value, ast.Constant) and isinstance(value.value, str)
                        and _TIME_CLAIM.search(value.value)):
                    found.append((value.lineno, value.value))
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            reads_why = any(
                isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                and v.func.attr == "get" and v.args
                and isinstance(v.args[0], ast.Constant) and v.args[0].value == "why"
                for v in node.values)
            if reads_why:
                for v in node.values:
                    if (isinstance(v, ast.Constant) and isinstance(v.value, str)
                            and _TIME_CLAIM.search(v.value)):
                        found.append((v.lineno, v.value))
    return found


def _app_source(name: str) -> str:
    import pathlib
    return (pathlib.Path(__file__).resolve().parents[1] / "app" / name).read_text(encoding="utf-8")


def test_no_radar_why_asserts_a_time_it_did_not_observe():
    """radar.py builds four candidate dicts; none may claim an age from a dateless path."""
    offenders = _why_time_claims(_app_source("radar.py"))
    assert not offenders, f"a `why` asserts an unobserved time: {offenders}"


def test_the_guard_can_fire():
    """NEGATIVE CONTROL. A guard nobody has watched fail is decoration.

    Both shapes, against synthetic source - so this cannot be quietly satisfied by the
    detector breaking (an AST typo would return [] on real files and look like a pass).
    """
    shape_one = 'items.append({"topic": t, "why": "It shipped today - live conversation."})'
    shape_two = 'msg = f"Why: {grounding.get(\'why\') or \'it is trending now\'}."'

    assert _why_time_claims(shape_one), "shape 1 (dict literal) not detected"
    assert _why_time_claims(shape_two), "shape 2 (get-or fallback) not detected"
    assert not _why_time_claims('items.append({"why": "From Together AI."})'), \
        "an OBSERVED, time-free why must not be flagged"


@__import__("pytest").mark.xfail(
    strict=True,
    reason="KNOWN LIVE DEFECT, NOT MINE TO FIX. main.py feeds `... or 'it is trending now'` "
           "straight into the WRITER'S PROMPT, so the model is instructed that a story of "
           "unknown age is current - the same class as 'Published today on X.' but causal "
           "rather than cosmetic. main.py is Lane Q's file and the edit routes through the "
           "supervisor (R-SCOPE-1). strict=True on purpose: when it is fixed this test XPASSES "
           "and FAILS the run, forcing this marker to be deleted rather than forgotten.")
def test_main_py_prompt_fallback_asserts_no_time():
    offenders = _why_time_claims(_app_source("main.py"))
    assert not offenders, f"a `why` fallback asserts an unobserved time: {offenders}"


# ---------------------------------------------------------------------------
# FRESH-CLAIM-3 — the candidate carries the TIMESTAMP, not only a sentence about it.
#
# The stamp was in scope in `_web_candidates` and thrown away after being rendered to
# English, so 0 of 24 live candidates carried a date and every downstream consumer had to
# parse age back out of prose or shrug. Sorting, the staleness gate and the moment
# detector all need the number.
# ---------------------------------------------------------------------------

def test_every_candidate_carries_a_published_at(monkeypatch):
    """0 = UNKNOWN. Present on every candidate, so no consumer has to guess."""
    import time

    from app import news as news_mod
    from app import radar as radar_mod

    an_hour_ago = time.time() - 3600
    stories = [
        {"title": "Qwen3.5 tops SWE-bench", "url": "https://example.com/a",
         "source": "Hacker News", "summary": "A new open-weights model.",
         "created_at": an_hour_ago},
        {"title": "Mistral ships a new coding model", "url": "https://example.com/b",
         "source": "Together AI", "summary": "No date on this feed.",
         "created_at": 0},
        # THE CRASH CASE. `int(story["created_at"] or 0)` — the obvious one-liner —
        # raises ValueError here and takes out the whole candidate build. _timeliness
        # already tolerates it (test_an_unparsable_stamp_is_unknown_not_recent), so the
        # field and the sentence must tolerate it identically or they disagree.
        {"title": "Llama 4.2 benchmarks land", "url": "https://example.com/c",
         "source": "VentureBeat AI", "summary": "Junk stamp.",
         "created_at": "not a date"},
    ]
    monkeypatch.setattr(news_mod, "headlines", lambda *a, **k: stories)

    items = radar_mod._web_candidates(["model releases"], limit=24, force=True)

    assert items, "fixture stories should survive the beat filter"
    for item in items:
        assert "published_at" in item, f"candidate has no published_at: {item.get('topic')!r}"
        assert isinstance(item["published_at"], int), \
            f"published_at must be an int epoch, got {type(item['published_at'])}"

    by_topic = {str(i.get("topic") or ""): i for i in items}
    dated = [i for i in items if i["published_at"] > 0]
    assert dated, "the dated story should carry a real epoch"
    assert abs(dated[0]["published_at"] - an_hour_ago) < 2

    for topic, item in by_topic.items():
        if item["published_at"] == 0:
            assert "ago" not in str(item.get("why") or "").lower(), \
                f"unknown date must not produce a time claim: {topic!r} -> {item.get('why')!r}"


# ---------------------------------------------------------------------------------------
# FRESH-CLAIM-4 — the coarse band (R-BAND-2), the writer's input.
#
# `_timeliness()` is for the CARD and stays a sentence. The band is for the WRITER: no source
# name, no digits, nothing quotable. Boundaries are R-SORT-1's `_age_tier` cuts so a
# candidate's band and its sort tier cannot disagree about the same story.
# ---------------------------------------------------------------------------------------

def test_the_four_bands():
    import time

    from app.radar import _published_band

    now = time.time()
    assert _published_band(now - 60) == "breaking"
    assert _published_band(now - 5 * 3600) == "breaking"
    assert _published_band(now - 7 * 3600) == "recent"
    assert _published_band(now - 23 * 3600) == "recent"
    assert _published_band(now - 25 * 3600) == "old"
    assert _published_band(now - 400 * 24 * 3600) == "old"


def test_the_boundaries_are_half_open_and_closed_at_the_bottom():
    """6h is `recent`, not `breaking`; 24h is `old`, not `recent`.

    A one-hour disagreement here is the `TREND_FRESH_HOURS` bug - 2h against 48h, disagreeing
    on 82.9% of cases with no test able to see it. Lane Q implements the same four names, so
    the edges are pinned on both sides.
    """
    import time

    from app.radar import _published_band

    now = time.time()
    assert _published_band(now - 6 * 3600 - 1) == "recent"
    assert _published_band(now - 24 * 3600 - 1) == "old"


def test_unknown_is_not_a_synonym_for_old():
    """Absence of evidence is not evidence of staleness - the rule `topic_staleness` states.

    A FUTURE timestamp lands here too: unusable, not fresh. Without that branch a clock skew
    or a mis-parsed feed date would promote a story to `breaking` on the strength of a
    negative age.
    """
    import time

    from app.radar import _published_band

    assert _published_band(0) == "unknown"
    assert _published_band(None) == "unknown"
    assert _published_band("not a date") == "unknown"
    assert _published_band(time.time() + 9999) == "unknown"


def test_a_verified_release_is_breaking_without_a_timestamp():
    """`release_status` observes that something SHIPPED but never when.

    That is the one case where a band is asserted with no date behind it, and it is an
    observation rather than an inference - which is why it is a named keyword argument and
    not a silent fallback for every dateless candidate.
    """
    from app.radar import _published_band

    assert _published_band(0, verified_release=True) == "breaking"
    assert _published_band(0) == "unknown", "only a VERIFIED release may skip the date"


def test_every_candidate_carries_a_band(monkeypatch):
    """End to end, alongside published_at, so a consumer never has to guess."""
    import time

    from app import news as news_mod
    from app import radar as radar_mod

    stories = [
        {"title": "Qwen3.5 tops SWE-bench", "url": "https://example.com/a",
         "source": "Hacker News", "summary": "Benchmarks.", "created_at": time.time() - 3600},
        {"title": "Mistral ships a new coding model", "url": "https://example.com/b",
         "source": "Together AI", "summary": "No date.", "created_at": 0},
    ]
    monkeypatch.setattr(news_mod, "headlines", lambda *a, **k: stories)

    items = radar_mod._web_candidates(["model releases"], limit=24, force=True)
    assert items
    for item in items:
        assert item["published_band"] in {"breaking", "recent", "old", "unknown"}
        if item["published_at"] == 0:
            assert item["published_band"] == "unknown"


# ---------------------------------------------------------------------------------------
# FRESH-CLAIM-5 — the age GATE, and the landmine that made it dangerous to build.
#
# R-SORT-1 has a HARD DEPENDENCY on this: lane-first ranking WITHOUT a freshness gate sends a
# 214-day-old "Claude Code costs up to $200 a month" straight to the top, because it is
# maximally on-lane. The gate lands first or the key does not land.
# ---------------------------------------------------------------------------------------

def test_sub_day_ages_parse_as_hours_not_days():
    """THE LANDMINE, with the negative control the design asked for.

    `_AGE_IN_DAYS.get(unit, 1)` defaulted an unknown unit to ONE DAY. Adding `hour` to the
    pattern without its divisor would make `_timeliness()`'s own output - "Published 17 hours
    ago on Hacker News." - parse as SEVENTEEN DAYS and mark every sub-day candidate stale.
    The freshest stories would be the first ones dropped, silently.
    """
    import time

    from app import radar as radar_mod

    fresh = {"topic": "Qwen3.5 tops SWE-bench",
             "published_at": int(time.time() - 20 * 3600),
             "evidence": "Published 17 hours ago on Hacker News.", "why": ""}
    assert radar_mod._evidence_is_old(fresh) is False

    # NEGATIVE CONTROL: restore the old divisor and the same candidate must flip to stale.
    original = dict(radar_mod._AGE_IN_DAYS)
    try:
        radar_mod._AGE_IN_DAYS["hour"] = 1
        assert radar_mod._evidence_is_old(fresh) is True, \
            "the detector cannot see the bug it was written for"
    finally:
        radar_mod._AGE_IN_DAYS.clear()
        radar_mod._AGE_IN_DAYS.update(original)

    assert radar_mod._evidence_is_old(fresh) is False, "control did not clean up"


def test_an_unknown_unit_is_skipped_never_defaulted():
    """Guessing is what made the old code dangerous. A unit we do not know is not evidence."""
    from app import radar as radar_mod

    assert radar_mod._evidence_is_old(
        {"topic": "x", "evidence": "Published 400 fortnights ago", "published_at": 0}) is False


def test_the_field_condemns_an_old_candidate_even_with_silent_prose():
    """The 214-day story: nothing in its text says how old it is - the FIELD does."""
    import time

    from app import radar as radar_mod

    assert radar_mod._evidence_is_old(
        {"topic": "Claude Code costs up to $200 a month. Goose does the same for free",
         "published_at": int(time.time() - 214 * 86400), "evidence": "", "why": ""}) is True


def test_no_date_and_no_prose_is_not_stale():
    """ABSENCE OF EVIDENCE IS NOT EVIDENCE OF STALENESS - `topic_staleness`'s standing rule.

    92% of RSS items carried no date before the deploy. A gate that treated unknown as old
    would have emptied the pool on the strength of a missing field.
    """
    from app import radar as radar_mod

    assert radar_mod._evidence_is_old(
        {"topic": "Mistral ships a new coding model", "published_at": 0,
         "evidence": "No date on this feed.", "why": "From Together AI."}) is False


def test_a_future_timestamp_is_unknown_not_fresh():
    """Clock skew or a mis-parsed feed date must not certify a story as fresh."""
    import time

    from app import radar as radar_mod

    assert radar_mod._age_days(time.time() + 9999) is None
    assert radar_mod._age_days(0) is None
