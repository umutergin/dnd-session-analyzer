from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
from pathlib import Path


class Settings(BaseSettings):
    # Discord
    discord_bot_token: str = Field(..., description="Discord bot token")

    # Database
    database_url: str = Field(
        default="postgresql://dnd:dndpassword@localhost:5432/dnd_recorder"
    )

    # Redis
    redis_url: str = Field(default="redis://localhost:6379/0")

    # AssemblyAI
    assemblyai_api_key: str = Field(..., description="AssemblyAI API key")

    # Anthropic
    anthropic_api_key: str = Field(..., description="Anthropic API key")

    # Storage
    audio_storage_path: Path = Field(default=Path("./data/audio"))

    # Logging
    log_level: str = Field(default="INFO")

    # Bot/Music Bot Exclusion
    # Skip recording audio from Discord bots (music bots, etc.)
    exclude_bots_from_recording: bool = Field(default=True)
    # Additional user IDs to exclude (comma-separated in .env)
    excluded_user_ids: list[int] = Field(default_factory=list)
    # Name patterns to exclude (case-insensitive, comma-separated)
    excluded_name_patterns: list[str] = Field(
        default_factory=lambda: [
            "rythm", "groovy", "fredboat", "hydra", "jockie",
            "musicbox", "matchbox", "mee6", "dyno", "carl-bot",
        ]
    )

    @field_validator("excluded_user_ids", mode="before")
    @classmethod
    def parse_user_ids(cls, v):
        if isinstance(v, str):
            if not v.strip():
                return []
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v or []

    @field_validator("excluded_name_patterns", mode="before")
    @classmethod
    def parse_name_patterns(cls, v):
        if isinstance(v, str):
            if not v.strip():
                # Return default patterns if empty
                return [
                    "rythm", "groovy", "fredboat", "hydra", "jockie",
                    "musicbox", "matchbox", "mee6", "dyno", "carl-bot",
                ]
            return [x.strip() for x in v.split(",") if x.strip()]
        return v or []

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
