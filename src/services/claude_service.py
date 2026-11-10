import anthropic
from typing import Optional
import json
import structlog

from src.config import settings

logger = structlog.get_logger()


# Prompt for session summary generation
SESSION_SUMMARY_SYSTEM = """You are a D&D session analyst. Your job is to read session transcripts and extract structured information about the GAME.

You will receive a transcript with speaker labels (labeled as Speaker A, Speaker B, etc., or by player name; one speaker is typically the DM/GM describing scenes and others are players). The transcript is automatic speech recognition output: expect misheard words, garbled names and no punctuation care. Do not guess identities you cannot support.

## Language
First decide which language the players mostly speak (for example Turkish or English). Write EVERY free-text field of your answer (summaries, descriptions, outcomes, significance notes) in that language. Players often mix in English game terms; keep these in their original English form, as the table does, and never translate them: dice and rolls (d20, d6, natural 20, advantage, disadvantage), mechanics (spell attack, saving throw, save DC, AC, HP, initiative, proficiency, concentration, short rest, long rest), and spell, monster, item, class and feat names (Fireball, Hold Person, Rogue, Sneak Attack). Do not translate or alter proper names of characters, NPCs and places. Write natural grammar in the chosen language around the English terms (for Turkish, for example, "Fireball büyüsü", "save DC 15", "d20 attı").

## Scope: the game only
Include only what happened in the game: the story and the fiction, in-character dialogue, exploration, social scenes, combat, loot, quests, and rules rulings that actually affected what happened. Leave out everything else, and do not mention that it was left out: real-life conversation (family, work, school, food, news, jokes unrelated to the game), scheduling, breaks, tech problems, and general banter. If a stretch of the transcript is out-of-game talk, ignore it completely. If you cannot tell whether something happened in the game, omit it unless it clearly changed the game state.

Respond ONLY with valid JSON matching the schema provided. Do not include any text outside the JSON."""

SESSION_SUMMARY_USER = """## Session Transcript
{transcript}

## Task
Analyze this D&D session transcript and extract the following (game content only, in the table's language, keeping English game terms as described):

1. **Language**: the language the players mostly speak (ISO 639-1 code, e.g. "tr" or "en")
2. **Session Summary**: 2-3 sentence overview
3. **Detailed Summary**: Narrative paragraph (200-400 words)
4. **Key Events**: Major plot points, discoveries, decisions
5. **Combat Encounters**: Battles that occurred (if any)
6. **NPCs Mentioned**: Characters that appeared or were mentioned
7. **Locations Mentioned**: Places visited or referenced

## Response Schema
```json
{{
  "language": "tr",
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
      "outcome": "victory, defeat, fled, or negotiated (in the table's language)",
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
        self.default_model = "claude-sonnet-5-5"

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

        # Call Claude API. Sonnet 5.5 always thinks, and thinking tokens count against
        # max_tokens, so leave generous headroom for the JSON answer. Streaming avoids HTTP
        # timeouts on long transcripts.
        # server-side fallback: if a safety classifier declines, the API re-runs the request
        # on a fallback model within the same call (D&D combat text can trip classifiers).
        with self.client.beta.messages.stream(
            model=model,
            max_tokens=32000,
            system=SESSION_SUMMARY_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": SESSION_SUMMARY_USER.format(transcript=transcript),
                }
            ],
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},
        ) as stream:
            message = stream.get_final_message()

        if message.stop_reason == "refusal":
            raise ValueError(f"Claude declined to analyze the transcript: {message.stop_details}")
        if message.stop_reason == "max_tokens":
            raise ValueError("Claude's response was cut off (max_tokens); raise the limit")

        # Parse response: content may start with a thinking block, so join the text blocks
        response_text = "".join(b.text for b in message.content if b.type == "text")

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
            language=result.get("language"),
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
        # Claude Sonnet 5.5 pricing: $2/M input, $10/M output
        input_cost = (prompt_tokens / 1_000_000) * 2.0
        output_cost = (completion_tokens / 1_000_000) * 10.0
        return input_cost + output_cost
