import assemblyai as aai
from pathlib import Path
from typing import Optional
import structlog

from src.config import settings
from src.services.dnd_vocabulary import DND_VOCABULARY

logger = structlog.get_logger()

# Configure AssemblyAI
aai.settings.api_key = settings.assemblyai_api_key


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
        transcript = self.transcriber.transcribe(str(audio_path), config=config)

        if transcript.status == aai.TranscriptStatus.error:
            logger.error("Transcription failed", error=transcript.error)
            raise Exception(f"Transcription failed: {transcript.error}")

        # Extract utterances with speaker labels
        utterances = []
        if transcript.utterances:
            for utterance in transcript.utterances:
                utterances.append({
                    "speaker": utterance.speaker,
                    "text": utterance.text,
                    "start_ms": utterance.start,
                    "end_ms": utterance.end,
                    "confidence": utterance.confidence,
                })

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
