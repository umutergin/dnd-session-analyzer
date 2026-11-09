import asyncio
import shutil
import threading
import time
import wave
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


class DiskWaveSink(sinks.Sink):
    """Streams each speaker's audio to disk, time-aligned to the start of the recording.

    - Discord sends no packets while someone is silent and this py-cord doesn't pad the gaps,
      so a speaker's raw stream collapses to just their speech. We insert silence based on
      wall-clock arrival time, so every speaker's WAV is full-length and the tracks line up
      (like Craig's), which the transcript merge relies on.
    - Audio goes straight to disk instead of an in-memory buffer (~0.7 GB/hour/speaker).
    - Keyed by numeric user ID (py-cord now passes Member objects, or None for streams it
      hasn't mapped to a user yet; those are dropped).
    - Files are finalized in cleanup(): in this py-cord the 'after' callback runs before it.
    """

    BYTES_PER_SECOND = SAMPLE_RATE * BYTES_PER_SAMPLE * CHANNELS
    FRAME_BYTES = BYTES_PER_SECOND // 50  # 20 ms
    GAP_THRESHOLD_BYTES = BYTES_PER_SECOND // 10  # ignore gaps/jitter under 100 ms
    ZEROS_CHUNK = bytes(1024 * 1024)

    def __init__(self, output_dir: Path):
        super().__init__()
        self.output_dir = output_dir
        self.saved: dict[int, Path] = {}
        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        self._streams: dict[int, list] = {}  # user_id -> [file, bytes_written]

    @property
    def speaker_count(self) -> int:
        return len(self._streams)

    def bytes_written(self) -> dict[int, int]:
        with self._lock:
            return {uid: st[1] for uid, st in self._streams.items()}

    def _elapsed_bytes(self) -> int:
        elapsed = int((time.monotonic() - self._t0) * self.BYTES_PER_SECOND)
        return elapsed - (elapsed % self.FRAME_BYTES)

    def _pad_to(self, stream: list, target: int) -> None:
        missing = target - stream[1]
        while missing > 0:
            chunk = self.ZEROS_CHUNK[: min(missing, len(self.ZEROS_CHUNK))]
            stream[0].write(chunk)
            stream[1] += len(chunk)
            missing -= len(chunk)

    def write(self, data, user) -> None:
        if user is None or getattr(self, "finished", False):
            return
        pcm = getattr(data, "pcm", data)
        uid = getattr(user, "id", user)
        with self._lock:
            stream = self._streams.get(uid)
            if stream is None:
                stream = [open(self.output_dir / f"speaker_{uid}.pcm", "wb"), 0]
                self._streams[uid] = stream
            # The packet covers the 20 ms ending now; insert silence for any real gap before it
            start = self._elapsed_bytes() - len(pcm)
            if start - stream[1] >= self.GAP_THRESHOLD_BYTES:
                self._pad_to(stream, start)
            stream[0].write(pcm)
            stream[1] += len(pcm)

    def cleanup(self) -> None:
        self.finished = True
        with self._lock:
            total = self._elapsed_bytes()
            for uid, stream in self._streams.items():
                self._pad_to(stream, total)  # every track ends at the same time
                stream[0].close()
                pcm_path = self.output_dir / f"speaker_{uid}.pcm"
                wav_path = self.output_dir / f"speaker_{uid}.wav"
                with open(pcm_path, "rb") as src, wave.open(str(wav_path), "wb") as dst:
                    dst.setnchannels(CHANNELS)
                    dst.setsampwidth(BYTES_PER_SAMPLE)
                    dst.setframerate(SAMPLE_RATE)
                    while chunk := src.read(1024 * 1024):
                        dst.writeframes(chunk)
                pcm_path.unlink()
                self.saved[uid] = wav_path
                logger.info("Saved speaker audio", user_id=uid, file_path=str(wav_path))


@dataclass
class RecordingSession:
    """Represents an active recording session."""

    session_id: str
    guild_id: int
    channel_id: int
    started_at: datetime
    output_dir: Path
    voice_client: Optional[discord.VoiceClient] = None
    sink: Optional[DiskWaveSink] = None
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
            sizes = session.sink.bytes_written()
            grew = {uid: n - last.get(uid, 0) for uid, n in sizes.items()}
            logger.info(
                "Recording heartbeat",
                session_id=session.session_id,
                connected=bool(session.voice_client and session.voice_client.is_connected()),
                receiving=any(g > 0 for g in grew.values()),
                kb_last_minute={uid: g // 1024 for uid, g in grew.items()},
            )
            last = sizes

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
            session.voice_client.start_recording(session.sink)
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
            "speaker_count": session.sink.speaker_count if session.sink else 0,
        }

    def is_recording(self, guild_id: int) -> bool:
        """Check if currently recording in a guild."""
        return guild_id in self.active_sessions
