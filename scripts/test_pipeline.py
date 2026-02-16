"""
Test script to run the full transcription and analysis pipeline on a recorded session.
"""
import asyncio
from pathlib import Path
import sys
import io

# Fix Windows console encoding for Turkish characters
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.assemblyai_service import AssemblyAIService
from src.services.claude_service import ClaudeService
import structlog

# Configure logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
)
logger = structlog.get_logger()


async def main():
    # Path to the test recording
    audio_path = Path("data/audio/test1/speaker_226414190783365120.wav")

    if not audio_path.exists():
        print(f"Audio file not found: {audio_path}")
        return

    print(f"\n{'='*60}")
    print("D&D Session Processing Pipeline Test")
    print(f"{'='*60}\n")

    print(f"Audio file: {audio_path}")
    print(f"File size: {audio_path.stat().st_size / 1024 / 1024:.2f} MB\n")

    # Step 1: Transcription
    print(f"{'='*60}")
    print("Step 1: Transcribing with AssemblyAI...")
    print(f"{'='*60}\n")

    transcription_service = AssemblyAIService(
        language_code="tr",
        use_vocabulary_boost=True,
    )

    try:
        result = transcription_service.transcribe_file(
            audio_path=audio_path,
            speaker_labels=True,
        )

        print(f"Transcription ID: {result.transcript_id}")
        print(f"Duration: {result.audio_duration_seconds} seconds")
        print(f"Confidence: {result.confidence:.2%}")
        print(f"Language: {result.language}")
        print(f"Utterances: {len(result.utterances)}")
        print(f"\n--- Full Transcript ---\n")
        print(result.text)
        print(f"\n--- Speaker Utterances ---\n")
        for utt in result.utterances[:10]:  # First 10 utterances
            print(f"Speaker {utt['speaker']}: {utt['text']}")
        if len(result.utterances) > 10:
            print(f"... and {len(result.utterances) - 10} more utterances")

    except Exception as e:
        print(f"Transcription failed: {e}")
        return

    # Step 2: AI Analysis
    print(f"\n{'='*60}")
    print("Step 2: Analyzing with Claude...")
    print(f"{'='*60}\n")

    claude_service = ClaudeService()

    try:
        analysis = claude_service.analyze_session(result.text)

        print("--- Session Summary ---\n")
        print(analysis.short_summary or "No summary generated")

        print("\n--- Detailed Summary ---\n")
        print(analysis.detailed_summary or "No detailed summary")

        print("\n--- Key Events ---")
        for event in analysis.key_events[:5]:
            print(f"  - {event.get('description', 'Unknown')}")

        print("\n--- NPCs Mentioned ---")
        for npc in analysis.npcs_mentioned[:5]:
            print(f"  - {npc.get('name', 'Unknown')}: {npc.get('description', '')}")

        print("\n--- Locations ---")
        for loc in analysis.locations_mentioned[:5]:
            print(f"  - {loc.get('name', 'Unknown')}: {loc.get('description', '')}")

        print("\n--- Combat Encounters ---")
        for combat in analysis.combat_encounters[:3]:
            print(f"  - Enemies: {combat.get('enemies', [])} | Outcome: {combat.get('outcome', 'Unknown')}")

    except Exception as e:
        print(f"Analysis failed: {e}")
        return

    print(f"\n{'='*60}")
    print("Pipeline completed successfully!")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
