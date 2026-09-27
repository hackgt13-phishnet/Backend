import asyncio
import json

from app.ai import rounds, vision
from app.ai.interests import post_text
from app.ai.picker import ItemView, pick_moment
from tests.test_rounds import IDS, LISBON, NAMES, ROOM, moment

BEACH = {
    "place": "beach at sunset",
    "activity": "hanging out",
    "people": 3,
    "summary": "friends on a beach at sunset",
}
SELFIE = {"place": "", "activity": "", "people": 1, "summary": "a person smiling at the camera"}


def test_vague_posts_lean_on_the_photo_and_location_or_are_skipped():
    assert (
        post_text("✨", BEACH, "Tybee Island")
        == "✨ [photo: friends on a beach at sunset] [at Tybee Island]"
    )
    assert (
        post_text("", BEACH, None) == "[photo: friends on a beach at sunset]"
    )  # no caption: the photo carries it
    assert post_text(None, SELFIE, None) == ""  # says nothing specific: skipped
    assert post_text("✨", SELFIE, None) == "✨"  # only the caption, which alone backs no interest
    assert post_text("✨", {**SELFIE, "activity": "posing"}, None) == "✨"  # "posing" says nothing


def test_post_reads_count_people_and_never_pass_unsafe_scenes():
    read = vision.validate_post({**BEACH, "safe": True})
    assert read.shows_person and read.specific and read.safe
    assert not vision.validate_post({**SELFIE, "safe": True}).specific
    assert not vision.validate_post(
        {"summary": "friends doing shots at a bar", "people": 4, "safe": True}
    ).safe


def test_who_sent_this_never_uses_a_photo_that_shows_someone(monkeypatch):
    posed = ItemView(
        "0b6c2f0e-6f7a-4c1e-9d59-2f4a1c9e8b11",
        IDS["maya"],
        LISBON[0].participant_ids,
        "",
        "post",
        "https://example.test/beach.jpg",
        "friends on a beach",
        "x · Unsplash",
        True,
    )
    seen = {}

    async def fake(system, user):
        seen.update(json.loads(user))
        return {"item_id": posed.id, "reveal": "it was maya"}  # the model even asks for it

    monkeypatch.setattr(rounds, "complete_json", fake)
    pick = pick_moment(
        [moment("beach", ["maya", "sam"], [posed, *LISBON])],
        {i.id: i for i in [posed, *LISBON]},
        ROOM,
        {},
    )
    draft = asyncio.run(rounds.write_round(1, pick, NAMES))
    assert posed.id not in {m["item_id"] for m in seen["messages"]}
    assert draft.media_url is None and str(draft.source_item_ids[0]) != posed.id
