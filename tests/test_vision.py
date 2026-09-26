import asyncio
import json

import pytest

from app.ai import llm, rounds, vision
from app.ai.picker import ItemView, pick_moment
from tests.test_rounds import IDS, LISBON, NAMES, ROOM, moment

PHOTO_ID = "0b6c2f0e-6f7a-4c1e-9d59-2f4a1c9e8b10"


@pytest.mark.parametrize(
    "reply", [None, {}, {"description": "", "safe": True}, {"description": "x " * 40, "safe": True}]
)
def test_unusable_reads_are_rejected(reply):
    assert vision.validate(reply) is None


def test_safe_means_muse_said_so_and_the_description_agrees():
    assert vision.validate({"description": "a red rice cooker on a counter", "safe": True}).safe
    # Muse forgot to flag it, but its own description gives it away.
    flagged = vision.validate({"description": "two pints of beer on a table", "safe": True})
    assert not flagged.safe and flagged.reason
    # A landmark is not a religion round; only the round text itself is held to that rule.
    assert vision.validate({"description": "sunset over rooftops and a church", "safe": True}).safe
    assert not vision.validate(
        {"description": "a dog", "safe": "yes"}
    ).safe  # only a real true counts


def test_images_are_sent_as_image_parts(monkeypatch):
    sent = {}

    class FakeClient:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, headers, json):
            sent.update(json)

            class R:
                status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    return {
                        "choices": [
                            {"message": {"content": '{"description": "a dog", "safe": true}'}}
                        ]
                    }

            return R()

    monkeypatch.setattr(llm.httpx, "AsyncClient", FakeClient)
    model = llm.ChatModel("https://example.test/v1", "k", "m")
    asyncio.run(model.complete_json("sys", "look", images=["data:image/jpeg;base64,AAAA"]))
    parts = sent["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_who_sent_this_can_use_a_photo_with_no_caption(monkeypatch):
    photo = ItemView(
        PHOTO_ID,
        IDS["maya"],
        LISBON[0].participant_ids,
        "",
        "photo",
        "https://example.test/natas.jpg",
        "a tray of custard tarts in a bakery window",
        "someone · CC BY",
    )
    seen = {}

    async def fake(system, user):
        seen.update(json.loads(user))
        return {"item_id": photo.id, "reveal": "maya's pastry era"}

    monkeypatch.setattr(rounds, "complete_json", fake)
    pick = pick_moment(
        [moment("lisbon", ["maya", "sam"], [photo, *LISBON])],
        {i.id: i for i in [photo, *LISBON]},
        ROOM,
        {},
    )
    draft = asyncio.run(rounds.write_round(1, pick, NAMES))
    assert {"item_id": PHOTO_ID, "text": None, "photo": photo.media_description} in seen["messages"]
    assert (
        draft.quote is None
        and draft.media_url == photo.media_url
        and draft.media_credit == photo.media_credit
    )
    assert draft.answer == "Maya"


def test_most_likely_to_shows_the_photo_muse_matched(monkeypatch):
    photo = ItemView(
        PHOTO_ID,
        IDS["maya"],
        LISBON[0].participant_ids,
        "",
        "photo",
        "https://example.test/natas.jpg",
        "a tray of custard tarts",
        "someone · CC BY",
    )
    replies = iter(
        [
            {
                "prompt": "who's most likely to eat six in one sitting?",
                "reveal": "we know",
                "photo_id": PHOTO_ID,
            },
            {
                "prompt": "who's most likely to eat six in one sitting?",
                "reveal": "we know",
                "photo_id": "made-up",
            },
        ]
    )

    async def fake(system, user):
        return next(replies)

    monkeypatch.setattr(rounds, "complete_json", fake)
    pick = pick_moment(
        [moment("lisbon", ["maya", "sam"], [photo, *LISBON])],
        {i.id: i for i in [photo, *LISBON]},
        ROOM,
        {},
    )
    matched = asyncio.run(rounds.most_likely_to(pick, NAMES, __import__("random").Random(0)))
    assert matched.media_url == photo.media_url and matched.media_credit == photo.media_credit
    invented = asyncio.run(rounds.most_likely_to(pick, NAMES, __import__("random").Random(0)))
    assert invented.media_url is None
