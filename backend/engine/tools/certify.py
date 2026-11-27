"""Run the ONE certification, and refuse to report a verdict it cannot support.

Four full-suite attempts have been made on this batch and none reached a verdict:

    run 1  died with its parent process at ~90%, zero failures written, no record
    run 2  killed deliberately to apply a fix, declared superseded
    run 3  ran ~50 min at 2.3% CPU, then VOIDED - eight files changed DURING it
    run 4  killed by the supervisor at 1150/3422, zero failures, box contention

Run 3 is the one this script exists for. A suite that takes an hour, on a tree
three sessions are writing to, cannot produce a verdict about any tree - and
nothing in pytest notices. It reports green about a mixture.

So this hashes `app/` and `tools/` BEFORE and AFTER, and a single changed byte
VOIDS the run rather than qualifying it. A voided run is a fact about the run.
A green run over a moving tree is a false statement about the product.

    python tools/certify.py             run it
    python tools/certify.py --baseline  print the hash and exit, to announce a freeze

Exit 0 only on: suite green AND tree unchanged. Anything else is non-zero, and
the reason is printed as the FIRST line so it cannot be lost in scrollback.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCHED = ("app", "tools")
LOG = ROOT / "certification.log"
REPORT = ROOT / "certification.json"


def tree_hash() -> tuple[str, dict[str, str]]:
    """One hash over every .py in the watched dirs, plus the per-file map.

    The map is what makes a VOID actionable: "something changed" sends someone
    hunting; "radar.py changed" is a name to ask about.
    """
    per_file: dict[str, str] = {}
    for name in WATCHED:
        base = ROOT / name
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(ROOT).as_posix()
            per_file[rel] = hashlib.md5(path.read_bytes()).hexdigest()
    combined = hashlib.md5(
        "\n".join(f"{k}:{v}" for k, v in sorted(per_file.items())).encode()
    ).hexdigest()
    return combined, per_file


def main() -> int:
    if "--baseline" in sys.argv:
        combined, per_file = tree_hash()
        print(f"BASELINE {combined}  ({len(per_file)} files)")
        return 0

    before_hash, before_files = tree_hash()
    started = time.time()

    # WRITE THE REPORT BEFORE RUNNING, NOT ONLY AFTER.
    #
    # This file is what a lane checks to know whether a freeze is live, and for
    # three runs it never existed - because it was written as the LAST act and
    # every one of those runs was killed before reaching it. A verdict file that
    # only appears when nothing went wrong is absent exactly when it is needed,
    # and a precondition gated on it blocks every write forever while reading as
    # airtight. Caught by a lane that checked the underlying fact instead of
    # obeying the rule.
    #
    # So: RUNNING goes down first. A killed run leaves RUNNING on disk, which is
    # the true statement, and a reader can tell "in progress" from "never ran".
    REPORT.write_text(json.dumps({
        "verdict": "RUNNING", "reason": "certification in progress",
        "started_at": round(started, 1), "hash_before": before_hash,
        "file_count": len(before_files),
    }, indent=1), encoding="utf-8")

    print(f"baseline {before_hash}  ({len(before_files)} files)")
    print(f"log      {LOG}")
    print("running the full suite with the box to itself. this takes ~50 minutes.\n",
          flush=True)

    with LOG.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(
            # -v and --tb=short, NOT -q, and the reason is a measured one: the
            # first attempt died at 25% with TWO failures visible as bare "F"
            # marks and no way to name them, because -q defers every detail to a
            # summary that a killed run never reaches. A log that is only
            # readable if the process survives is not a log. Same law this
            # campaign applied to every other instrument: READABLE LOGS, NOT
            # CLEAN VERDICTS - and the instrument that enforced it was the one
            # breaking it.
            [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short",
             "-p", "no:randomly", "--durations=25"],
            cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, text=True)

    elapsed = time.time() - started
    after_hash, after_files = tree_hash()
    tail = LOG.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    summary = next((ln for ln in reversed(tail)
                    if " passed" in ln or " failed" in ln or " error" in ln), "(no summary line)")
    # A truncated run has no summary, but -v means every failure is already
    # named in the log. Surface them rather than reporting "(no summary line)".
    failed_names = [ln.split(" ")[0] for ln in tail if " FAILED" in ln or ln.endswith("FAILED")]
    if failed_names:
        summary = f"{summary} | FAILED: {', '.join(failed_names[:12])}"

    changed = sorted(
        set(before_files) ^ set(after_files)
        | {k for k in before_files.keys() & after_files.keys()
           if before_files[k] != after_files[k]})

    # VOID BEATS EVERYTHING, including green. A green verdict over a tree that
    # moved is the failure this whole script exists to prevent, and it is more
    # dangerous than a red one because nobody re-runs a pass.
    if changed:
        verdict = "VOID"
        reason = (f"{len(changed)} file(s) changed during the run - the suite did not "
                  f"test any single tree: {', '.join(changed[:6])}"
                  + (" ..." if len(changed) > 6 else ""))
    elif proc.returncode == 0:
        verdict = "GREEN"
        reason = "suite passed and the tree did not move"
    else:
        verdict = "RED"
        reason = f"pytest exit {proc.returncode}"

    print(f"\n{verdict}: {reason}")
    print(f"  {summary}")
    print(f"  elapsed  {elapsed/60:.1f} min")
    print(f"  before   {before_hash}")
    print(f"  after    {after_hash}")

    REPORT.write_text(json.dumps({
        "verdict": verdict, "reason": reason, "summary": summary,
        "elapsed_s": round(elapsed, 1), "exit_code": proc.returncode,
        "hash_before": before_hash, "hash_after": after_hash,
        "changed_during_run": changed, "file_count": len(before_files),
    }, indent=1), encoding="utf-8")

    return 0 if verdict == "GREEN" else 1


if __name__ == "__main__":
    raise SystemExit(main())
