"""FREEZE THE 105. They stay as authored; nothing NEW may be authored.

The ruling keeps the existing em-dash template strings and cleans them at the
exit. That only holds if the set stops growing, so this counts non-docstring
string constants containing an em/en dash per module and fails when a count
rises.

The baseline was MEASURED from the tree immediately after the patch applied —
not typed. If you are here because this test failed, you either added a dash to
a user-facing template (don't; the exit will turn it into a sentence break
mid-line) or you removed one and should lower the number deliberately.
"""
import ast
import pathlib

BASELINE = {'app/strategy.py': 24, 'app/opportunities.py': 19, 'app/capabilities.py': 15, 'app/analyzer.py': 8, 'app/intelligence.py': 20}

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _dash_strings(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if ast.get_docstring(node, clean=False) is not None and node.body:
                docs.add(id(node.body[0].value))
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs and ("—" in n.value or "–" in n.value)]


def test_no_new_em_dash_templates():
    for rel, expected in BASELINE.items():
        found = _dash_strings(ROOT / rel)
        assert len(found) <= expected, (
            f"{rel}: {len(found)} em-dash template strings, baseline {expected}. "
            f"Something new was authored. This list is NOT the new one — it is "
            f"every dash string in the file, in source order; diff it against "
            f"the branch point to find yours:\n  "
            + "\n  ".join(f"L{n.lineno}: {n.value[:64]!r}" for n in found))
