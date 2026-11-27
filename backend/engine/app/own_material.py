"""Material the user hands the account directly, and what it is allowed to do.

Everything the voice model knows arrives by one of two accidents: the X archive
he imported once, and the drafts he happened to edit. Neither is a way to TELL
the account something. This is that way, and it carries exactly two kinds,
because they have opposite safety properties and mixing them is how a note to
self ends up published:

    VOICE       his own writing, added to teach HOW he writes. It joins the
                voice example pool beside the archive, and like every other
                voice example it is style-only: the prompt block it lands in
                already says "STYLE ONLY ... never reuse their sentences,
                restate their content, or recycle their ideas".

    BACKGROUND  standing context that steers WHAT the account engages with —
                "I don't care about image models", "I'm bearish on agents".
                It is never shown to the writer, never quoted, and never
                reaches a prompt. It moves selection, not wording.

THE SECOND ONE IS THE DANGEROUS HALF and its safety is structural, not a
promise. Background text is never returned by anything the drafting path calls:
`voice_texts()` is what the model reads and it filters on kind, and
`tests/test_own_material.py` asserts that a background line cannot appear in a
generated prompt. If a future change makes background reachable from the
writer, that test fails rather than the account publishing his private note.

Deliberately NOT a third kind. Asked what the account should be allowed to do
with material he types, he chose voice and background and explicitly not
"facts it can state in posts" — a typed fact would be an unverifiable claim
published under his name, and this file has no mechanism that could make that
safe.
"""
from __future__ import annotations

import re
from uuid import uuid4

from .storage import _utc, connect

KIND_VOICE = "voice"
KIND_BACKGROUND = "background"
KINDS = (KIND_VOICE, KIND_BACKGROUND)

# A voice example shorter than this teaches nothing and dilutes the pool; the
# archive's own shortest usable texts sit around four words.
MIN_VOICE_WORDS = 3
MAX_TEXT_CHARS = 2_000
# The pool the writer sees is five examples; a hundred stored is already far
# more than can ever be shown, and an unbounded table is a slow prompt.
MAX_ITEMS_PER_KIND = 200


class OwnMaterialError(ValueError):
    """Rejected input, phrased for the card rather than for a log."""


def _ensure_table(conn) -> None:
    conn.execute("""
      CREATE TABLE IF NOT EXISTS own_material (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
      )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_own_material_kind "
                 "ON own_material(kind, active)")


def _clean(text: str) -> str:
    # Newlines survive — his posts have blocks and a voice example that loses
    # them teaches a shape he does not write.
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def add(kind: str, text: str) -> dict:
    """Store one item. Raises `OwnMaterialError` with a sentence he can read."""
    if kind not in KINDS:
        raise OwnMaterialError(f"Unknown kind {kind!r}.")
    body = _clean(text)
    if not body:
        raise OwnMaterialError("There is nothing in it.")
    if len(body) > MAX_TEXT_CHARS:
        raise OwnMaterialError(
            f"That is {len(body)} characters; the limit is {MAX_TEXT_CHARS}. "
            "Split it into separate entries.")
    if kind == KIND_VOICE and len(body.split()) < MIN_VOICE_WORDS:
        raise OwnMaterialError(
            f"A voice example needs at least {MIN_VOICE_WORDS} words — "
            "anything shorter teaches the model nothing.")
    with connect() as conn:
        _ensure_table(conn)
        # The same text twice is not two examples, it is one example with
        # double the weight in every pool that reads this table.
        existing = conn.execute(
            "SELECT id FROM own_material WHERE kind=? AND text=? AND active=1",
            (kind, body)).fetchone()
        if existing:
            raise OwnMaterialError("You have already added that one.")
        count = conn.execute(
            "SELECT COUNT(*) FROM own_material WHERE kind=? AND active=1",
            (kind,)).fetchone()[0]
        if count >= MAX_ITEMS_PER_KIND:
            raise OwnMaterialError(
                f"That is the {MAX_ITEMS_PER_KIND}th entry for {kind}. "
                "Remove one before adding another.")
        item_id = uuid4().hex
        conn.execute(
            "INSERT INTO own_material(id,kind,text,created_at,active) "
            "VALUES(?,?,?,?,1)", (item_id, kind, body, _utc()))
    return {"id": item_id, "kind": kind, "text": body}


def remove(item_id: str) -> bool:
    """Deactivate rather than delete, so a mis-click is recoverable."""
    with connect() as conn:
        _ensure_table(conn)
        cursor = conn.execute("UPDATE own_material SET active=0 WHERE id=? AND active=1",
                              (str(item_id or ""),))
        return cursor.rowcount > 0


def items(kind: str | None = None) -> list[dict]:
    """Everything he has added, newest first — for the settings list."""
    with connect() as conn:
        _ensure_table(conn)
        if kind is not None:
            if kind not in KINDS:
                return []
            rows = conn.execute(
                "SELECT id,kind,text,created_at FROM own_material "
                "WHERE active=1 AND kind=? ORDER BY created_at DESC", (kind,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id,kind,text,created_at FROM own_material "
                "WHERE active=1 ORDER BY created_at DESC").fetchall()
    return [{"id": row[0], "kind": row[1], "text": row[2], "created_at": row[3]}
            for row in rows]


def voice_texts(limit: int = 60) -> list[str]:
    """The writing he added, for the voice pool. THE ONLY DRAFTING-PATH READ.

    Filters on kind in the query rather than in Python: the safety property
    this module claims is that background text never reaches the writer, and a
    filter that lives next to the SELECT is one a future edit cannot drop by
    reordering something else.
    """
    with connect() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT text FROM own_material WHERE active=1 AND kind=? "
            "ORDER BY created_at DESC LIMIT ?",
            (KIND_VOICE, max(0, int(limit)))).fetchall()
    return [row[0] for row in rows if row[0]]


def background_lines(limit: int = 40) -> list[str]:
    """Standing context for SELECTION. Never returned to a prompt builder.

    Callers are the decision surfaces — what to reply to, what to post about.
    Nothing that composes a draft may call this, and the test suite pins that.
    """
    with connect() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT text FROM own_material WHERE active=1 AND kind=? "
            "ORDER BY created_at DESC LIMIT ?",
            (KIND_BACKGROUND, max(0, int(limit)))).fetchall()
    return [row[0] for row in rows if row[0]]


def summary() -> dict:
    """Counts for the settings card, cheap enough to call on every render."""
    with connect() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT kind, COUNT(*) FROM own_material WHERE active=1 GROUP BY kind"
        ).fetchall()
    counts = {kind: 0 for kind in KINDS}
    for kind, count in rows:
        if kind in counts:
            counts[kind] = int(count)
    return counts
