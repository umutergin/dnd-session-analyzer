import discord
from discord.ext import commands
from datetime import datetime
import uuid
import structlog

from src.config import settings
from src.recorder.session_recorder import SessionRecorder
from src.database.connection import AsyncSessionLocal
from src.database.models import Session, SessionAudioTrack, SessionSummary
from src.processing.tasks import process_session

logger = structlog.get_logger()


def should_exclude_user(member: discord.Member | None, user_id: int) -> tuple[bool, str]:
    """
    Check if a user should be excluded from transcription.

    Returns:
        Tuple of (should_exclude, reason)
    """
    # Check if user ID is in exclusion list
    if user_id in settings.excluded_user_ids:
        return True, "user_id_excluded"

    if member is None:
        return False, ""

    # Check if it's a bot and bot exclusion is enabled
    if settings.exclude_bots_from_recording and member.bot:
        return True, "discord_bot"

    # Check name patterns
    display_name_lower = member.display_name.lower()
    username_lower = member.name.lower()

    for pattern in settings.excluded_name_patterns:
        pattern_lower = pattern.lower()
        if pattern_lower in display_name_lower or pattern_lower in username_lower:
            return True, f"name_pattern:{pattern}"

    return False, ""


class Recording(commands.Cog):
    """Cog for handling D&D session recording commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.recorder = SessionRecorder(settings.audio_storage_path)
        # Map guild_id to database session UUID
        self._session_db_ids: dict[int, uuid.UUID] = {}

    # Create a command group for /dnd commands
    dnd = discord.SlashCommandGroup(name="dnd", description="D&D session recording commands")

    @dnd.command(name="start", description="Start recording the D&D session")
    async def start_recording(
        self,
        ctx: discord.ApplicationContext,
        session_name: discord.Option(str, "Optional name for this session", required=False) = None,
    ):
        """Start recording the current voice channel."""
        # Check if user is in a voice channel
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.respond(
                "You need to be in a voice channel to start recording!",
                ephemeral=True,
            )
            return

        voice_channel = ctx.author.voice.channel

        # Check if already recording
        if self.recorder.is_recording(ctx.guild_id):
            await ctx.respond(
                "Already recording in this server! Use `/dnd stop` to end the current session.",
                ephemeral=True,
            )
            return

        await ctx.defer()

        try:
            # Start recording
            recording_session = await self.recorder.start_recording(
                voice_channel=voice_channel,
                session_name=session_name,
            )

            # Create database record
            async with AsyncSessionLocal() as db:
                db_session = Session(
                    guild_id=ctx.guild_id,
                    channel_id=voice_channel.id,
                    notification_channel_id=ctx.channel_id,  # Text channel for notifications
                    name=session_name or recording_session.session_id,
                    started_at=recording_session.started_at,
                    status="recording",
                    audio_directory=str(recording_session.output_dir),
                )
                db.add(db_session)
                await db.commit()
                await db.refresh(db_session)

                # Store mapping
                self._session_db_ids[ctx.guild_id] = db_session.id

            # Create success embed
            embed = discord.Embed(
                title="Recording Started",
                description=f"Now recording in **{voice_channel.name}**",
                color=discord.Color.green(),
                timestamp=datetime.now(),
            )
            embed.add_field(name="Session ID", value=recording_session.session_id, inline=True)
            embed.add_field(
                name="Channel",
                value=voice_channel.mention,
                inline=True,
            )
            embed.add_field(
                name="Members",
                value=str(len([m for m in voice_channel.members if not m.bot])),
                inline=True,
            )
            embed.set_footer(text="Use /dnd stop to end the recording")

            await ctx.followup.send(embed=embed)

            logger.info(
                "Recording started via command",
                session_id=recording_session.session_id,
                guild_id=ctx.guild_id,
                user=str(ctx.author),
            )

        except Exception as e:
            logger.error("Failed to start recording", error=str(e))
            await ctx.followup.send(
                f"Failed to start recording: {str(e)}",
                ephemeral=True,
            )

    @dnd.command(name="stop", description="Stop recording the D&D session")
    async def stop_recording(self, ctx: discord.ApplicationContext):
        """Stop the current recording session."""
        if not self.recorder.is_recording(ctx.guild_id):
            await ctx.respond(
                "Not currently recording in this server!",
                ephemeral=True,
            )
            return

        await ctx.defer()

        try:
            recording_session = await self.recorder.stop_recording(ctx.guild_id)

            # Calculate duration
            duration = datetime.now() - recording_session.started_at
            hours, remainder = divmod(int(duration.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            duration_str = f"{hours}h {minutes}m {seconds}s"

            # Update database record
            db_session_id = self._session_db_ids.pop(ctx.guild_id, None)
            excluded_count = 0
            included_count = 0

            if db_session_id:
                async with AsyncSessionLocal() as db:
                    db_session = await db.get(Session, db_session_id)
                    if db_session:
                        db_session.ended_at = datetime.now()
                        db_session.duration_seconds = int(duration.total_seconds())
                        db_session.status = "processing"

                        # Add audio track records with Discord usernames
                        for user_id, file_path in recording_session.speaker_files.items():
                            # Get Discord member
                            member = ctx.guild.get_member(user_id)
                            username = member.display_name if member else f"User_{user_id}"

                            # Check if user should be excluded (bot, music bot, etc.)
                            is_excluded, exclude_reason = should_exclude_user(member, user_id)

                            if is_excluded:
                                excluded_count += 1
                                logger.info(
                                    "Excluding user from transcription",
                                    user_id=user_id,
                                    username=username,
                                    reason=exclude_reason,
                                )
                                continue  # Skip adding this track

                            track = SessionAudioTrack(
                                session_id=db_session_id,
                                discord_user_id=user_id,
                                discord_username=username,
                                file_path=str(file_path),
                            )
                            db.add(track)
                            included_count += 1

                        if excluded_count > 0:
                            logger.info(
                                "Excluded users from processing",
                                excluded_count=excluded_count,
                                included_count=included_count,
                                session_id=str(db_session_id),
                            )

                        await db.commit()

                # Trigger async processing pipeline
                process_session.delay(str(db_session_id))

            # Create success embed
            embed = discord.Embed(
                title="Recording Stopped",
                description="Session has been saved and is being processed.",
                color=discord.Color.blue(),
                timestamp=datetime.now(),
            )
            embed.add_field(name="Session ID", value=recording_session.session_id, inline=True)
            embed.add_field(name="Duration", value=duration_str, inline=True)

            # Show speaker count with exclusion info
            total_speakers = len(recording_session.speaker_files)
            speakers_value = str(total_speakers)
            if excluded_count > 0:
                speakers_value = f"{included_count} ({excluded_count} bot{'s' if excluded_count > 1 else ''} excluded)"

            embed.add_field(
                name="Speakers Recorded",
                value=speakers_value,
                inline=True,
            )
            embed.add_field(
                name="Status",
                value="Processing... You'll be notified when the transcript is ready.",
                inline=False,
            )
            embed.set_footer(text="Use /dnd session to view past sessions")

            await ctx.followup.send(embed=embed)

            logger.info(
                "Recording stopped via command",
                session_id=recording_session.session_id,
                duration_seconds=int(duration.total_seconds()),
                speaker_count=len(recording_session.speaker_files),
            )

        except Exception as e:
            logger.error("Failed to stop recording", error=str(e))
            await ctx.followup.send(
                f"Failed to stop recording: {str(e)}",
                ephemeral=True,
            )

    @dnd.command(name="status", description="Check current recording status")
    async def status(self, ctx: discord.ApplicationContext):
        """Get the status of the current recording."""
        status = self.recorder.get_session_status(ctx.guild_id)

        if not status:
            await ctx.respond(
                "Not currently recording in this server.",
                ephemeral=True,
            )
            return

        # Calculate duration
        hours, remainder = divmod(status["duration_seconds"], 3600)
        minutes, seconds = divmod(remainder, 60)
        duration_str = f"{hours}h {minutes}m {seconds}s"

        # Get channel name
        channel = self.bot.get_channel(status["channel_id"])
        channel_name = channel.name if channel else "Unknown"

        # Create status embed
        embed = discord.Embed(
            title="Recording Status",
            color=discord.Color.orange() if status["is_paused"] else discord.Color.green(),
        )
        embed.add_field(name="Session ID", value=status["session_id"], inline=True)
        embed.add_field(name="Channel", value=channel_name, inline=True)
        embed.add_field(name="Duration", value=duration_str, inline=True)
        embed.add_field(
            name="Status",
            value="Paused" if status["is_paused"] else "Recording",
            inline=True,
        )
        embed.add_field(name="Active Speakers", value=str(status["speaker_count"]), inline=True)

        await ctx.respond(embed=embed)

    @dnd.command(name="pause", description="Pause the current recording")
    async def pause(self, ctx: discord.ApplicationContext):
        """Pause the current recording."""
        try:
            await self.recorder.pause_recording(ctx.guild_id)
            await ctx.respond(
                "Recording paused. Use `/dnd resume` to continue.",
            )
        except ValueError as e:
            await ctx.respond(str(e), ephemeral=True)

    @dnd.command(name="resume", description="Resume a paused recording")
    async def resume(self, ctx: discord.ApplicationContext):
        """Resume a paused recording."""
        try:
            await self.recorder.resume_recording(ctx.guild_id)
            await ctx.respond("Recording resumed!")
        except ValueError as e:
            await ctx.respond(str(e), ephemeral=True)

    @dnd.command(name="session", description="View a past session's summary")
    async def view_session(
        self,
        ctx: discord.ApplicationContext,
        session_id: discord.Option(str, "Session ID to view (leave empty for latest)", required=False) = None,
    ):
        """View the summary and details of a past session."""
        await ctx.defer()

        async with AsyncSessionLocal() as db:
            from sqlalchemy import select, desc

            if session_id:
                # Try to find by UUID
                try:
                    session_uuid = uuid.UUID(session_id)
                    query = select(Session).where(
                        Session.id == session_uuid,
                        Session.guild_id == ctx.guild_id,
                    )
                except ValueError:
                    # Try to find by name
                    query = select(Session).where(
                        Session.name == session_id,
                        Session.guild_id == ctx.guild_id,
                    ).order_by(desc(Session.started_at))

                result = await db.execute(query)
                session = result.scalar_one_or_none()
            else:
                # Get latest completed session
                query = (
                    select(Session)
                    .where(
                        Session.guild_id == ctx.guild_id,
                        Session.status == "completed",
                    )
                    .order_by(desc(Session.started_at))
                    .limit(1)
                )
                result = await db.execute(query)
                session = result.scalar_one_or_none()

            if not session:
                await ctx.followup.send(
                    "No session found. Make sure you have recorded at least one session.",
                    ephemeral=True,
                )
                return

            # Get summary if available
            summary_query = select(SessionSummary).where(SessionSummary.session_id == session.id)
            summary_result = await db.execute(summary_query)
            summary = summary_result.scalar_one_or_none()

            # Create embed
            embed = discord.Embed(
                title=f"Session: {session.name or 'Unnamed'}",
                color=self._status_color(session.status),
                timestamp=session.started_at,
            )

            # Duration
            if session.duration_seconds:
                hours, remainder = divmod(session.duration_seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                embed.add_field(
                    name="Duration",
                    value=f"{hours}h {minutes}m {seconds}s",
                    inline=True,
                )

            embed.add_field(name="Status", value=session.status.title(), inline=True)

            if summary:
                # Add summary content
                if summary.short_summary:
                    embed.add_field(
                        name="Summary",
                        value=summary.short_summary[:1024],
                        inline=False,
                    )

                if summary.key_events:
                    events_text = "\n".join(
                        f"- {event.get('description', 'Unknown')}"
                        for event in summary.key_events[:5]
                    )
                    embed.add_field(
                        name="Key Events",
                        value=events_text[:1024] or "None recorded",
                        inline=False,
                    )

                if summary.npcs_mentioned:
                    npcs_text = ", ".join(
                        npc.get("name", "Unknown")
                        for npc in summary.npcs_mentioned[:10]
                    )
                    embed.add_field(
                        name="NPCs",
                        value=npcs_text[:1024] or "None",
                        inline=True,
                    )

                if summary.locations_mentioned:
                    locations_text = ", ".join(
                        loc.get("name", "Unknown")
                        for loc in summary.locations_mentioned[:10]
                    )
                    embed.add_field(
                        name="Locations",
                        value=locations_text[:1024] or "None",
                        inline=True,
                    )
            elif session.status == "completed":
                embed.add_field(
                    name="Summary",
                    value="Summary not available for this session.",
                    inline=False,
                )
            elif session.status in ("processing", "transcribing", "analyzing"):
                embed.add_field(
                    name="Processing",
                    value=f"Session is currently being processed ({session.status})...",
                    inline=False,
                )
            elif session.status == "failed":
                embed.add_field(
                    name="Error",
                    value=session.error_message or "Unknown error occurred",
                    inline=False,
                )

            embed.set_footer(text=f"Session ID: {session.id}")

            await ctx.followup.send(embed=embed)

    @dnd.command(name="sessions", description="List recent sessions")
    async def list_sessions(self, ctx: discord.ApplicationContext):
        """List recent recording sessions."""
        await ctx.defer()

        async with AsyncSessionLocal() as db:
            from sqlalchemy import select, desc

            query = (
                select(Session)
                .where(Session.guild_id == ctx.guild_id)
                .order_by(desc(Session.started_at))
                .limit(10)
            )
            result = await db.execute(query)
            sessions = result.scalars().all()

            if not sessions:
                await ctx.followup.send(
                    "No sessions found. Use `/dnd start` to begin recording!",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title="Recent Sessions",
                color=discord.Color.blue(),
            )

            for session in sessions:
                duration_str = "In progress"
                if session.duration_seconds:
                    hours, remainder = divmod(session.duration_seconds, 3600)
                    minutes, _ = divmod(remainder, 60)
                    duration_str = f"{hours}h {minutes}m"

                status_emoji = {
                    "recording": "🔴",
                    "processing": "⏳",
                    "transcribing": "📝",
                    "analyzing": "🧠",
                    "completed": "✅",
                    "failed": "❌",
                }.get(session.status, "❓")

                embed.add_field(
                    name=f"{status_emoji} {session.name or 'Unnamed'}",
                    value=f"Duration: {duration_str}\nDate: {session.started_at.strftime('%Y-%m-%d')}",
                    inline=True,
                )

            embed.set_footer(text="Use /dnd session <name> to view details")

            await ctx.followup.send(embed=embed)

    def _status_color(self, status: str) -> discord.Color:
        """Get color for session status."""
        colors = {
            "recording": discord.Color.red(),
            "processing": discord.Color.orange(),
            "transcribing": discord.Color.yellow(),
            "analyzing": discord.Color.purple(),
            "completed": discord.Color.green(),
            "failed": discord.Color.dark_red(),
        }
        return colors.get(status, discord.Color.greyple())


def setup(bot: commands.Bot):
    """Setup function for loading the cog."""
    bot.add_cog(Recording(bot))
