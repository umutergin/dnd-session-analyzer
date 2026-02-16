import subprocess
from pathlib import Path
from datetime import datetime
import structlog
import httpx
import json

from celery import chain, group, chord
from src.processing.celery_app import celery_app
from src.config import settings
from src.database.connection import SyncSessionLocal
from src.database.models import Session, SessionAudioTrack, Transcript, SessionSummary
from src.services.assemblyai_service import AssemblyAIService
from src.services.claude_service import ClaudeService

logger = structlog.get_logger()

DISCORD_API_BASE = "https://discord.com/api/v10"

# Discord file upload limits (in bytes)
# Using 8 MB as safe default (non-boosted servers)
# Boosted servers can have up to 25 MB, but we use conservative limit
DISCORD_FILE_SIZE_LIMIT = 8 * 1024 * 1024  # 8 MB


@celery_app.task(bind=True, max_retries=3)
def process_session(self, session_id: str):
    """
    Main task that orchestrates the full processing pipeline.

    Uses chord for parallel speaker transcription, then chains to analysis.

    Args:
        session_id: UUID of the session to process
    """
    logger.info("Starting session processing pipeline", session_id=session_id)

    # Update session status
    with SyncSessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        if not session:
            raise ValueError(f"Session {session_id} not found")

        session.status = "processing"
        session.processing_started_at = datetime.utcnow()
        db.commit()

    # Pipeline: merge -> parallel transcription (chord) -> analyze -> complete
    # First, run merge_audio_files, then trigger the parallel transcription pipeline
    pipeline = chain(
        merge_audio_files.s(session_id),
        start_parallel_transcription.s(session_id),
    )
    pipeline.apply_async()


@celery_app.task(bind=True, max_retries=3)
def complete_session(self, summary_id: str, session_id: str):
    """
    Mark session as completed and send Discord notification.

    Args:
        summary_id: UUID of the summary (passed from chain)
        session_id: UUID of the session
    """
    logger.info("Completing session", session_id=session_id, summary_id=summary_id)

    with SyncSessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        if not session:
            raise ValueError(f"Session {session_id} not found")

        session.status = "completed"
        session.processing_completed_at = datetime.utcnow()
        notification_channel_id = session.notification_channel_id or session.channel_id
        db.commit()

    # Send Discord notification
    send_discord_notification.delay(session_id, notification_channel_id)

    logger.info("Session processing completed", session_id=session_id)


@celery_app.task(bind=True)
def handle_pipeline_error(self, request, exc, traceback, session_id: str):
    """
    Error handler for the processing pipeline.

    Args:
        request: Celery request object
        exc: The exception that was raised
        traceback: Traceback string
        session_id: UUID of the session
    """
    logger.error(
        "Pipeline task failed",
        session_id=session_id,
        task_id=request.id,
        error=str(exc),
    )

    with SyncSessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        if session:
            session.status = "failed"
            session.error_message = str(exc)
            db.commit()


@celery_app.task(bind=True, max_retries=3)
def merge_audio_files(self, session_id: str) -> str:
    """
    Merge per-speaker audio files into a single file.

    Args:
        session_id: UUID of the session

    Returns:
        Path to the merged audio file
    """
    logger.info("Merging audio files", session_id=session_id)

    with SyncSessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        if not session:
            raise ValueError(f"Session {session_id} not found")

        audio_tracks = db.query(SessionAudioTrack).filter(
            SessionAudioTrack.session_id == session_id
        ).all()

        if not audio_tracks:
            raise ValueError(f"No audio tracks found for session {session_id}")

        # Prepare FFmpeg command to merge audio files
        audio_dir = Path(session.audio_directory)
        merged_path = audio_dir / "merged.wav"

        input_files = [track.file_path for track in audio_tracks]

        if len(input_files) == 1:
            # Just one speaker, copy the file
            import shutil
            shutil.copy(input_files[0], merged_path)
        else:
            # Multiple speakers - merge with FFmpeg
            # Build FFmpeg command: ffmpeg -i file1.wav -i file2.wav -filter_complex amix=inputs=N merged.wav
            cmd = ["ffmpeg", "-y"]  # -y to overwrite

            for f in input_files:
                cmd.extend(["-i", str(f)])

            cmd.extend([
                "-filter_complex", f"amix=inputs={len(input_files)}:duration=longest",
                "-ac", "1",  # Mono output
                "-ar", "48000",  # 48kHz sample rate
                str(merged_path)
            ])

            logger.info("Running FFmpeg", command=" ".join(cmd))

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                logger.error("FFmpeg failed", stderr=result.stderr)
                raise Exception(f"FFmpeg failed: {result.stderr}")

        # Update session with merged audio path
        session.merged_audio_path = str(merged_path)
        db.commit()

        logger.info("Audio files merged", session_id=session_id, output_path=str(merged_path))
        return str(merged_path)


@celery_app.task(bind=True, max_retries=3)
def start_parallel_transcription(self, audio_path: str, session_id: str):
    """
    Start parallel transcription of all speakers using chord.

    Args:
        audio_path: Path to merged audio (from chain, not used)
        session_id: UUID of the session
    """
    logger.info("Starting parallel transcription", session_id=session_id)

    # Update status
    with SyncSessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        session.status = "transcribing"
        db.commit()

        # Get all audio tracks
        audio_tracks = db.query(SessionAudioTrack).filter(
            SessionAudioTrack.session_id == session_id
        ).all()
        track_ids = [str(track.id) for track in audio_tracks]

    if not track_ids:
        raise ValueError(f"No audio tracks found for session {session_id}")

    logger.info(
        "Launching parallel transcription tasks",
        session_id=session_id,
        speaker_count=len(track_ids),
    )

    # Use chord: run all transcribe_speaker tasks in parallel,
    # then call combine_transcripts when all are done
    transcription_chord = chord(
        [transcribe_speaker.s(track_id) for track_id in track_ids],
        combine_transcripts.s(session_id),
    )
    transcription_chord.apply_async()


@celery_app.task(bind=True, max_retries=3)
def transcribe_speaker(self, track_id: str) -> dict:
    """
    Transcribe a single speaker's audio file.

    Args:
        track_id: UUID of the SessionAudioTrack

    Returns:
        Dict with speaker utterances and metadata
    """
    logger.info("Transcribing speaker", track_id=track_id)

    # Get track info
    with SyncSessionLocal() as db:
        track = db.query(SessionAudioTrack).filter(
            SessionAudioTrack.id == track_id
        ).first()
        if not track:
            raise ValueError(f"Audio track {track_id} not found")

        username = track.discord_username or f"User_{track.discord_user_id}"
        file_path = track.file_path

    # Transcribe
    service = AssemblyAIService()

    try:
        result = service.transcribe_file(
            Path(file_path),
            speaker_labels=False,  # Single speaker per file
        )

        utterances = []
        if result.utterances:
            for utt in result.utterances:
                utt["speaker"] = username
                utterances.append(utt)
        elif result.text:
            utterances.append({
                "speaker": username,
                "text": result.text,
                "start_ms": 0,
                "end_ms": (result.audio_duration_seconds or 0) * 1000,
                "confidence": result.confidence or 0.0,
            })

        return {
            "track_id": track_id,
            "username": username,
            "utterances": utterances,
            "duration_seconds": result.audio_duration_seconds or 0,
            "confidence": result.confidence or 0.0,
            "language": result.language,
        }

    except Exception as e:
        logger.error(
            "Failed to transcribe speaker",
            track_id=track_id,
            username=username,
            error=str(e),
        )
        # Return empty result so other speakers still complete
        return {
            "track_id": track_id,
            "username": username,
            "utterances": [],
            "duration_seconds": 0,
            "confidence": 0.0,
            "language": None,
            "error": str(e),
        }


@celery_app.task(bind=True, max_retries=3)
def combine_transcripts(self, speaker_results: list, session_id: str):
    """
    Combine transcription results from all speakers and continue pipeline.

    Args:
        speaker_results: List of dicts from parallel transcribe_speaker tasks
        session_id: UUID of the session
    """
    logger.info(
        "Combining transcripts",
        session_id=session_id,
        speaker_count=len(speaker_results),
    )

    # Collect all utterances and metadata
    all_utterances = []
    total_duration = 0
    total_confidence = 0
    confidence_count = 0
    detected_language = None

    for result in speaker_results:
        if result.get("error"):
            logger.warning(
                "Speaker transcription had error",
                session_id=session_id,
                username=result.get("username"),
                error=result.get("error"),
            )

        all_utterances.extend(result.get("utterances", []))
        total_duration += result.get("duration_seconds", 0)

        if result.get("confidence"):
            total_confidence += result["confidence"]
            confidence_count += 1

        if result.get("language"):
            detected_language = result["language"]

    # Sort utterances by start time
    all_utterances.sort(key=lambda x: x.get("start_ms", 0))

    # Combine all text
    full_text = "\n".join(
        f"{utt['speaker']}: {utt['text']}"
        for utt in all_utterances
    )

    avg_confidence = total_confidence / confidence_count if confidence_count > 0 else 0.0

    # Store transcript in database
    with SyncSessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()

        transcript = Transcript(
            session_id=session_id,
            assemblyai_transcript_id=None,
            full_text=full_text,
            utterances=all_utterances,
            language=detected_language,
            audio_duration_seconds=total_duration,
            confidence_average=avg_confidence,
        )
        db.add(transcript)

        # Estimate cost
        service = AssemblyAIService()
        cost = service.estimate_cost(total_duration)
        session.transcription_cost_cents = int(cost * 100)

        db.commit()

        transcript_id = str(transcript.id)

    logger.info(
        "Transcripts combined",
        session_id=session_id,
        transcript_id=transcript_id,
        total_duration=total_duration,
        utterance_count=len(all_utterances),
    )

    # Continue pipeline: analyze -> complete
    pipeline = chain(
        analyze_transcript.s(transcript_id, session_id),
        complete_session.s(session_id),
    )
    pipeline.apply_async()


@celery_app.task(bind=True, max_retries=3)
def transcribe_audio(self, audio_path: str, session_id: str) -> str:
    """
    Transcribe audio using AssemblyAI - processes each speaker's audio separately.

    Args:
        audio_path: Path to the merged audio file (not used, kept for chain compatibility)
        session_id: UUID of the session

    Returns:
        Transcript ID from the database
    """
    logger.info("Starting per-speaker transcription", session_id=session_id)

    # Update status
    with SyncSessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        session.status = "transcribing"
        db.commit()

        # Get all audio tracks with usernames
        audio_tracks = db.query(SessionAudioTrack).filter(
            SessionAudioTrack.session_id == session_id
        ).all()

    if not audio_tracks:
        raise ValueError(f"No audio tracks found for session {session_id}")

    # Transcribe each speaker's audio separately
    service = AssemblyAIService()
    all_utterances = []
    total_duration = 0
    total_confidence = 0
    track_count = 0
    detected_language = None

    for track in audio_tracks:
        username = track.discord_username or f"User_{track.discord_user_id}"
        logger.info(
            "Transcribing speaker audio",
            session_id=session_id,
            username=username,
            file_path=track.file_path,
        )

        try:
            # Transcribe without speaker diarization (single speaker per file)
            result = service.transcribe_file(
                Path(track.file_path),
                speaker_labels=False,  # Don't need diarization for single speaker
            )

            # Add utterances with the Discord username as speaker
            if result.utterances:
                for utt in result.utterances:
                    utt["speaker"] = username
                    all_utterances.append(utt)
            elif result.text:
                # If no utterances but has text, create one utterance
                all_utterances.append({
                    "speaker": username,
                    "text": result.text,
                    "start_ms": 0,
                    "end_ms": (result.audio_duration_seconds or 0) * 1000,
                    "confidence": result.confidence or 0.0,
                })

            total_duration += result.audio_duration_seconds or 0
            if result.confidence:
                total_confidence += result.confidence
                track_count += 1
            if result.language:
                detected_language = result.language

        except Exception as e:
            logger.error(
                "Failed to transcribe speaker audio",
                session_id=session_id,
                username=username,
                error=str(e),
            )
            # Continue with other speakers

    # Sort utterances by start time
    all_utterances.sort(key=lambda x: x.get("start_ms", 0))

    # Combine all text
    full_text = "\n".join(
        f"{utt['speaker']}: {utt['text']}"
        for utt in all_utterances
    )

    avg_confidence = total_confidence / track_count if track_count > 0 else 0.0

    # Store transcript in database
    with SyncSessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()

        transcript = Transcript(
            session_id=session_id,
            assemblyai_transcript_id=None,  # Multiple transcripts, no single ID
            full_text=full_text,
            utterances=all_utterances,
            language=detected_language,
            audio_duration_seconds=total_duration,
            confidence_average=avg_confidence,
        )
        db.add(transcript)

        # Update session with cost estimate
        cost = service.estimate_cost(total_duration)
        session.transcription_cost_cents = int(cost * 100)

        db.commit()

        logger.info(
            "Transcription completed",
            session_id=session_id,
            transcript_id=str(transcript.id),
            duration_seconds=total_duration,
            speaker_count=len(audio_tracks),
        )

        return str(transcript.id)


@celery_app.task(bind=True, max_retries=2)
def analyze_transcript(self, transcript_id: str, session_id: str) -> str:
    """
    Analyze transcript using Claude to extract summary and entities.

    Args:
        transcript_id: UUID of the transcript (from chain)
        session_id: UUID of the session

    Returns:
        Summary ID from the database
    """
    logger.info("Starting LLM analysis", session_id=session_id, transcript_id=transcript_id)

    # Update status
    with SyncSessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        session.status = "analyzing"
        db.commit()

    # Get transcript text
    with SyncSessionLocal() as db:
        transcript = db.query(Transcript).filter(Transcript.id == transcript_id).first()
        if not transcript:
            raise ValueError(f"Transcript {transcript_id} not found")

        # Format transcript with speaker labels for analysis
        formatted_transcript = transcript.full_text

        if transcript.utterances:
            # Use utterance format with speaker labels
            lines = []
            for utterance in transcript.utterances:
                speaker = utterance.get("speaker", "Unknown")
                text = utterance.get("text", "")
                lines.append(f"{speaker}: {text}")
            formatted_transcript = "\n".join(lines)

    # Analyze with Claude
    service = ClaudeService()
    result = service.analyze_session(formatted_transcript)

    # Store summary in database
    with SyncSessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()

        summary = SessionSummary(
            session_id=session_id,
            short_summary=result.short_summary,
            detailed_summary=result.detailed_summary,
            key_events=result.key_events,
            combat_encounters=result.combat_encounters,
            npcs_mentioned=result.npcs_mentioned,
            locations_mentioned=result.locations_mentioned,
            model_used=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
        db.add(summary)

        # Update session with cost estimate
        cost = service.estimate_cost(result.prompt_tokens, result.completion_tokens)
        session.llm_cost_cents = int(cost * 100)

        db.commit()

        logger.info(
            "LLM analysis completed",
            session_id=session_id,
            summary_id=str(summary.id),
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )

        return str(summary.id)


def generate_session_report(
    session: Session,
    summary: SessionSummary | None,
    transcript: Transcript | None,
) -> str:
    """
    Generate a full markdown report for a session.

    Args:
        session: The session object
        summary: The session summary (if available)
        transcript: The transcript (if available)

    Returns:
        Markdown formatted report string
    """
    lines = []

    # Header
    lines.append(f"# D&D Session Report: {session.name or 'Unnamed Session'}")
    lines.append("")
    lines.append(f"**Session ID:** `{session.id}`")
    lines.append(f"**Date:** {session.started_at.strftime('%Y-%m-%d %H:%M') if session.started_at else 'Unknown'}")

    if session.duration_seconds:
        hours, remainder = divmod(session.duration_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        lines.append(f"**Duration:** {hours}h {minutes}m {seconds}s")

    lines.append("")

    # Transcript stats
    if transcript:
        speaker_count = len(set(u.get("speaker") for u in (transcript.utterances or [])))
        lines.append("## Transcript Info")
        lines.append(f"- **Speakers:** {speaker_count}")
        lines.append(f"- **Language:** {transcript.language or 'Unknown'}")
        if transcript.confidence_average:
            lines.append(f"- **Confidence:** {transcript.confidence_average:.1%}")
        if transcript.audio_duration_seconds:
            lines.append(f"- **Audio Duration:** {transcript.audio_duration_seconds} seconds")
        lines.append("")

    if summary:
        # Short summary
        if summary.short_summary:
            lines.append("## Summary")
            lines.append(summary.short_summary)
            lines.append("")

        # Detailed summary
        if summary.detailed_summary:
            lines.append("## Detailed Summary")
            lines.append(summary.detailed_summary)
            lines.append("")

        # Key events
        if summary.key_events:
            lines.append("## Key Events")
            for i, event in enumerate(summary.key_events, 1):
                desc = event.get("description", "Unknown event")
                timestamp = event.get("timestamp", "")
                if timestamp:
                    lines.append(f"{i}. [{timestamp}] {desc}")
                else:
                    lines.append(f"{i}. {desc}")
            lines.append("")

        # Combat encounters
        if summary.combat_encounters:
            lines.append("## Combat Encounters")
            for i, combat in enumerate(summary.combat_encounters, 1):
                enemies = ", ".join(combat.get("enemies", ["Unknown"]))
                outcome = combat.get("outcome", "Unknown")
                description = combat.get("description", "")
                lines.append(f"### Encounter {i}: vs {enemies}")
                lines.append(f"**Outcome:** {outcome}")
                if description:
                    lines.append(f"\n{description}")
                lines.append("")

        # NPCs
        if summary.npcs_mentioned:
            lines.append("## NPCs Mentioned")
            for npc in summary.npcs_mentioned:
                name = npc.get("name", "Unknown")
                description = npc.get("description", "")
                role = npc.get("role", "")
                lines.append(f"### {name}")
                if role:
                    lines.append(f"*{role}*")
                if description:
                    lines.append(f"\n{description}")
                lines.append("")

        # Locations
        if summary.locations_mentioned:
            lines.append("## Locations")
            for loc in summary.locations_mentioned:
                name = loc.get("name", "Unknown")
                description = loc.get("description", "")
                loc_type = loc.get("type", "")
                lines.append(f"### {name}")
                if loc_type:
                    lines.append(f"*Type: {loc_type}*")
                if description:
                    lines.append(f"\n{description}")
                lines.append("")

    # Full transcript (if available and not too long)
    if transcript and transcript.utterances:
        lines.append("## Full Transcript")
        lines.append("")
        for utterance in transcript.utterances:
            speaker = utterance.get("speaker", "Unknown")
            text = utterance.get("text", "")
            lines.append(f"**Speaker {speaker}:** {text}")
            lines.append("")

    lines.append("---")
    lines.append("*Generated by D&D Session Recorder Bot*")

    return "\n".join(lines)


def truncate_report_for_discord(report_content: str, max_bytes: int = DISCORD_FILE_SIZE_LIMIT) -> tuple[str, bool]:
    """
    Truncate report content if it exceeds Discord's file size limit.

    Prioritizes keeping summary and key events, truncates transcript if needed.

    Args:
        report_content: Full markdown report
        max_bytes: Maximum file size in bytes

    Returns:
        Tuple of (truncated_content, was_truncated)
    """
    content_bytes = report_content.encode("utf-8")
    if len(content_bytes) <= max_bytes:
        return report_content, False

    logger.warning(
        "Report exceeds Discord file size limit, truncating",
        original_size=len(content_bytes),
        max_size=max_bytes,
    )

    # Find the transcript section and truncate it
    transcript_marker = "## Full Transcript"
    footer_marker = "---\n*Generated by"

    if transcript_marker in report_content:
        # Split at transcript section
        before_transcript = report_content.split(transcript_marker)[0]
        after_parts = report_content.split(footer_marker)
        footer = footer_marker + after_parts[-1] if len(after_parts) > 1 else ""

        # Calculate available space for transcript
        overhead = len(before_transcript.encode("utf-8")) + len(footer.encode("utf-8"))
        # Add truncation notice
        truncation_notice = (
            "\n\n## Full Transcript\n\n"
            "*[Transcript truncated due to Discord file size limit. "
            "Full transcript is available in the database.]*\n\n"
        )
        overhead += len(truncation_notice.encode("utf-8"))
        available = max_bytes - overhead - 1000  # Buffer

        if available > 0:
            # Try to include partial transcript
            transcript_section = report_content.split(transcript_marker)[1].split(footer_marker)[0]
            # Truncate to available bytes
            truncated_transcript = transcript_section.encode("utf-8")[:available].decode("utf-8", errors="ignore")
            # Find last complete line
            last_newline = truncated_transcript.rfind("\n")
            if last_newline > 0:
                truncated_transcript = truncated_transcript[:last_newline]

            result = (
                before_transcript +
                "## Full Transcript\n" +
                truncated_transcript +
                "\n\n*[Transcript truncated...]*\n\n" +
                footer
            )
        else:
            # Not enough space for any transcript
            result = before_transcript + truncation_notice + footer
    else:
        # No transcript section, just truncate from end
        truncated = content_bytes[:max_bytes - 100].decode("utf-8", errors="ignore")
        last_newline = truncated.rfind("\n")
        if last_newline > 0:
            truncated = truncated[:last_newline]
        result = truncated + "\n\n*[Report truncated...]*"

    return result, True


@celery_app.task(bind=True, max_retries=3)
def send_discord_notification(self, session_id: str, channel_id: int):
    """
    Send a Discord notification with embed and full report file.

    Args:
        session_id: UUID of the session
        channel_id: Discord channel ID to send notification to
    """
    logger.info(
        "Sending Discord notification",
        session_id=session_id,
        channel_id=channel_id,
    )

    # Get session data from database
    with SyncSessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        if not session:
            logger.error("Session not found for notification", session_id=session_id)
            return

        summary = db.query(SessionSummary).filter(
            SessionSummary.session_id == session_id
        ).first()

        transcript = db.query(Transcript).filter(
            Transcript.session_id == session_id
        ).first()

        # Generate full report
        report_content = generate_session_report(session, summary, transcript)

        # Truncate if exceeds Discord file size limit
        report_content, was_truncated = truncate_report_for_discord(report_content)
        if was_truncated:
            logger.info(
                "Report was truncated for Discord",
                session_id=session_id,
                final_size=len(report_content.encode("utf-8")),
            )

    # Build a shorter embed for the notification
    embed = {
        "title": f"Session Complete: {session.name or 'Unnamed Session'}",
        "color": 0x00FF00,  # Green
        "timestamp": datetime.utcnow().isoformat(),
        "fields": [],
        "footer": {"text": "Full report attached below"}
    }

    # Add duration
    if session.duration_seconds:
        hours, remainder = divmod(session.duration_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        embed["fields"].append({
            "name": "Duration",
            "value": f"{hours}h {minutes}m {seconds}s",
            "inline": True
        })

    # Add transcript info
    if transcript:
        speaker_count = len(set(u.get("speaker") for u in (transcript.utterances or [])))
        embed["fields"].append({
            "name": "Speakers",
            "value": str(speaker_count),
            "inline": True
        })
        if transcript.confidence_average:
            embed["fields"].append({
                "name": "Confidence",
                "value": f"{transcript.confidence_average:.1%}",
                "inline": True
            })

    # Add short summary as description (truncated if needed)
    if summary and summary.short_summary:
        embed["description"] = summary.short_summary[:2000]

    # Send via Discord REST API with file attachment
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {settings.discord_bot_token}",
    }

    # Prepare multipart form data
    payload_json = {
        "content": "Your D&D session has been processed!",
        "embeds": [embed],
    }

    # Create file for attachment
    session_name_safe = (session.name or "session").replace(" ", "_").replace("/", "-")[:50]
    filename = f"{session_name_safe}_report.md"

    try:
        response = httpx.post(
            url,
            headers=headers,
            data={"payload_json": json.dumps(payload_json)},
            files={"files[0]": (filename, report_content.encode("utf-8"), "text/markdown")},
            timeout=60,
        )
        response.raise_for_status()
        logger.info(
            "Discord notification sent successfully",
            session_id=session_id,
            channel_id=channel_id,
        )
    except httpx.HTTPStatusError as e:
        logger.error(
            "Failed to send Discord notification",
            session_id=session_id,
            status_code=e.response.status_code,
            response=e.response.text,
        )
        raise self.retry(exc=e, countdown=30)
    except Exception as e:
        logger.error(
            "Failed to send Discord notification",
            session_id=session_id,
            error=str(e),
        )
        raise self.retry(exc=e, countdown=30)
