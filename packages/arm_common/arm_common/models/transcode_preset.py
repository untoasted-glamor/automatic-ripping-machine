from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Column, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from arm_common.models._columns import created_at_column, enum_column, updated_at_column
from arm_common.enums import (
    AudioSelection,
    ContainerFormat,
    MediaType,
    SubtitleOutput,
    SubtitleSelection,
    TranscodeTool,
)
from arm_common.ulid import new_id


def _transcode_preset_id() -> str:
    return new_id("tpr")


class TranscodePreset(SQLModel, table=True):
    __tablename__ = "transcode_presets"

    id: str = Field(default_factory=_transcode_preset_id, primary_key=True)
    name: str = Field(sa_column=Column(String, nullable=False))
    media_type: MediaType = Field(sa_column=enum_column(MediaType, "media_type"))
    is_builtin: bool = Field(sa_column=Column(Boolean, nullable=False, server_default="false"))
    tool: TranscodeTool = Field(sa_column=enum_column(TranscodeTool, "transcode_tool"))
    preset_ref: str | None = Field(default=None)
    preset_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    container: ContainerFormat = Field(sa_column=enum_column(ContainerFormat, "container_format"))
    # Catalog id (arm_common.encoders): what to encode with; replaces codec +
    # hw_preference. Validated in the app layer, stored as VARCHAR.
    encoder: str = Field(default="preset", sa_column=Column(String, nullable=False, server_default="preset"))
    extra_args: str | None = Field(default=None)
    # Track selection (validated in the app layer, stored as VARCHAR/JSONB).
    # The `preset` modes leave the choice to the engine; the language lists
    # are ordered ISO 639-2 codes (arm_common.languages), output order.
    audio_selection: AudioSelection = Field(
        default=AudioSelection.PRESET,
        sa_column=enum_column(AudioSelection, "audio_selection", server_default=AudioSelection.PRESET.value),
    )
    audio_languages: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False, server_default="[]")
    )
    subtitle_selection: SubtitleSelection = Field(
        default=SubtitleSelection.PRESET,
        sa_column=enum_column(SubtitleSelection, "subtitle_selection", server_default=SubtitleSelection.PRESET.value),
    )
    subtitle_languages: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False, server_default="[]")
    )
    subtitle_output: SubtitleOutput = Field(
        default=SubtitleOutput.EMBEDDED,
        sa_column=enum_column(SubtitleOutput, "subtitle_output", server_default=SubtitleOutput.EMBEDDED.value),
    )
    created_by_user_id: str | None = Field(
        sa_column=Column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    )
    created_at: datetime | None = Field(sa_column=created_at_column())
    updated_at: datetime | None = Field(sa_column=updated_at_column())
