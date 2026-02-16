# D&D Session Recorder Bot

A Discord bot that records your D&D sessions, transcribes them with speaker identification, and uses AI to generate session summaries, extract NPCs, locations, and key events.

## Features

- **Voice Recording**: Join your voice channel and record the entire session
- **Speaker Identification**: Automatically identifies different speakers in the recording
- **AI Transcription**: Uses AssemblyAI for accurate speech-to-text with speaker diarization
- **Smart Summaries**: Claude AI generates session summaries, extracts NPCs, locations, and key events
- **Session History**: Browse and search through past sessions

## Commands

| Command | Description |
|---------|-------------|
| `/dnd start [name]` | Start recording the current voice channel |
| `/dnd stop` | Stop recording and process the session |
| `/dnd status` | Check current recording status |
| `/dnd pause` | Pause the recording |
| `/dnd resume` | Resume a paused recording |
| `/dnd session [id]` | View a past session's summary |
| `/dnd sessions` | List recent sessions |

## Prerequisites

- Python 3.11+
- FFmpeg (for audio processing)
- Docker (for PostgreSQL and Redis)
- Discord Bot Token
- AssemblyAI API Key
- Anthropic (Claude) API Key

## Quick Start

1. **Clone and install dependencies:**
   ```bash
   cd dnd-recorder-bot
   pip install -e .
   ```

2. **Set up environment variables:**
   ```bash
   cp .env.example .env
   # Edit .env with your API keys
   ```

3. **Start the database services:**
   ```bash
   docker-compose up -d
   ```

4. **Initialize the database:**
   ```bash
   python scripts/init_db.py
   ```

5. **Run the bot:**
   ```bash
   python -m src.bot.main
   ```

6. **Start the Celery worker (in a separate terminal):**
   ```bash
   celery -A src.processing.celery_app worker -l info
   ```

## Configuration

Create a `.env` file with:

```env
DISCORD_BOT_TOKEN=your_discord_bot_token
ASSEMBLYAI_API_KEY=your_assemblyai_key
ANTHROPIC_API_KEY=your_anthropic_key
DATABASE_URL=postgresql://dnd:dndpassword@localhost:5432/dnd_recorder
REDIS_URL=redis://localhost:6379/0
```

## Cost Estimates

Per 3-hour session:
- AssemblyAI transcription: ~$0.45
- Claude analysis: ~$0.20
- **Total: ~$0.65 per session**

## Architecture

```
Discord Bot (Pycord)
    ↓
Voice Recording (per-speaker WAV)
    ↓
Celery Task Queue
    ↓
Audio Merge (FFmpeg) → AssemblyAI (Transcription) → Claude (Analysis)
    ↓
PostgreSQL (Storage)
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Format code
black src/
ruff check src/
```

## License

MIT
