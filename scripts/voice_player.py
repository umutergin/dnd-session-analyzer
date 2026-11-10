"""
Test helper: a throwaway bot that joins a voice channel and plays an audio file, so the
recorder can be tested without other people. Run one instance per simulated speaker, each
with its own bot token (set in .env, e.g. PLAYER_BOT_TOKEN, PLAYER_BOT_TOKEN_2).

    python scripts/voice_player.py --token-env PLAYER_BOT_TOKEN --channel-id 123 file.flac [--loop]
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

p = argparse.ArgumentParser()
p.add_argument("file", type=Path)
p.add_argument("--channel-id", type=int, required=True, help="Voice channel ID")
p.add_argument("--token-env", default="PLAYER_BOT_TOKEN")
p.add_argument("--loop", action="store_true", help="Replay the file until stopped")
p.add_argument("--start-delay", type=float, default=0, help="Seconds to wait before playing")
args = p.parse_args()

token = os.environ.get(args.token_env)
if not token:
    sys.exit(f"{args.token_env} is not set in .env")
if not args.file.is_file():
    sys.exit(f"Not found: {args.file}")

bot = discord.Bot(intents=discord.Intents.default())


@bot.event
async def on_ready():
    channel = bot.get_channel(args.channel_id) or await bot.fetch_channel(args.channel_id)
    vc = await channel.connect()
    print(f"[{bot.user}] joined {channel.name}", flush=True)
    await asyncio.sleep(args.start_delay)
    while True:
        done = asyncio.Event()
        loop = asyncio.get_running_loop()
        vc.play(discord.FFmpegPCMAudio(str(args.file)),
                after=lambda e: loop.call_soon_threadsafe(done.set))
        print(f"[{bot.user}] playing {args.file.name}", flush=True)
        await done.wait()
        if not args.loop or not vc.is_connected():
            break
    print(f"[{bot.user}] finished, leaving", flush=True)
    await vc.disconnect()
    await bot.close()


bot.run(token)
