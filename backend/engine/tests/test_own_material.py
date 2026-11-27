"""Material he hands the account: it must arrive, and half of it must not.

Two claims, and neither is worth anything unasserted:

  * a VOICE example he types reaches the writer's prompt — the failure mode
    this module has committed most often is a setting that stores something
    nothing ever reads (see the inert-filter and cached-pool findings);
  * a BACKGROUND note NEVER reaches it. He chose background over "facts it can
    state" precisely so a private note cannot be published, and that has to be
    a property of the code rather than an intention.
"""
import pytest

from app import own_material as om


@pytest.fixture(autouse=True)
def clean_table():
    from app.storage import connect
    with connect() as conn:
        om._ensure_table(conn)
        conn.execute("DELETE FROM own_material")
    yield
    with connect() as conn:
        conn.execute("DELETE FROM own_material")


VOICE = "half the price and this close to Fable 5 is kinda insane"
BACKGROUND = "I do not care about image models and never want to post about them"


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def test_a_voice_example_round_trips():
    added = om.add(om.KIND_VOICE, VOICE)
    assert added["text"] == VOICE
    assert [item["text"] for item in om.items(om.KIND_VOICE)] == [VOICE]


def test_the_two_kinds_do_not_mix():
    om.add(om.KIND_VOICE, VOICE)
    om.add(om.KIND_BACKGROUND, BACKGROUND)
    assert om.voice_texts() == [VOICE]
    assert om.background_lines() == [BACKGROUND]
    assert om.summary() == {"voice": 1, "background": 1}


def test_removing_is_reversible_not_destructive():
    item = om.add(om.KIND_VOICE, VOICE)
    assert om.remove(item["id"]) is True
    assert om.voice_texts() == []
    assert om.remove(item["id"]) is False, "already gone"


@pytest.mark.parametrize("kind,text,fragment", [
    (om.KIND_VOICE, "", "nothing in it"),
    (om.KIND_VOICE, "   \n ", "nothing in it"),
    (om.KIND_VOICE, "too short", "at least"),
    (om.KIND_VOICE, "x" * 2_500, "limit is"),
    ("nonsense", VOICE, "Unknown kind"),
])
def test_bad_input_is_refused_with_a_readable_reason(kind, text, fragment):
    with pytest.raises(om.OwnMaterialError) as caught:
        om.add(kind, text)
    assert fragment in str(caught.value)


def test_the_same_example_twice_is_one_example():
    om.add(om.KIND_VOICE, VOICE)
    with pytest.raises(om.OwnMaterialError, match="already added"):
        om.add(om.KIND_VOICE, VOICE)


def test_a_short_background_note_is_allowed():
    """The word floor is about teaching style; a standing note is not that."""
    om.add(om.KIND_BACKGROUND, "no crypto")
    assert om.background_lines() == ["no crypto"]


def test_blocks_survive_because_his_posts_have_them():
    om.add(om.KIND_VOICE, "First block.\n\nSecond block.")
    assert om.voice_texts() == ["First block.\n\nSecond block."]


# --------------------------------------------------------------------------- #
# It has to actually reach the writer.
# --------------------------------------------------------------------------- #
def test_a_voice_example_reaches_the_voice_pool():
    """The whole point. A store nothing reads is the inert fix this module
    keeps shipping — so assert against the pool the drafting path uses, not
    against the table."""
    from app.intelligence import PersonalModel

    om.add(om.KIND_VOICE, VOICE)
    pool = [row["text"] for row in PersonalModel().example_rows]
    assert VOICE in pool


def test_it_is_weighted_above_an_ordinary_archive_post():
    from app.intelligence import PersonalModel

    om.add(om.KIND_VOICE, VOICE)
    mine = [row for row in PersonalModel().example_rows if row["text"] == VOICE]
    assert mine and mine[0]["performance"] >= 0.9


# --------------------------------------------------------------------------- #
# ...and background has to NOT.
# --------------------------------------------------------------------------- #
def test_background_never_enters_the_voice_pool():
    from app.intelligence import PersonalModel

    om.add(om.KIND_BACKGROUND, BACKGROUND)
    pool = " ".join(row["text"] for row in PersonalModel().example_rows)
    assert BACKGROUND not in pool
    assert "image models" not in pool


def test_the_drafting_path_cannot_read_background():
    """Structural, not a promise: nothing under the writer imports it.

    He chose background over "facts it can state in posts" so that a private
    note could never be published. If a future edit wires `background_lines`
    into a prompt builder, this fails here rather than on his timeline.
    """
    import inspect

    from app import intelligence, llm, reply_plan

    for module in (intelligence, llm, reply_plan):
        source = inspect.getsource(module)
        assert "background_lines" not in source, (
            f"{module.__name__} reads background material; it composes drafts, "
            "so a private note could reach a post")


def test_voice_material_is_the_only_drafting_read():
    """The companion claim: the one function the writer may call is filtered."""
    import inspect

    source = inspect.getsource(om.voice_texts)
    assert "KIND_VOICE" in source
    assert "kind=?" in source, "the kind filter must live in the query"
