"""OPTION-4 P2a — the point is reachable as a value, and nothing else moved.

P2a is behaviour-neutral: the instruction string is byte-identical to what it
was, and `room.decided_point` is a new way to reach a value that was already
there. These tests pin BOTH halves, because "both payloads stay populated" is
the property that makes the migration rollback-able.
"""
from app.reply_plan import Room, _POINT_LEAD, planned_point


def test_room_defaults_the_point_to_empty():
    """Additive field: every existing caller of plan_reply is untouched."""
    assert Room().decided_point == ""


def test_the_string_and_the_value_agree_when_a_point_was_decided():
    """The migration's core invariant: two payloads, one truth.

    Built by CALLING the producer's own formatting, never by typing the string
    a consumer wants to see.
    """
    point = "the harness is what broke, not the model"
    instruction = "base direction"
    room = Room()
    # exactly what plan_reply does, both halves
    room.decided_point = point
    instruction += (f'{_POINT_LEAD}"{point}". Say exactly that, in the writer\'s '
                    "voice. Do not add a second idea.")
    assert planned_point(instruction) == point
    assert room.decided_point == point
    assert planned_point(instruction) == room.decided_point


def test_no_point_leaves_both_payloads_empty():
    """Unknown stays unknown: no point decided means no value and no marker."""
    room = Room()
    assert room.decided_point == ""
    assert planned_point("a direction with no point in it") == ""
