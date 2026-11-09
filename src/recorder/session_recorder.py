import asyncio
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field
import discord
from discord import sinks
import structlog

logger = structlog.get_logger()

# EXPERIMENT (py-cord PR #3159 / issue #3388): UDPKeepAlive.delay is 5000 but is passed to
# time.sleep(), i.e. seconds, so after the first packet no keepalive is sent for ~83 minutes
# and Discord stops delivering voice. Send one every 5 seconds instead.
try:
    from discord.voice.receive import reader as _voice_reader

    _voice_reader.UDPKeepAlive.delay = 5
except Exception:  # older py-cord without this module
    pass

# Audio storage constants for disk space calculation
SAMPLE_RATE = 48000  # 48kHz
BYTES_PER_SAMPLE = 2  # 16-bit audio
CHANNELS = 2  # Stereo from Discord
SECONDS_PER_HOUR = 3600
DISK_BUFFER_MULTIPLIER = 1.5  # 50% buffer for safety


class InsufficientDiskSpaceError(Exception):
    """Raised when there's not enough disk space for recording."""
    pass


class DiskWaveSink(sinks.WaveSink):
    """WaveSink that saves each speaker's WAV when recording is finalized.

    In the DAVE-capable py-cord the 'after' callback runs *before* the sink is cleaned up
    (which is what converts raw PCM to WAV), so persist from cleanup() itself.
    """

    def __init__(self, output_dir: Path):
        super().__init__()
        self.output_dir = output_dir
        self.saved: dict[int, Path] = {}

    def cleanup(self):
        super().cleanup()  # marks finished and formats each speaker's PCM as WAV
        for user_id, audio in self.audio_data.items():
            path = self.output_dir / f"speaker_{user_id}.wav"
            audio.file.seek(0)
            path.write_bytes(audio.file.read())
            self.saved[user_id] = path
            logger.info("Saved speaker audio", user_id=user_id, file_path=str(path))


@dataclass
class RecordingSession:
    """Represents an active recording session."""

    session_id: str
    guild_id: int
    channel_id: int
    started_at: datetime
    output_dir: Path
    voice_client: Optional[discord.VoiceClient] = None
    sink: Optional[sinks.WaveSink] = None
    is_paused: bool = False
    speaker_files: dict[int, Path] = field(default_factory=dict)
    monitor: Optional[asyncio.Task] = None


class SessionRecorder:
    """Manages voice recording sessions for D&D games."""

    def __init__(self, audio_base_path: Path):
        self.audio_base_path = audio_base_path
        self.active_sessions: dict[int, RecordingSession] = {}  # guild_id -> session

    def _estimate_required_disk_space(
        self,
        max_speakers: int = 6,
        max_duration_hours: float = 4.0,
    ) -> int:
        """
        Estimate required disk space for a recording session.

        Args:
            max_speakers: Maximum expected speakers (default 6 for typical D&D party + DM)
            max_duration_hours: Maximum expected session duration in hours

        Returns:
            Required disk space in bytes
        """
        # Per-speaker file size: sample_rate * bytes_per_sample * channels * seconds
        bytes_per_speaker = (
            SAMPLE_RATE * BYTES_PER_SAMPLE * CHANNELS *
            int(max_duration_hours * SECONDS_PER_HOUR)
        )
        # Total for all speakers plus merged file
        total_bytes = bytes_per_speaker * (max_speakers + 1)  # +1 for merged
        # Add buffer
        return int(total_bytes * DISK_BUFFER_MULTIPLIER)

    def _check_disk_space(self, required_bytes: int) -> None:
        """
        Check if there's enough disk space for recording.

        Args:
            required_bytes: Required disk space in bytes

        Raises:
            InsufficientDiskSpaceError: If not enough disk space
        """
        # Ensure base path exists
        self.audio_base_path.mkdir(parents=True, exist_ok=True)

        disk_usage = shutil.disk_usage(self.audio_base_path)
        free_bytes = disk_usage.free

        if free_bytes < required_bytes:
            required_gb = required_bytes / (1024 ** 3)
            free_gb = free_bytes / (1024 ** 3)
            raise InsufficientDiskSpaceError(
                f"Insufficient disk space for recording. "
                f"Required: {required_gb:.1f} GB, Available: {free_gb:.1f} GB. "
                f"Please free up space before starting a recording."
            )

        logger.info(
            "Disk space check passed",
            required_gb=required_bytes / (1024 ** 3),
            available_gb=free_bytes / (1024 ** 3),
        )

    def _generate_session_id(self) -> str:
        """Generate a unique session ID based on timestamp."""
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def _get_session_dir(self, session_id: str) -> Path:
        """Get the directory path for a session's audio files."""
        session_dir = self.audio_base_path / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir

    async def start_recording(
        self,
        voice_channel: discord.VoiceChannel,
        session_name: Optional[str] = None,
    ) -> RecordingSession:
        """
        Start recording a voice channel.

        Args:
            voice_channel: The Discord voice channel to record
            session_name: Optional custom name for the session

        Returns:
            RecordingSession object with session details

        Raises:
            ValueError: If already recording in this guild
        """
        guild_id = voice_channel.guild.id

        if guild_id in self.active_sessions:
            raise ValueError("Already recording in this server. Use /dnd stop first.")

        # Check disk space before starting
        # Estimate based on voice channel members or default to 6
        expected_speakers = max(len(voice_channel.members), 6)
        required_space = self._estimate_required_disk_space(
            max_speakers=expected_speakers,
            max_duration_hours=4.0,  # Plan for 4-hour sessions
        )
        self._check_disk_space(required_space)

        session_id = session_name or self._generate_session_id()
        output_dir = self._get_session_dir(session_id)

        logger.info(
            "Starting recording session",
            session_id=session_id,
            guild_id=guild_id,
            channel=voice_channel.name,
        )

        # Connect to voice channel
        voice_client = await voice_channel.connect()

        # Create WAV sink for recording
        sink = DiskWaveSink(output_dir)

        # Create session object
        session = RecordingSession(
            session_id=session_id,
            guild_id=guild_id,
            channel_id=voice_channel.id,
            started_at=datetime.now(),
            output_dir=output_dir,
            voice_client=voice_client,
            sink=sink,
        )

        # Store active session
        self.active_sessions[guild_id] = session

        # Start recording with callback
        voice_client.start_recording(sink)
        session.monitor = asyncio.create_task(self._monitor(session))

        logger.info("Recording started", session_id=session_id)
        return session

    async def _monitor(self, session: RecordingSession, interval: int = 60):
        """Log bytes received per speaker every minute so a receive stall is visible."""
        last: dict[int, int] = {}
        while True:
            await asyncio.sleep(interval)
            sizes = {
                uid: audio.file.getbuffer().nbytes
                for uid, audio in list(session.sink.audio_data.items())
            }
            grew = {uid: n - last.get(uid, 0) for uid, n in sizes.items()}
            logger.info(
                "Recording heartbeat",
                session_id=session.session_id,
                connected=bool(session.voice_client and session.voice_client.is_connected()),
                receiving=any(g > 0 for g in grew.values()),
                kb_last_minute={uid: g // 1024 for uid, g in grew.items()},
            )
            last = sizes

    async def _on_recording_stopped(
        self,
        sink: sinks.WaveSink,
        session: RecordingSession,
    ):
        """Callback when recording stops - save audio files."""
        logger.info("Processing recorded audio", session_id=session.session_id)

        for user_id, audio in sink.audio_data.items():
            file_path = session.output_dir / f"speaker_{user_id}.wav"

            # Write audio data to file
            with open(file_path, "wb") as f:
                audio.file.seek(0)
                f.write(audio.file.read())

            session.speaker_files[user_id] = file_path
            logger.info(
                "Saved speaker audio",
                session_id=session.session_id,
                user_id=user_id,
                file_path=str(file_path),
            )

    async def stop_recording(self, guild_id: int) -> RecordingSession:
        """
        Stop recording for a guild.

        Args:
            guild_id: The guild ID to stop recording for

        Returns:
            The completed RecordingSession

        Raises:
            ValueError: If not currently recording in this guild
        """
        if guild_id not in self.active_sessions:
            raise ValueError("Not currently recording in this server.")

        session = self.active_sessions[guild_id]

        logger.info("Stopping recording", session_id=session.session_id)

        # Stop recording (triggers callback)
        if session.monitor:
            session.monitor.cancel()
        if session.voice_client and session.voice_client.is_connected():
            # Synchronous: finalizes the sink, which writes the speaker files
            session.voice_client.stop_recording()
            session.speaker_files = dict(session.sink.saved)
            await session.voice_client.disconnect()

        # Remove from active sessions
        del self.active_sessions[guild_id]

        # Wait a moment for files to be written
        await asyncio.sleep(1)

        logger.info(
            "Recording stopped",
            session_id=session.session_id,
            speaker_count=len(session.speaker_files),
        )

        return session

    async def pause_recording(self, guild_id: int) -> bool:
        """Pause recording for a guild."""
        if guild_id not in self.active_sessions:
            raise ValueError("Not currently recording in this server.")

        session = self.active_sessions[guild_id]
        if session.is_paused:
            raise ValueError("Recording is already paused.")

        if session.voice_client:
            session.voice_client.stop_recording()
            session.is_paused = True
            logger.info("Recording paused", session_id=session.session_id)
            return True

        return False

    async def resume_recording(self, guild_id: int) -> bool:
        """Resume a paused recording."""
        if guild_id not in self.active_sessions:
            raise ValueError("Not currently recording in this server.")

        session = self.active_sessions[guild_id]
        if not session.is_paused:
            raise ValueError("Recording is not paused.")

        if session.voice_client and session.sink:
            session.voice_client.start_recording(
                session.sink,
                self._on_recording_stopped,
                session,
            )
            session.is_paused = False
            logger.info("Recording resumed", session_id=session.session_id)
            return True

        return False

    def get_session_status(self, guild_id: int) -> Optional[dict]:
        """Get the current recording status for a guild."""
        if guild_id not in self.active_sessions:
            return None

        session = self.active_sessions[guild_id]
        duration = datetime.now() - session.started_at

        return {
            "session_id": session.session_id,
            "channel_id": session.channel_id,
            "started_at": session.started_at.isoformat(),
            "duration_seconds": int(duration.total_seconds()),
            "is_paused": session.is_paused,
            "speaker_count": len(session.sink.audio_data) if session.sink else 0,
        }

    def is_recording(self, guild_id: int) -> bool:
        """Check if currently recording in a guild."""
        return guild_id in self.active_sessions
