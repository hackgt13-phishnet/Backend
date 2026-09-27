from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoundPhase(StrEnum):
    PENDING = "pending"
    ANSWERING = "answering"
    REVEALED = "revealed"
    COMPLETE = "complete"


class GameType(StrEnum):
    WHO_SENT_THIS = "who_sent_this"  # guessing: needs a moment only some players know
    MOST_LIKELY_TO = "most_likely_to"  # vote: needs a moment everyone knows
    HOT_TAKE = "hot_take"  # agree/disagree: from a clash or strong overlap in interests
    THIS_OR_THAT = "this_or_that"  # pick a side: from an overlap in interests, or general


class TimelineType(StrEnum):
    MESSAGE = "message"
    GAME_STARTED = "game_started"
    GAME_PROMPT = "game_prompt"
    SUBMISSION_STATUS = "submission_status"
    GAME_REVEAL = "game_reveal"


class DemoSessionRequest(Command):
    profile_id: UUID


class CreateRoomRequest(Command):
    name: str = Field(min_length=1, max_length=80)


class JoinRoomRequest(Command):
    code: str = Field(min_length=6, max_length=8)


class MessageRequest(Command):
    body: str = Field(min_length=1, max_length=1_000)


class StartSessionRequest(Command):
    vibe: Literal["chaos"] = "chaos"
    game_type: Literal["who_sent_this"] = "who_sent_this"


class ThreadRoomRequest(Command):
    name: str = Field(default="Group chat", min_length=1, max_length=80)


class SendGameRequest(StartSessionRequest):
    name: str = Field(default="Group chat", min_length=1, max_length=80)
    # async: everyone plays on their own time (GamePigeon). live: timed, game-master paced.
    mode: Literal["async", "live"] = "async"


class SubmitResponseRequest(Command):
    value: str = Field(min_length=1, max_length=64)  # an option id
    why: str | None = Field(default=None, max_length=140)  # required in opinion rounds


class MaterialChoice(Command):
    excluded: bool  # true = the game may not use this item


class RoundDraft(BaseModel):
    game_type: GameType
    prompt: str = Field(min_length=1, max_length=240)
    quote: str | None = Field(default=None, max_length=400)  # the item shown in Who Sent This?
    source_content_type: str = "message"
    media_url: str | None = None
    media_credit: str | None = None  # shown small under a photo (demo photos are openly licensed)
    options: list[str] = Field(min_length=2, max_length=6)
    answer: str | None  # None for vote games (Most Likely To)
    source_item_ids: list[UUID] = Field(min_length=1, max_length=8)
    reveal_copy: str = Field(min_length=1, max_length=240)
    moment_id: UUID | None = None
    source: str = (
        "moment"  # "moment" (shared history), "interest" (players' own activity) or "general"
    )
    branch: str | None = None  # which flowchart branch planned this session
    story_holder_id: UUID | None = None
    written_by: str = "muse"  # "muse" or "template" when every model failed


ROUNDS_PER_SESSION = 3


class RoomSummary(BaseModel):
    id: UUID
    name: str
    join_code: str
    host_profile_id: UUID
    created_at: datetime


class RoundOption(BaseModel):
    id: str
    label: str
    profile_id: UUID | None = None  # set when the option is a player

    @model_validator(mode="before")
    @classmethod
    def default_id(cls, data):
        # Fixture rounds predate option ids; their options are players.
        if isinstance(data, dict) and "id" not in data and data.get("profile_id"):
            return {**data, "id": str(data["profile_id"])}
        return data


class PublicMedia(BaseModel):
    url: str | None = None
    asset_key: str | None = None
    caption: str | None = None
    quote: str | None = None  # the message shown in Who Sent This?
    type: str | None = None


class PublicResult(BaseModel):
    profile_id: UUID
    correct: bool
    points: int
    choice: str | None = None
    why: str | None = None


class PublicReveal(BaseModel):
    correct_profile_id: UUID | None = None  # guessing rounds
    message: str
    results: list[PublicResult]
    winner_profile_id: UUID | None = (
        None  # opinion rounds: the answer the AI judged most interesting
    )
    shoutout: str | None = None


class LegacyReveal(BaseModel):
    """Preserves already-revealed data copied by the follow-up migration."""

    answer: JsonValue
    message: str
    results: list[PublicResult]


class PublicRound(BaseModel):
    id: UUID
    room_id: UUID
    session_id: UUID
    ordinal: int
    game_type: GameType
    phase: RoundPhase
    prompt: str
    media: PublicMedia
    options: list[RoundOption]
    player_profile_ids: list[UUID] = []
    required_response_count: int
    submitted_profile_ids: list[UUID]
    reveal: PublicReveal | LegacyReveal | None
    revision: int

    @model_validator(mode="after")
    def check_reveal(self):
        if self.phase in (RoundPhase.PENDING, RoundPhase.ANSWERING) and self.reveal is not None:
            raise ValueError("Unrevealed rounds cannot contain a reveal")
        return self
