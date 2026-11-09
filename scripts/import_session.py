"""
Import per-speaker recordings (e.g. a Craig export) and run them through the pipeline.

Live Discord voice recording is currently unreliable (py-cord can't receive voice
under Discord's DAVE encryption), so audio is captured elsewhere and imported here.

Accepts a directory of audio files, or a .zip (Craig's download format).
One file per speaker; the speaker name is taken from the filename, with any
leading "<number>-" prefix removed (Craig names files like "1-Username.flac").

Usage:
    python scripts/import_session.py path/to/craig-export.zip \
        --guild-id 123 --channel-id 456 --name "Session 12"
    python scripts/import_session.py path/to/folder --guild-id 123 --channel-id 456 --dry-run
"""
import argparse
import re
import shutil
import subprocess
import sys
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

AUDIO_EXTENSIONS = {".flac", ".wav", ".mp3", ".ogg", ".m4a", ".aac", ".opus"}
PREFIX_RE = re.compile(r"^\d+-")


def speaker_name(path: Path) -> str:
    """'1-Some_User.flac' -> 'Some_User'."""
    return PREFIX_RE.sub("", path.stem) or path.stem


def stable_user_id(name: str) -> int:
    """Deterministic pseudo user ID (Python's hash() changes between runs)."""
    return zlib.crc32(name.encode("utf-8"))


def audio_duration_seconds(path: Path) -> int | None:
    """Duration via ffprobe, or None if ffprobe is unavailable/fails."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return int(float(out.stdout.strip())) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def safe_extract(zip_path: Path, dest: Path) -> None:
    """Extract a zip, rejecting entries that would escape dest."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            target = (dest / member.filename).resolve()
            if root != target and root not in target.parents:
                raise ValueError(f"Unsafe path in zip: {member.filename}")
        zf.extractall(dest)


def find_audio_files(directory: Path) -> list[Path]:
    return sorted(
        p for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )


def main():
    parser = argparse.ArgumentParser(description="Import per-speaker recordings into the pipeline")
    parser.add_argument("source", type=Path, help="Directory of audio files, or a .zip")
    parser.add_argument("--guild-id", type=int, required=True, help="Discord server ID")
    parser.add_argument("--channel-id", type=int, required=True,
                        help="Discord text channel ID to post the finished report in")
    parser.add_argument("--name", help="Session name (default: source name)")
    parser.add_argument("--date", help="Session date, YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be imported without touching the database")
    args = parser.parse_args()

    source: Path = args.source.expanduser()
    if not source.exists():
        sys.exit(f"Not found: {source}")

    name = args.name or source.stem
    started = datetime.strptime(args.date, "%Y-%m-%d") if args.date else datetime.now(timezone.utc).replace(tzinfo=None)

    # Imported settings is deferred so --help works without a configured .env
    from src.config import settings

    # Work out where the audio lives. Zips are extracted into managed storage.
    if source.is_file():
        if source.suffix.lower() != ".zip":
            sys.exit("Source must be a directory or a .zip file")
        audio_dir = settings.audio_storage_path / re.sub(r"[^\w.-]+", "_", name)
        if args.dry_run:
            print(f"(dry run) would extract {source} -> {audio_dir}")
            with zipfile.ZipFile(source) as zf:
                files = [Path(n) for n in zf.namelist()
                         if Path(n).suffix.lower() in AUDIO_EXTENSIONS]
        else:
            if audio_dir.exists() and any(audio_dir.iterdir()):
                sys.exit(f"{audio_dir} already exists and is not empty; pick another --name")
            try:
                safe_extract(source, audio_dir)
            except (ValueError, zipfile.BadZipFile) as exc:
                shutil.rmtree(audio_dir, ignore_errors=True)
                sys.exit(f"Could not extract {source.name}: {exc}")
            files = find_audio_files(audio_dir)
    else:
        audio_dir = source
        files = find_audio_files(audio_dir)

    if not files:
        sys.exit("No audio files found")

    included: list[tuple[Path, str]] = []
    for f in files:
        speaker = speaker_name(f)
        lowered = speaker.lower()
        reason = next((p for p in settings.excluded_name_patterns if p.lower() in lowered), None)
        if reason:
            print(f"  EXCLUDED  {speaker}  (matches '{reason}')")
        else:
            included.append((f, speaker))
            print(f"  include   {speaker}  <- {f.name}")

    if not included:
        sys.exit("All tracks were excluded")
    if args.dry_run:
        print(f"\n(dry run) {len(included)} track(s) would be imported.")
        return

    from src.database.connection import SyncSessionLocal
    from src.database.models import Session, SessionAudioTrack
    from src.processing.tasks import process_session

    durations = [audio_duration_seconds(f) for f, _ in included]
    duration = max((d for d in durations if d), default=None)

    with SyncSessionLocal() as db:
        db_session = Session(
            guild_id=args.guild_id,
            channel_id=args.channel_id,
            notification_channel_id=args.channel_id,
            name=name,
            started_at=started,
            ended_at=started + timedelta(seconds=duration) if duration else started,
            duration_seconds=duration,
            status="processing",
            audio_directory=str(audio_dir),
        )
        db.add(db_session)
        db.flush()
        for f, speaker in included:
            db.add(SessionAudioTrack(
                session_id=db_session.id,
                discord_user_id=stable_user_id(speaker),
                discord_username=speaker,
                file_path=str(f),
                file_size_bytes=f.stat().st_size,
            ))
        db.commit()
        session_id = db_session.id

    process_session.delay(str(session_id))
    print(f"\nImported {len(included)} track(s) as session {session_id}.")
    print("Processing queued - make sure the Celery worker is running.")
    print("The report will be posted to your notification channel when it's done.")


if __name__ == "__main__":
    main()
