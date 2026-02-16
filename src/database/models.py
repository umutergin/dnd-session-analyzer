import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import (
    String,
    Text,
    Integer,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    JSON,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from src.database.connection import Base


class Campaign(Base):
    """Represents a D&D campaign that groups multiple sessions."""

    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    sessions: Mapped[list["Session"]] = relationship(back_populates="campaign")

    __table_args__ = (Index("ix_campaigns_guild_name", "guild_id", "name", unique=True),)


class Session(Base):
    """Represents a single D&D recording session."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # Voice channel
    notification_channel_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # Text channel for notifications
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    session_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Recording metadata
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Processing status
    status: Mapped[str] = mapped_column(
        String(50), default="recording", index=True
    )  # recording, processing, transcribing, analyzing, completed, failed
    processing_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    processing_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # File references
    audio_directory: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    merged_audio_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Cost tracking (in cents)
    transcription_cost_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    llm_cost_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    campaign: Mapped[Optional["Campaign"]] = relationship(back_populates="sessions")
    audio_tracks: Mapped[list["SessionAudioTrack"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    transcript: Mapped[Optional["Transcript"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", uselist=False
    )
    summary: Mapped[Optional["SessionSummary"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", uselist=False
    )


class SessionAudioTrack(Base):
    """Represents a per-speaker audio file from a session."""

    __tablename__ = "session_audio_tracks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_username: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    session: Mapped["Session"] = relationship(back_populates="audio_tracks")


class Transcript(Base):
    """Stores the transcription of a session."""

    __tablename__ = "transcripts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    # AssemblyAI metadata
    assemblyai_transcript_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Full transcript text (for search)
    full_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Structured utterances with speaker labels
    # Array of {speaker: str, text: str, start_ms: int, end_ms: int, confidence: float}
    utterances: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Processing metadata
    language: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    audio_duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    confidence_average: Mapped[Optional[float]] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    session: Mapped["Session"] = relationship(back_populates="transcript")


class SessionSummary(Base):
    """Stores AI-generated session summaries."""

    __tablename__ = "session_summaries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    # Summary content
    short_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    detailed_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    key_events: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    combat_encounters: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Extracted entities (for MVP - full entities come later)
    npcs_mentioned: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    locations_mentioned: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # LLM metadata
    model_used: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    session: Mapped["Session"] = relationship(back_populates="summary")


class CharacterMapping(Base):
    """Maps Discord users to their in-game character names."""

    __tablename__ = "character_mappings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    character_name: Mapped[str] = mapped_column(String(255), nullable=False)
    character_class: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    character_race: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_dm: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_character_mappings_campaign_user", "campaign_id", "discord_user_id", unique=True),
    )
