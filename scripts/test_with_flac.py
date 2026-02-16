"""
Test script to process existing FLAC files through the full pipeline.

This creates a fake session in the database pointing to FLAC files,
then triggers the processing pipeline to test parallel transcription.

Usage:
    python scripts/test_with_flac.py --guild-id 123456 --channel-id 789012
    python scripts/test_with_flac.py  # Interactive mode
"""
import sys
import argparse
from pathlib import Path
from datetime import datetime, timedelta
import re

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.database.connection import SyncSessionLocal
from src.database.models import Session, SessionAudioTrack
from src.processing.tasks import process_session


def should_exclude_username(username: str) -> tuple[bool, str]:
    """
    Check if a username matches exclusion patterns.

    Returns:
        Tuple of (should_exclude, reason)
    """
    username_lower = username.lower()

    for pattern in settings.excluded_name_patterns:
        pattern_lower = pattern.lower()
        if pattern_lower in username_lower:
            return True, f"name_pattern:{pattern}"

    return False, ""


def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Test FLAC file processing pipeline")
    parser.add_argument("--guild-id", type=int, help="Discord Guild (Server) ID")
    parser.add_argument("--channel-id", type=int, help="Discord text channel ID for notifications")
    parser.add_argument("--session-name", default="FLAC Test Session", help="Name for the test session")
    parser.add_argument("--flac-dir", type=Path,
                        default=Path(r"C:\Users\umutb\OneDrive\Belgeler\Audacity\craig-hjmBYuSIYXSK-z1mX4rpKsYfRb6csVEpx5Bp8n5K2GN.aup\hjmBYuSIYXSK_data"),
                        help="Directory containing FLAC files")
    parser.add_argument("--auto-confirm", "-y", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    # Path to FLAC files
    flac_dir = args.flac_dir

    if not flac_dir.exists():
        print(f"FLAC directory not found: {flac_dir}")
        return

    # Find all FLAC files
    flac_files = list(flac_dir.glob("*.flac"))
    if not flac_files:
        print("No FLAC files found")
        return

    print(f"Found {len(flac_files)} FLAC files:")
    for f in flac_files:
        size_mb = f.stat().st_size / (1024 * 1024)
        print(f"  - {f.name} ({size_mb:.1f} MB)")

    # Get guild_id and channel_id (from args or interactive)
    if args.guild_id and args.channel_id:
        guild_id = args.guild_id
        channel_id = args.channel_id
    else:
        print("\n" + "="*60)
        print("To send the Discord notification, I need your server details.")
        print("You can find these by enabling Developer Mode in Discord settings,")
        print("then right-clicking on your server and channel to copy IDs.")
        print("="*60 + "\n")

        guild_id_str = input("Enter your Discord Guild (Server) ID: ").strip()
        channel_id_str = input("Enter the text channel ID for notifications: ").strip()

        try:
            guild_id = int(guild_id_str)
            channel_id = int(channel_id_str)
        except ValueError:
            print("Invalid IDs. Please enter numeric IDs.")
            return

    session_name = args.session_name

    print(f"\nCreating test session: {session_name}")
    print(f"Guild ID: {guild_id}")
    print(f"Notification Channel: {channel_id}")

    # Create session in database
    with SyncSessionLocal() as db:
        # Create session record
        db_session = Session(
            guild_id=guild_id,
            channel_id=channel_id,  # Voice channel (same as notification for test)
            notification_channel_id=channel_id,
            name=session_name,
            started_at=datetime.utcnow() - timedelta(hours=4),  # Pretend it started 4 hours ago
            ended_at=datetime.utcnow(),
            duration_seconds=4 * 3600,  # 4 hours
            status="processing",
            audio_directory=str(flac_dir),
        )
        db.add(db_session)
        db.flush()  # Get the ID

        session_id = db_session.id
        print(f"Created session: {session_id}")

        # Create audio track records for each FLAC file
        # Filename pattern: {number}-{username}.flac
        included_count = 0
        excluded_count = 0

        for flac_file in flac_files:
            # Extract username from filename (e.g., "1-MatchBox_2664.flac" -> "MatchBox_2664")
            match = re.match(r'\d+-(.+)\.flac', flac_file.name)
            if match:
                username = match.group(1)
            else:
                username = flac_file.stem

            # Check if user should be excluded (music bots, etc.)
            is_excluded, exclude_reason = should_exclude_username(username)
            if is_excluded:
                excluded_count += 1
                print(f"  EXCLUDED: {username} -> {flac_file.name} (reason: {exclude_reason})")
                continue

            # Generate a fake Discord user ID based on hash of username
            fake_user_id = abs(hash(username)) % (10**18)  # 18-digit ID

            track = SessionAudioTrack(
                session_id=session_id,
                discord_user_id=fake_user_id,
                discord_username=username,
                file_path=str(flac_file),
                file_size_bytes=flac_file.stat().st_size,
            )
            db.add(track)
            included_count += 1
            print(f"  Added track: {username} -> {flac_file.name}")

        db.commit()
        print(f"\nSession created with {included_count} tracks ({excluded_count} excluded)")
        if excluded_count > 0:
            print(f"  Exclusion patterns: {settings.excluded_name_patterns}")

    # Trigger processing
    print("\n" + "="*60)
    print("Starting processing pipeline...")
    print("This will:")
    print("  1. Merge audio files (FFmpeg)")
    print("  2. Transcribe each speaker in PARALLEL (AssemblyAI)")
    print("  3. Analyze transcript (Claude)")
    print("  4. Send Discord notification with report")
    print("="*60 + "\n")

    if not args.auto_confirm:
        confirm = input("Start processing? (y/n): ").strip().lower()
        if confirm != 'y':
            print("Cancelled.")
            return

    # Trigger the Celery task
    process_session.delay(str(session_id))

    print(f"\nProcessing started! Task queued for session {session_id}")
    print("Watch the Celery worker logs to see progress.")
    print("You'll receive a Discord notification when complete.")


if __name__ == "__main__":
    main()
