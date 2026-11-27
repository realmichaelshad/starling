"""Topic precision (2026-07-28): the live pool served a $1B security
acquisition, a VC fund-data feed, an openness-policy paper and planetary
geospatial inference, all builder_fit=True. His beat is building with models
— releases, coding tools, pricing, benchmarks, local running — never capital
flows, governance papers, or domains that merely run inference."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.subject import builder_fit                        # noqa: E402

OFF_BEAT = [
    "Cyera agrees to acquire Oasis Security for $1B to safeguard data",
    "Fund Momentum MCP: Live VC Fund Data for AI Agents",
    "Unpacking Open Source AI: Toward a Framework for Openness in Foundation Models",
    "The OlmoEarth Platform: Geospatial inference at planetary scale",
    # The distrust audit's traps (2026-07-29), locked in:
    "Databricks acquires MosaicML for $1.3B",
    "OpenAI secures Series F at $300B valuation",
    "EU AI Act regulation enters enforcement phase",
    "Startup lands $50M to build agent infrastructure",
    "Toward a Framework for Responsible Agentic AI",
    "Satellite imagery model maps planetary deforestation",
]

HIS_BEAT = [
    "Nous Research's NousCoder-14B is an open-source coding model",
    "Claude Code costs up to $200 a month. Goose does the same for free",
    "Kimi K3 Now Available via Telnyx Inference API",
    "Open-source engine running Gemma 4 26B in 2 GB RAM on any M-series Mac",
    "Anthropic launches Cowork, a Claude Desktop agent that works in your files",
    "Bringing Nunchaku 4-bit Diffusion Inference to Diffusers",
    "CogniKernel; AI Coding with cross session and cross platform memory",
    # The audit's near-miss keepers: verb collisions ("acquires new skills",
    # "raises prices"), the "teams"-of-people collision, version-glued names
    # (Qwen3.5 never matched `qwen\b`), and tools the list simply lacked.
    "Cursor raises prices for Pro subscriptions",
    "Claude Code acquires new skills through plugins",
    "Anthropic ships governance controls for Claude Code teams",
    "Qwen3.5 tops SWE-bench with open weights",
    "vLLM adds Blackwell GPU support for local inference",
    "GPT-5.6 rate limits cut in half",
]


def test_capital_policy_and_foreign_domains_are_off_beat():
    wrong = [t for t in OFF_BEAT if builder_fit(t)]
    assert not wrong, f"passed off-beat topic: {wrong[0]!r}"


def test_his_actual_beat_still_passes():
    lost = [t for t in HIS_BEAT if not builder_fit(t)]
    assert not lost, f"lost his topic: {lost[0]!r}"


def test_notability_outranks_merely_being_on_the_beat():
    """The complaint, 2026-07-29: a picked topic looked like nothing this
    account would talk about; it had only been on Hacker News. Measured on
    the live pool that produced it: 22 topics, only 5 naming anything he
    writes about, 17 naming nothing
    recognisable — and seven of those seventeen passed the lane filter
    because a Show HN blurb says "coding agents". The picker took OpenLore
    over "Claude Code costs $200, Goose does it free" and "Anthropic launches
    Cowork", because the rotation rule steers away from a subject used
    recently and lands in obscurity.

    An open-weights MODEL release is his beat whoever shipped it — the
    parameter count is the tell a tool launch never carries.
    """
    from app.subject import names_notable

    obscure = [
        "OpenLore: local-first memory and guardrails for AI coding agents",
        "MindFlock - Parallel AI coding agents, each in its own Git worktree",
        "Supapool - a Supabase per coding agent in ~400 ms",
        "UnUI - Help coding agents make better design decisions",
        "Free Formatter - Online coding tools website shuts down after 10 years",
    ]
    # Recognisable with NO archive at all — a fresh install still knows the
    # difference between a lab everyone has heard of and a Show HN handle.
    known = [
        "Claude Code costs up to $200 a month. Goose does the same for free",
        "Anthropic launches Cowork, a Claude Desktop agent",
        "Claude Opus 5 became ruthless when tasked with running a business",
        "Kimi K3 now available via Telnyx Inference API",
        "Cursor tab completion runs on a smaller model",
        # Unknown lab, real model release — the parameter count carries it.
        "Nous Research's NousCoder-14B is an open-source coding model",
        "Open-source engine running Gemma 4 26B in 2 GB RAM on any Mac",
    ]
    leaked = [t for t in obscure if names_notable(t)]
    assert not leaked, f"an unknown Show HN project rated notable: {leaked[0]!r}"
    lost = [t for t in known if not names_notable(t)]
    assert not lost, f"real news rated unnotable: {lost[0]!r}"


def test_notability_is_learned_from_whoever_owns_the_archive():
    """NOT HARDCODED TO ONE ACCOUNT — an explicit requirement. A name is
    notable because THIS archive returns to it, so a subject nobody has
    heard of becomes notable for the account that writes about it, and stays
    unnotable for everyone else. A public figure is notable for an account
    only because its own posts mention them twice; on a clean install they
    are not.
    """
    from app import storage, subject as subject_mod
    from app.subject import names_notable

    topic = "Ottolenghi opens a second bakery in Notting Hill"
    assert not names_notable(topic), "unknown to a clean install, as it should be"

    with storage.connect() as conn:
        for index, text in enumerate([
                "Ottolenghi's new cookbook is worth it for the pantry chapter.",
                "Went back to Ottolenghi today, the aubergine is still the move.",
                "Bakery pricing in London is out of hand.",
        ]):
            conn.execute("insert into posts (post_id, text) values (?, ?)",
                         (f"food-{index}", text))
        conn.commit()
    subject_mod._LANE_CACHE = None

    assert names_notable(topic), "the account's own repeated subject must count"
    # ...and their archive does not make somebody else's Show HN notable.
    assert not names_notable("UnUI - Help coding agents make better design decisions")


def test_sentence_openers_never_become_subjects():
    """The capitalised-name fallback dragged "Alright", "Look", "Half" and
    "People" into the archive's subject set, where they could make any topic
    containing those words look like his."""
    from app.subject import subjects
    for opener in ("Alright", "Look", "Half", "People", "Work", "Crazy",
                   "Honestly", "Okay", "Wow"):
        assert opener.lower() not in subjects(f"{opener} this changes things")


def test_vibe_core_separates_his_lane_from_adjacent_ai():
    """The account's stated lane (2026-07-29) is Claude Code and the vibe
    coding world, not LLMs broadly. Core = what it would actually post;
    adjacent AI clears builder_fit but ranks last and carries the off-lane
    wait."""
    from app.subject import vibe_core
    core = [
        "Claude Code costs up to $200 a month. Goose does the same for free",
        "Cursor raises prices for Pro subscriptions",
        "Nous Research's NousCoder-14B is an open-source coding model",
        "GPT-5.6 rate limits cut in half",
        "Qwen3.5 tops SWE-bench with open weights",
        "vLLM adds Blackwell GPU support for local inference",
    ]
    adjacent = [
        "FeyNoBg – Automatic background removal model and training library",
        "Evals before prompts: building an LLM OCR for KYC",
        "SOTA on the hardest AI memory benchmark (BEAM, 10M tokens)",
        "Bringing Nunchaku 4-bit Diffusion Inference to Diffusers",
    ]
    missed = [t for t in core if not vibe_core(t)]
    assert not missed, f"core topic rated adjacent: {missed[0]!r}"
    leaked = [t for t in adjacent if vibe_core(t)]
    assert not leaked, f"adjacent topic rated core: {leaked[0]!r}"
