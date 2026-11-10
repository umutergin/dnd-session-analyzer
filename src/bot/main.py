import asyncio
import discord
from discord.ext import commands
import structlog
from pathlib import Path

from src.config import settings

# Configure structured logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(settings.log_level),
)

logger = structlog.get_logger()

AUTO_STOP_GRACE_SECONDS = 120


class DnDRecorderBot(commands.Bot):
    """Discord bot for recording and summarizing D&D sessions."""

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            description="D&D Session Recorder - Record, transcribe, and summarize your sessions!",
            # Debug guilds for instant slash command sync (remove in production)
            debug_guilds=[940893054888472586],
        )

        self._auto_stop_tasks: dict[int, asyncio.Task] = {}

        # Load cogs
        logger.info("Loading cogs...")
        self.load_extension("src.bot.cogs.recording")
        logger.info("Cogs loaded successfully")

    async def on_ready(self):
        """Called when the bot is ready."""
        logger.info(
            "Bot is ready!",
            user=str(self.user),
            guilds=len(self.guilds),
        )

        # Sync slash commands
        logger.info("Syncing slash commands...")
        await self.sync_commands()
        logger.info("Slash commands synced!")

        # Set bot status
        activity = discord.Activity(
            type=discord.ActivityType.listening,
            name="your D&D sessions | /dnd start",
        )
        await self.change_presence(activity=activity)

    async def on_guild_join(self, guild: discord.Guild):
        """Called when the bot joins a new server."""
        logger.info("Joined new guild", guild=guild.name, guild_id=guild.id)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        """Handle voice state changes (e.g., disconnect if alone)."""
        guild = member.guild
        recording_cog = self.get_cog("Recording")
        if not recording_cog or not recording_cog.recorder.is_recording(guild.id):
            return
        if member.id == self.user.id:
            return  # our own state changes (e.g. the disconnect after stopping)

        if self._humans_in_voice(guild) == 0:
            if guild.id not in self._auto_stop_tasks:
                logger.info("Voice channel empty, auto-stop pending",
                            grace_seconds=AUTO_STOP_GRACE_SECONDS, guild_id=guild.id)
                self._auto_stop_tasks[guild.id] = asyncio.create_task(self._auto_stop(guild))
        else:
            task = self._auto_stop_tasks.pop(guild.id, None)
            if task:
                task.cancel()

    def _humans_in_voice(self, guild: discord.Guild) -> int:
        """People in the recording channel, not counting this bot or known bots.

        Uses voice state ids because member objects may not be cached without the members
        intent; an id we can't resolve counts as a person.
        """
        vc = guild.voice_client
        if not vc or not vc.channel:
            return 0
        count = 0
        for user_id in vc.channel.voice_states:
            if user_id == self.user.id:
                continue
            m = guild.get_member(user_id)
            if m is None or not m.bot:
                count += 1
        return count

    async def _auto_stop(self, guild: discord.Guild):
        """After a grace period with nobody present, finish the recording like /dnd stop."""
        try:
            await asyncio.sleep(AUTO_STOP_GRACE_SECONDS)
            cog = self.get_cog("Recording")
            if not cog.recorder.is_recording(guild.id) or self._humans_in_voice(guild) > 0:
                return
            logger.warning("Everyone left the voice channel, stopping recording",
                           guild_id=guild.id)
            session, duration, included, _ = await cog.finalize_recording(guild)
            channel = guild.get_channel(session.channel_id)
            if channel:
                try:
                    await channel.send(
                        "Everyone left, so I stopped recording "
                        f"({int(duration.total_seconds() // 60)} min, {included} speaker(s)). "
                        "Processing has started."
                    )
                except discord.DiscordException:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Auto-stop failed", error=str(e), guild_id=guild.id)
        finally:
            self._auto_stop_tasks.pop(guild.id, None)


def main():
    """Main entry point for the bot."""
    # Ensure audio storage directory exists
    settings.audio_storage_path.mkdir(parents=True, exist_ok=True)

    logger.info("Starting D&D Recorder Bot...")

    bot = DnDRecorderBot()
    bot.run(settings.discord_bot_token)


if __name__ == "__main__":
    main()
