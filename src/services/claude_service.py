import anthropic
from typing import Optional
import json
import structlog

from src.config import settings

logger = structlog.get_logger()


# Prompt for session summary generation
SESSION_SUMMARY_SYSTEM = """You are a D&D session analyst. Your job is to read session transcripts and extract structured information.

You will receive a transcript with speaker labels (labeled as Speaker A, Speaker B, etc., where one speaker is typically the DM/GM describing scenes and others are players).

Respond ONLY with valid JSON matching the schema provided. Do not include any text outside the JSON."""

SESSION_SUMMARY_USER = """## Session Transcript
{transcript}

## Task
Analyze this D&D session transcript and extract:

1. **Session Summary**: 2-3 sentence overview
2. **Detailed Summary**: Narrative paragraph (200-400 words)
3. **Key Events**: Major plot points, discoveries, decisions
4. **Combat Encounters**: Battles that occurred (if any)
5. **NPCs Mentioned**: Characters that appeared or were mentioned
6. **Locations Mentioned**: Places visited or referenced

## Response Schema
```json
{{
  "short_summary": "2-3 sentence overview",
  "detailed_summary": "Full narrative summary paragraph",
  "key_events": [
    {{
      "description": "What happened",
      "participants": ["Character names involved"],
      "significance": "major or minor"
    }}
  ],
  "combat_encounters": [
    {{
      "enemies": ["Enemy names"],
      "outcome": "victory, defeat, fled, or negotiated",
      "notable_moments": ["Notable things that happened"]
    }}
  ],
  "npcs_mentioned": [
    {{
      "name": "NPC name",
      "description": "Brief description if provided",
      "role": "Their role (merchant, villain, ally, etc.)"
    }}
  ],
  "locations_mentioned": [
    {{
      "name": "Location name",
      "type": "city, dungeon, tavern, wilderness, etc.",
      "description": "Brief description if provided"
    }}
  ]
}}
```

Respond with ONLY the JSON, no additional text."""


class AnalysisResult:
    """Wrapper for LLM analysis results."""

    def __init__(
        self,
        short_summary: str,
        detailed_summary: str,
        key_events: list[dict],
        combat_encounters: list[dict],
        npcs_mentioned: list[dict],
        locations_mentioned: list[dict],
        prompt_tokens: int,
        completion_tokens: int,
        model: str,
    ):
        self.short_summary = short_summary
        self.detailed_summary = detailed_summary
        self.key_events = key_events
        self.combat_encounters = combat_encounters
        self.npcs_mentioned = npcs_mentioned
        self.locations_mentioned = locations_mentioned
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.model = model


class ClaudeService:
    """Service for analyzing transcripts using Claude."""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.default_model = "claude-sonnet-4-20250514"

    def analyze_session(
        self,
        transcript: str,
        model: Optional[str] = None,
    ) -> AnalysisResult:
        """
        Analyze a session transcript and extract summary and entities.

        Args:
            transcript: The full transcript text
            model: Optional model override

        Returns:
            AnalysisResult with summary and extracted entities
        """
        model = model or self.default_model

        logger.info(
            "Starting session analysis",
            model=model,
            transcript_length=len(transcript),
        )

        # Truncate very long transcripts to fit context window
        max_chars = 500000  # ~125K tokens, safe for Claude
        if len(transcript) > max_chars:
            logger.warning(
                "Truncating transcript",
                original_length=len(transcript),
                truncated_to=max_chars,
            )
            transcript = transcript[:max_chars] + "\n\n[Transcript truncated due to length]"

        # Call Claude API
        message = self.client.messages.create(
            model=model,
            max_tokens=4096,
            system=SESSION_SUMMARY_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": SESSION_SUMMARY_USER.format(transcript=transcript),
                }
            ],
        )

        # Parse response
        response_text = message.content[0].text

        try:
            # Try to extract JSON from the response
            result = json.loads(response_text)
        except json.JSONDecodeError:
            # Try to find JSON in the response
            import re
            json_match = re.search(r'\{[\s\S]*\}', response_text)
            if json_match:
                result = json.loads(json_match.group())
            else:
                logger.error("Failed to parse Claude response as JSON", response=response_text)
                raise ValueError("Failed to parse Claude response as JSON")

        logger.info(
            "Session analysis completed",
            prompt_tokens=message.usage.input_tokens,
            completion_tokens=message.usage.output_tokens,
        )

        return AnalysisResult(
            short_summary=result.get("short_summary", ""),
            detailed_summary=result.get("detailed_summary", ""),
            key_events=result.get("key_events", []),
            combat_encounters=result.get("combat_encounters", []),
            npcs_mentioned=result.get("npcs_mentioned", []),
            locations_mentioned=result.get("locations_mentioned", []),
            prompt_tokens=message.usage.input_tokens,
            completion_tokens=message.usage.output_tokens,
            model=model,
        )

    def estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """
        Estimate the cost for a Claude API call.

        Args:
            prompt_tokens: Number of input tokens
            completion_tokens: Number of output tokens

        Returns:
            Estimated cost in USD
        """
        # Claude Sonnet pricing: $3/M input, $15/M output
        input_cost = (prompt_tokens / 1_000_000) * 3.0
        output_cost = (completion_tokens / 1_000_000) * 15.0
        return input_cost + output_cost
