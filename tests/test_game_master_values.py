import uuid

from app.services.rounds import answer_id, decoded

PID = str(uuid.uuid4())


def test_decoded_passes_through_codec_values():
    assert decoded(PID) == PID
    assert decoded({"correct_profile_id": PID}) == {"correct_profile_id": PID}
    assert decoded(["a", "b"]) == ["a", "b"]


def test_decoded_unwraps_legacy_json_strings():
    assert decoded(f'"{PID}"') == PID
    assert decoded('{"choices": ["x"]}') == {"choices": ["x"]}


def test_answer_id_accepts_fixture_and_plain_answers():
    assert answer_id({"correct_profile_id": PID}) == PID
    assert answer_id(PID) == PID
    assert answer_id(f'"{PID}"') == PID
    assert answer_id(None) is None
