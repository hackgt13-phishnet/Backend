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
    WHO_SENT_THIS = "who_sent_this"
    MOST_LIKELY_TO = "most_likely_to"


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


class SubmitResponseRequest(Command):
    value: UUID


class RoundDraft(BaseModel):
    game_type: GameType
    prompt: str
    options: list[str] = Field(min_length=2, max_length=6)
    answer: str
    source_item_ids: list[UUID] = Field(min_length=1, max_length=8)
    reveal_copy: str


class GeneratedSession(BaseModel):
    rounds: list[RoundDraft] = Field(min_length=3, max_length=3)


class RoomSummary(BaseModel):
    id: UUID
    name: str
    join_code: str
    host_profile_id: UUID
    created_at: datetime


class RoundOption(BaseModel):
    profile_id: UUID
    label: str


class PublicMedia(BaseModel):
    url: str | None = None
    asset_key: str | None = None
    caption: str | None = None


class PublicResult(BaseModel):
    profile_id: UUID
    correct: bool
    points: int


class PublicReveal(BaseModel):
    correct_profile_id: UUID
    message: str
    results: list[PublicResult]


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
    required_response_count: int
    submitted_profile_ids: list[UUID]
    reveal: PublicReveal | LegacyReveal | None
    revision: int

    @model_validator(mode="after")
    def check_reveal(self):
        if self.phase in (RoundPhase.PENDING, RoundPhase.ANSWERING) and self.reveal is not None:
            raise ValueError("Unrevealed rounds cannot contain a reveal")
        return self
