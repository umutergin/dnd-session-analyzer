import assemblyai as aai
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
import structlog

from src.config import settings
from src.services.dnd_vocabulary import DND_VOCABULARY

logger = structlog.get_logger()

# Configure AssemblyAI
aai.settings.api_key = settings.assemblyai_api_key


# Files bigger than this are compressed before upload. Raw 48 kHz stereo WAV from the live
# recorder is ~1.9 GB per speaker for a 2.5 h session, and uploads of that size drop mid-way.
COMPRESS_ABOVE_BYTES = 100 * 1024 * 1024


def compress_for_upload(path: Path) -> Optional[Path]:
    """Mono 16 kHz FLAC copy of a large file (speech recognition doesn't need more).

    Returns the temp file path, or None if the file is small enough / ffmpeg failed
    (callers then upload the original). AssemblyAI bills by duration, so this is free.
    """
    if path.stat().st_size <= COMPRESS_ABOVE_BYTES:
        return None
    fd, tmp = tempfile.mkstemp(suffix=".flac")
    import os
    os.close(fd)
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
         "-ac", "1", "-ar", "16000", "-c:a", "flac", tmp],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.warning("Audio compression failed; uploading original", error=result.stderr[-300:])
        Path(tmp).unlink(missing_ok=True)
        return None
    logger.info("Compressed audio for upload", original_mb=path.stat().st_size // 2**20,
                compressed_mb=Path(tmp).stat().st_size // 2**20)
    return Path(tmp)


# Split a single speaker's word stream into utterances at pauses, so utterances from
# different speakers can be interleaved by time.
PAUSE_SPLIT_MS = 1500
MAX_UTTERANCE_MS = 30_000


def build_utterances(transcript) -> list[dict]:
    """
    Utterances (speaker, text, start_ms, end_ms, confidence) for a finished transcript.

    With diarization AssemblyAI returns utterances directly. For single-speaker tracks it
    returns none, so build them from word timestamps instead of collapsing the whole
    track into one block.
    """
    if transcript.utterances:
        return [
            {
                "speaker": u.speaker,
                "text": u.text,
                "start_ms": u.start,
                "end_ms": u.end,
                "confidence": u.confidence,
            }
            for u in transcript.utterances
        ]

    utterances: list[dict] = []
    current: list = []

    def flush():
        if current:
            utterances.append({
                "speaker": None,
                "text": " ".join(w.text for w in current),
                "start_ms": current[0].start,
                "end_ms": current[-1].end,
                "confidence": sum(w.confidence for w in current) / len(current),
            })
            current.clear()

    for word in transcript.words or []:
        if current:
            gap = word.start - current[-1].end
            too_long = word.end - current[0].start > MAX_UTTERANCE_MS
            sentence_end = current[-1].text.rstrip()[-1:] in ".?!"
            if gap >= PAUSE_SPLIT_MS or (too_long and sentence_end):
                flush()
        current.append(word)
    flush()
    return utterances


class TranscriptionResult:
    """Wrapper for transcription results."""

    def __init__(
        self,
        transcript_id: str,
        text: str,
        utterances: list[dict],
        audio_duration_seconds: int,
        confidence: float,
        language: str,
    ):
        self.transcript_id = transcript_id
        self.text = text
        self.utterances = utterances
        self.audio_duration_seconds = audio_duration_seconds
        self.confidence = confidence
        self.language = language


class AssemblyAIService:
    """Service for transcribing audio using AssemblyAI."""

    def __init__(
        self,
        language_code: str = "tr",
        use_vocabulary_boost: bool = True,
    ):
        """
        Initialize the AssemblyAI service.

        Args:
            language_code: Primary language code (default: "tr" for Turkish)
            use_vocabulary_boost: Whether to boost D&D terminology recognition
        """
        self.transcriber = aai.Transcriber()
        self.language_code = language_code
        self.use_vocabulary_boost = use_vocabulary_boost

    def transcribe_file(
        self,
        audio_path: Path,
        speaker_labels: bool = True,
        speakers_expected: Optional[int] = None,
        language_code: Optional[str] = None,
    ) -> TranscriptionResult:
        """
        Transcribe an audio file with speaker diarization.

        Args:
            audio_path: Path to the audio file
            speaker_labels: Whether to enable speaker diarization
            speakers_expected: Expected number of speakers (improves accuracy)
            language_code: Override the default language code

        Returns:
            TranscriptionResult with text and speaker-labeled utterances
        """
        lang = language_code or self.language_code

        logger.info(
            "Starting transcription",
            audio_path=str(audio_path),
            speaker_labels=speaker_labels,
            speakers_expected=speakers_expected,
            language_code=lang,
            vocabulary_boost=self.use_vocabulary_boost,
        )

        # Configure transcription with speaker diarization
        # Using both universal models for multilingual support (Turkish + English)
        config_kwargs = {
            "speech_models": ["universal-3-pro", "universal-2"],
            "speaker_labels": speaker_labels,
            "speakers_expected": speakers_expected,
            "punctuate": True,
            "format_text": True,
        }

        # Add keyterms prompt for D&D terms if enabled
        # This helps recognize English D&D terminology in speech
        if self.use_vocabulary_boost:
            # Create a prompt with key D&D terms for better recognition
            # Limit to most important terms (keyterms_prompt has limits)
            key_terms = DND_VOCABULARY[:100]  # Use top 100 terms
            config_kwargs["keyterms_prompt"] = key_terms
            logger.info(
                "Vocabulary boost enabled via keyterms_prompt",
                term_count=len(key_terms),
            )

        config = aai.TranscriptionConfig(**config_kwargs)

        # Transcribe
        compressed = compress_for_upload(Path(audio_path))
        try:
            transcript = self.transcriber.transcribe(str(compressed or audio_path), config=config)
        finally:
            if compressed:
                compressed.unlink(missing_ok=True)

        if transcript.status == aai.TranscriptStatus.error:
            logger.error("Transcription failed", error=transcript.error)
            raise Exception(f"Transcription failed: {transcript.error}")

        utterances = build_utterances(transcript)

        logger.info(
            "Transcription completed",
            transcript_id=transcript.id,
            duration_seconds=transcript.audio_duration,
            utterance_count=len(utterances),
            detected_language=transcript.language_code,
        )

        return TranscriptionResult(
            transcript_id=transcript.id,
            text=transcript.text or "",
            utterances=utterances,
            audio_duration_seconds=transcript.audio_duration or 0,
            confidence=transcript.confidence or 0.0,
            language=transcript.language_code or lang,
        )

    def estimate_cost(self, duration_seconds: int) -> float:
        """
        Estimate the cost for transcribing audio.

        Args:
            duration_seconds: Duration of audio in seconds

        Returns:
            Estimated cost in USD
        """
        # AssemblyAI pricing: $0.15 per hour = $0.0025 per minute
        minutes = duration_seconds / 60
        return minutes * 0.0025
