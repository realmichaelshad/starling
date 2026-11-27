"""The client code for an engine error on the assist stream (engines/streams.py client_error)."""
from starling_server.engines.streams import client_error


def test_an_engine_bad_request_reaches_the_client_as_bad_request():
    code, message, retry = client_error({"code": "bad_request", "message": "no post"})
    assert code == "bad_request"
    assert message
    assert retry is None


def test_unknown_engine_codes_still_read_as_ai_unavailable():
    assert client_error({"code": "something_new"})[0] == "ai_unavailable"
    assert client_error("not a dict")[0] == "ai_unavailable"


def test_fair_use_and_pause_still_pass_through():
    assert client_error({"code": "fair_use_exceeded"})[0] == "fair_use_exceeded"
    assert client_error({"code": "budget_exhausted"})[0] == "service_paused"
