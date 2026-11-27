"""The evaluation's report: one Markdown page to read, and the raw results as JSON."""
from __future__ import annotations

import json
import statistics
from dataclasses import asdict
from pathlib import Path

from .golden import GOLDEN, VOICE_CARDS
from .run import Outcome, Run

KIND_NAMES = {
    "star": "✦ on a post", "create_reply": "Composer: new reply", "create_post": "Composer: new post",
    "enhance": "Composer: improve my draft", "chat": "Chat",
}


def _usd(micros: int) -> str:
    return f"${micros / 1_000_000:.4f}" if micros < 10_000_000 else f"${micros / 1_000_000:.2f}"


def _pct(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))]


def _quote(text: str) -> str:
    return "\n".join("> " + line if line.strip() else ">" for line in (text or "").strip().splitlines()) or "> (empty)"


def _summary(outcomes: list[Outcome]) -> list[str]:
    lines = ["| Surface | Presses | Median time | 95th percentile | First text (median) | AI calls per press | Cost per press |",
             "|---|---|---|---|---|---|---|"]
    for kind, name in KIND_NAMES.items():
        rows = [o for o in outcomes if o.kind == kind]
        if not rows:
            continue
        seconds = [o.seconds for o in rows]
        firsts = [o.first_text_seconds for o in rows if o.first_text_seconds is not None]
        calls = [len(o.calls) for o in rows]
        cost = [o.cost_micros for o in rows]
        lines.append(
            f"| {name} | {len(rows)} | {statistics.median(seconds):.2f} s | {_pct(seconds, 0.95):.2f} s | "
            f"{(f'{statistics.median(firsts):.2f} s') if firsts else '–'} | {statistics.mean(calls):.1f} | "
            f"{_usd(int(statistics.mean(cost)))} |")
    return lines


def _checks(outcomes: list[Outcome]) -> list[str]:
    """The world-tool checks: a press that expected a web check, a linked page or a news story."""
    rows = [(o, check) for o in outcomes for check in (o.checks or [])]
    if not rows:
        return ["No press in this run expected a world tool.", ""]
    failed = sum(1 for _, check in rows if not check.get("ok"))
    lines = [f"{len(rows) - failed} of {len(rows)} passed." + (" **Failed checks are flagged below.**" if failed else ""),
             "", "| Press | Expects | Result | Why |", "|---|---|---|---|"]
    for outcome, check in rows:
        mark = "pass" if check.get("ok") else "**FAIL**"
        lines.append(f"| {outcome.press} | {check.get('expect')} | {mark} | {str(check.get('why') or '')[:200]} |")
    return lines + [""]


def _press(o: Outcome) -> list[str]:
    lines = [f"### {o.press} · {KIND_NAMES.get(o.kind, o.kind)}", ""]
    if o.post:
        metrics = o.post.get("visible_metrics") or {}
        lines += [f"On a post by {o.post.get('author')} (@{o.post.get('author_handle')}), "
                  f"{metrics.get('views', 0):,} views, {metrics.get('replies', 0)} replies:", "", _quote(o.post.get("text", "")), ""]
    links = (o.post or {}).get("links") or []
    if links:
        lines += ["The post links: " + " · ".join(str(url) for url in links), ""]
    if o.draft_text:
        lines += ["Their rough draft:", "", _quote(o.draft_text), ""]
    if o.message:
        lines += [f"They asked: *{o.message}*", ""]
    if o.error and not (o.draft or o.answer):
        lines += [f"**Failed** ({o.status}): `{o.error}`", ""]
    else:
        decided = f"**Starling:** {o.action or 'answer'}"
        if o.verdict_why and o.action == "wait":
            decided += f": {o.verdict_why}"
        lines += [decided, ""]
        if o.draft:
            lines += ["**Draft:**", "", _quote(o.draft), ""]
        if o.answer and o.answer != o.draft:
            lines += ["**Answer:**" if not o.draft else "**Card text:**", "", _quote(o.answer), ""]
        if o.sources:
            lines += ["**Sources:** " + " · ".join(f"[{s['title'] or s['url']}]({s['url']})" for s in o.sources), ""]
        if o.notes:
            lines += ["**Notes:** " + "; ".join(o.notes), ""]
    first = f", first text after {o.first_text_seconds:.2f} s" if o.first_text_seconds is not None else ""
    cached = f" ({o.cached_input_tokens:,} cached)" if o.cached_input_tokens else ""
    golden = GOLDEN.get(o.press)
    if golden is not None:
        lines += ["*An excellent answer (golden):* " + (f"wait: {golden.why}" if golden.verdict == "wait" else golden.text.replace("\n", " / ")), ""]
    for check in o.checks or []:
        lines += [f"*Tool check ({check.get('expect')}):* {'pass' if check.get('ok') else '**FAIL**'}: {check.get('why')}", ""]
    lines += [f"*What good looks like:* {o.good}", "",
              f"`{o.seconds:.2f} s{first} · {len(o.calls)} AI call(s) · {o.input_tokens:,} in{cached} / "
              f"{o.output_tokens:,} out · {_usd(o.cost_micros)}`", ""]
    if o.provider_calls:
        lines += [f"<details><summary>What the model was given ({len(o.provider_calls)} call(s))</summary>", ""]
        for number, call in enumerate(o.provider_calls, start=1):
            shown = call.get("messages") or call.get("input") or call.get("prompt")
            text = json.dumps(shown, ensure_ascii=False, indent=1) if shown is not None else json.dumps(call, ensure_ascii=False)[:1500]
            lines += [f"Call {number} ({call.get('model') or call.get('path') or 'model'}):", "", "```", text[:12_000], "```", ""]
            answer = call.get("response_text") or call.get("output_text") or call.get("answer")
            if answer:
                lines += ["Answer:", "", "```", str(answer)[:4_000], "```", ""]
        lines += ["</details>", ""]
    return lines


def _read(run: Run, persona: str) -> list[str]:
    """What Starling concluded about the account, beside what an excellent model would conclude."""
    read = run.reads.get(persona) or {}
    lines = ["### Starling's read of this account", ""]
    card = read.get("voice_card")
    if card:
        body = card.get("card") or card.get("card_json") or card.get("body") or card
        lines += ["Voice card (the engine's newest):", "", "```", json.dumps(body, ensure_ascii=False, indent=1)[:4_000]
                  if not isinstance(body, str) else body[:4_000], "```", ""]
    elif read.get("tables"):
        lines += ["No voice card was stored during the run.", ""]
    else:
        lines += ["The engine's database could not be read.", ""]
    topics = read.get("topics") or []
    if topics:
        lines += ["Topics it found for this account:", ""]
        for topic in topics:
            title = topic.get("title") or topic.get("headline") or topic.get("development") or ""
            url = topic.get("url") or topic.get("source_url") or ""
            lines += [f"- {str(title)[:200]}" + (f" ({url})" if url else "")]
        lines += [""]
    golden = VOICE_CARDS.get(persona)
    if golden:
        lines += ["What an excellent read would say: beats " + ", ".join(golden["beats"]) + ". " + str(golden["card"]), ""]
    return lines


def write(run: Run, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(asdict(run), indent=1, ensure_ascii=False), encoding="utf-8")
    provider = {
        "real": "the real provider",
        "stand-in, golden answers": "the stand-in serving the golden answers (e2e/eval/golden.py): this run checks "
                                    "whether the engine keeps a great answer intact, fast and sourced",
    }.get(run.provider, "the stand-in (its text is deterministic filler: this run checks the tool, not the writing)")
    models = ", ".join(f"{lane} {model}" for lane, model in sorted(run.models.items())) or "not reported"
    background_cost = sum(int(call.get("cost_micros") or 0) for call in run.background)
    lines = [
        "# Starling AI evaluation", "",
        f"Run {run.started_at[:19].replace('T', ' ')} UTC against {provider}. Models: {models}. "
        f"Spend: {_usd(run.total_cost_micros)} in total (presses {_usd(run.total_cost_micros - background_cost)}, "
        f"background work such as summaries, voice profiles and topic research {_usd(background_cost)}), "
        f"capped at ${run.budget_usd} by the product's own AI budget.", "",
        "Every time below is measured at the extension's API, so it includes the gateway, the engine and the "
        "provider. The first press for each customer also starts that customer's engine.", "",
        "Plans: " + ("the product's own plans file (the real Free caps)" if getattr(run, "plans", "eval") == "real"
                     else "relaxed evaluation limits (EVAL_PLANS)") + ". World tools: " + {
            "fixtures": "linked pages and news stories from e2e/eval/world_fixtures.json (the stand-in world)",
            "real": "the real web", "offline": "offline (no page and no news story can be read)",
        }.get(getattr(run, "world", ""), "not reported") + ".", "",
        "## Speed and cost", "", *_summary(run.outcomes), "",
        "## World tools", "", *_checks(run.outcomes),
    ]
    for persona in dict.fromkeys(o.persona for o in run.outcomes):
        lines += [f"## {run.about.get(persona, persona)}", ""]
        samples = run.samples.get(persona) or []
        if samples:
            lines += [f"Their own writing ({len(samples)} samples), for comparison:", ""]
            lines += [_quote(sample) for sample in samples[:6]]
            lines += [""]
        else:
            lines += ["No writing of their own yet.", ""]
        lines += _read(run, persona)
        for outcome in (o for o in run.outcomes if o.persona == persona):
            lines += _press(outcome)
    report = out_dir / "report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report
