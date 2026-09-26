import asyncio
import uuid

import numpy as np
import pytest

from app.ai import host, rounds
from app.ai.guard import check_host_line, check_round_text
from app.ai.picker import ItemView, MomentView, eligible_items, pick_moment, split_score
from app.domain import GameType

IDS = {n: str(uuid.uuid5(uuid.NAMESPACE_DNS, n)) for n in ["maya", "sam", "dev", "ana", "outsider"]}
NAMES = {IDS[n]: n.capitalize() for n in ["maya", "sam", "dev", "ana"]}
ROOM = frozenset(NAMES)


def item(n: int, sender: str, participants: list[str], body: str) -> ItemView:
    return ItemView(str(uuid.uuid5(uuid.NAMESPACE_DNS, f"i{n}")), IDS[sender],
                    frozenset(IDS[p] for p in participants), body)


def moment(key: str, members: list[str], its: list[ItemView]) -> MomentView:
    return MomentView(str(uuid.uuid5(uuid.NAMESPACE_DNS, key)), "moment", tuple(i.id for i in its),
                      frozenset(IDS[m] for m in members), np.eye(4)[0])


LISBON = [item(1, "maya", ["maya", "sam"], "fuck it im booking a one way ticket to lisbon"),
          item(2, "sam", ["maya", "sam"], "WAIT ur deadass going to lisbon alone??")]
FIRE = [item(3, "ana", list(NAMES_LOWER := ["maya", "sam", "dev", "ana"]), "WHO SET OFF THE FIRE ALARM"),
        item(4, "dev", NAMES_LOWER, "i genuinely did not hear a fire alarm")]
ITEMS = {i.id: i for i in LISBON + FIRE}


def test_prefers_the_moment_the_room_is_split_on():
    everyone_knows = moment("fire", NAMES_LOWER, FIRE)
    half_know = moment("lisbon", ["maya", "sam"], LISBON)
    pick = pick_moment([everyone_knows, half_know], ITEMS, ROOM, member_vectors={})
    assert pick.moment is half_know
    assert pick.p_known[IDS["maya"]] > 0.9 > pick.p_known[IDS["dev"]]


def test_split_score_peaks_when_half_know():
    assert split_score([0.9, 0.9, 0.1, 0.1]) > split_score([0.9, 0.9, 0.9, 0.9])


def test_never_uses_threads_with_people_outside_the_room():
    leaky = item(9, "maya", ["maya", "outsider"], "something the outsider said")
    m = moment("leaky", ["maya", "outsider"], [leaky, *LISBON])
    usable = eligible_items(m, {**ITEMS, leaky.id: leaky}, ROOM)
    assert leaky not in usable and len(usable) == 2


def test_who_sent_this_answer_comes_from_the_data_not_the_model(monkeypatch):
    async def fake(system, user):
        return {"item_id": LISBON[0].id, "reveal": "maya's spite era"}

    monkeypatch.setattr(rounds, "complete_json", fake)
    pick = pick_moment([moment("lisbon", ["maya", "sam"], LISBON)], ITEMS, ROOM, {})
    draft = asyncio.run(rounds.write_round(1, pick, NAMES))
    assert draft.game_type == GameType.WHO_SENT_THIS
    assert draft.answer == "Maya" and draft.written_by == "muse"
    assert draft.quote == LISBON[0].body
    assert str(draft.story_holder_id) == IDS["maya"]


@pytest.mark.parametrize("reply", [None, {"item_id": "made-up", "reveal": "x"}, {"item_id": "", "reveal": ""}])
def test_who_sent_this_falls_back_to_a_template(monkeypatch, reply):
    async def fake(system, user):
        return reply

    monkeypatch.setattr(rounds, "complete_json", fake)
    pick = pick_moment([moment("lisbon", ["maya", "sam"], LISBON)], ITEMS, ROOM, {})
    draft = asyncio.run(rounds.write_round(1, pick, NAMES))
    assert draft.written_by == "template"
    assert draft.answer in {"Maya", "Sam"}


def test_most_likely_to_rejects_prompts_that_name_people(monkeypatch):
    async def fake(system, user):
        return {"prompt": "who's most likely to be maya", "reveal": "lol"}

    monkeypatch.setattr(rounds, "complete_json", fake)
    pick = pick_moment([moment("lisbon", ["maya", "sam"], LISBON)], ITEMS, ROOM, {})
    draft = asyncio.run(rounds.write_round(2, pick, NAMES))
    assert draft.game_type == GameType.MOST_LIKELY_TO
    assert draft.written_by == "template" and draft.answer is None


def test_host_line_rules():
    names = list(NAMES.values())
    assert check_host_line("maya, context. now.", "Maya", names) is None
    assert check_host_line("wow fun!", "Maya", names) == "doesn't hand the turn to the target"
    assert check_host_line("maya and sam spill", "Maya", names) == "names someone other than the target"
    assert check_host_line("maya were you hungover", "Maya", names) == "sensitive topic"
    assert check_round_text("who's most likely to talk to their ex") == "sensitive topic"


def test_nudge_falls_back_when_muse_breaks_a_rule(monkeypatch):
    async def fake(system, user):
        return {"line": "sam you tell it"}

    monkeypatch.setattr(host, "complete_json", fake)
    line, by = asyncio.run(host.nudge_line("Maya", list(NAMES.values()), "who sent this?", "it was maya", []))
    assert by == "template" and "Maya" in line


def test_llm_json_parsing_tolerates_fences_and_chatter():
    from app.ai.llm import parse_json

    assert parse_json('{"line": "maya spill"}') == {"line": "maya spill"}
    assert parse_json('sure!\n```json\n{"line": "maya spill"}\n```') == {"line": "maya spill"}
    with pytest.raises(ValueError):
        parse_json("no json here")
