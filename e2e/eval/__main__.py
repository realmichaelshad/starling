"""`python -m e2e.eval`: draft for invented customers through the whole product and write a report.

    python -m e2e.eval                                # the stand-in: free, checks the tool itself
    python -m e2e.eval --real --spend-up-to 3         # the real provider, with YOUR key, at most $3

The real run reads the key from the environment variable STARLING_EVAL_AI_API_KEY, or from a file
`.env.eval` at the repository root holding one line `AI_API_KEY=sk-...` (Git ignores `.env.*`; never
commit a key). It sets the run's daily and monthly AI budgets to --spend-up-to, so the product's own
budget guard refuses every AI call once that is spent (a call already running may finish), and it
prints only the key's last four characters. Give the key a hard limit in the provider's dashboard
too. A full run is about 30 presses plus background work, typically well under a dollar with the
default model (docs/AI-PROVIDER.md), but the cap is what you actually risk.

The report is written to e2e/.run/eval/<run>/report.md, with every press's post, draft, sources,
notes, time, tokens and cost, and results.json for comparing two runs.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from e2e.harness.paths import REPO_ROOT, RUN_ROOT
from e2e.harness.stack import RealAI

from .personas import PERSONAS
from .report import write
from .run import run

KEY_ENV = "STARLING_EVAL_AI_API_KEY"
KEY_FILE = REPO_ROOT / ".env.eval"


def read_key(path: Path) -> str:
    key = os.environ.get(KEY_ENV, "").strip()
    if key:
        return key
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() in ("AI_API_KEY", KEY_ENV) and value.strip():
                return value.strip().strip('"').strip("'")
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m e2e.eval", description=__doc__.splitlines()[0])
    parser.add_argument("--real", action="store_true", help="use the real provider with your key (spends money)")
    parser.add_argument("--spend-up-to", type=float, default=None,
                        help="with --real: the most this run may spend, in US dollars (0.25 to 25)")
    parser.add_argument("--key-file", default=str(KEY_FILE), help="file holding AI_API_KEY=... (default: .env.eval)")
    parser.add_argument("--personas", default=",".join(p.key for p in PERSONAS),
                        help="which customers to draft for, e.g. terse,coach (default: all)")
    parser.add_argument("--topic-wait", type=float, default=None,
                        help="seconds to wait before each customer's Create post and chat, so background topic "
                             "research can finish (default: 45 with --real, 0 otherwise)")
    parser.add_argument("--golden", action="store_true",
                        help="the stand-in answers with the golden answers (e2e/eval/golden.py), to check the engine "
                             "keeps a great answer intact; not with --real")
    parser.add_argument("--plans", choices=("eval", "real"), default="eval",
                        help="eval: relaxed limits so the run measures drafts (default); real: the product's own "
                             "plans file, so the real Free caps (web checks, rates) show in the report")
    parser.add_argument("--out", default=None, help="report folder (default: e2e/.run/eval/<run>)")
    args = parser.parse_args(argv)

    wanted = [key.strip() for key in args.personas.split(",") if key.strip()]
    personas = tuple(p for p in PERSONAS if p.key in wanted)
    unknown = sorted(set(wanted) - {p.key for p in personas})
    if unknown or not personas:
        parser.error(f"unknown personas {unknown}; choose from {', '.join(p.key for p in PERSONAS)}")

    if args.golden and args.real:
        parser.error("--golden replaces the provider's answers, so it cannot be combined with --real")
    real = None
    if args.real:
        if args.spend_up_to is None or not 0.25 <= args.spend_up_to <= 25:
            parser.error("--real needs --spend-up-to between 0.25 and 25 (US dollars): the most this run may spend")
        key = read_key(Path(args.key_file))
        if not key:
            parser.error(f"no key: set {KEY_ENV}, or put AI_API_KEY=sk-... in {args.key_file}")
        real = RealAI(api_key=key, budget_usd=f"{args.spend_up_to:.2f}")
        print(f"[eval] REAL provider, key ending ...{key[-4:]}. This run's AI budget is ${args.spend_up_to:.2f}: "
              "the product refuses every AI call after that. Starting in 5 seconds; Ctrl+C stops it.")
        time.sleep(5)
    elif args.golden:
        print("[eval] the stand-in serving the golden answers: free. This run checks that the engine keeps them intact.")
    else:
        print("[eval] the stand-in: free. Its text is deterministic filler, so this run checks the tool, not the writing.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    label = "real" if real else ("golden" if args.golden else "stand-in")
    out_dir = (Path(args.out) if args.out else RUN_ROOT / "eval" / f"{stamp}-{label}").resolve()
    golden_script = None
    if args.golden:
        try:
            from .golden_script import build_rules
        except ImportError:
            parser.error("e2e/eval/golden_script.py is not there yet: it turns the golden answers into stand-in rules")
        import json
        out_dir.mkdir(parents=True, exist_ok=True)
        golden_script = out_dir / "golden-script.json"
        golden_script.write_text(json.dumps(build_rules(), ensure_ascii=False, indent=1), encoding="utf-8")
    topic_wait = args.topic_wait if args.topic_wait is not None else (45.0 if real else 0.0)
    record = run(personas, real=real, out_dir=out_dir, topic_wait_s=topic_wait, golden_script=golden_script,
                 plans=args.plans)
    report = write(record, out_dir)
    failed = [o.press for o in record.outcomes if o.error and not (o.draft or o.answer)]
    tool_fails = [f"{o.press}:{check['expect']}" for o in record.outcomes for check in o.checks if not check.get("ok")]
    print(f"[eval] {len(record.outcomes)} presses, {len(failed)} failed, spend ${record.total_cost_micros / 1_000_000:.4f}")
    if tool_fails:
        print(f"[eval] world-tool checks failed: {', '.join(tool_fails)}")
    print(f"[eval] report: {report}")
    return 1 if failed and not real else 0


if __name__ == "__main__":
    sys.exit(main())
