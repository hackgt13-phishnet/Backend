from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


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


class DemoSessionRequest(BaseModel):
    profile_id: UUID


class CreateRoomRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class JoinRoomRequest(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class MessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=1_000)


class StartSessionRequest(BaseModel):
    vibe: str = "chaos"


class SubmitResponseRequest(BaseModel):
    value: str = Field(min_length=1, max_length=2_000)


class RoundDraft(BaseModel):
    game_type: GameType
    prompt: str = Field(min_length=1, max_length=240)
    quote: str | None = Field(default=None, max_length=400)  # the item shown in Who Sent This?
    source_content_type: str = "message"
    media_url: str | None = None
    options: list[str] = Field(min_length=2, max_length=6)
    answer: str | None  # None for vote games (Most Likely To)
    source_item_ids: list[UUID] = Field(min_length=1, max_length=8)
    reveal_copy: str = Field(min_length=1, max_length=240)
    moment_id: UUID
    story_holder_id: UUID | None = None
    written_by: str = "muse"  # "muse" or "template" when every model failed


ROUNDS_PER_SESSION = 3


class RoomSummary(BaseModel):
    id: UUID
    name: str
    join_code: str
    host_profile_id: UUID
    created_at: datetime
