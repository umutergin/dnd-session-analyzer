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
        # Get the recording cog to check if we're recording
        recording_cog = self.get_cog("Recording")
        if not recording_cog:
            return

        # If the bot is in a voice channel
        if self.user and member.guild.voice_client:
            voice_client = member.guild.voice_client
            channel = voice_client.channel

            # If we're the only one left in the channel (excluding bots)
            if channel:
                human_members = [m for m in channel.members if not m.bot]
                if len(human_members) == 0:
                    logger.warning(
                        "All users left voice channel, stopping recording",
                        guild_id=member.guild.id,
                    )
                    # Auto-stop recording if everyone leaves
                    if recording_cog.recorder.is_recording(member.guild.id):
                        await recording_cog.recorder.stop_recording(member.guild.id)
                        await voice_client.disconnect()


def main():
    """Main entry point for the bot."""
    # Ensure audio storage directory exists
    settings.audio_storage_path.mkdir(parents=True, exist_ok=True)

    logger.info("Starting D&D Recorder Bot...")

    bot = DnDRecorderBot()
    bot.run(settings.discord_bot_token)


if __name__ == "__main__":
    main()
