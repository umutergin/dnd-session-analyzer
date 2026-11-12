"""Player -> character mappings (stored per server in its default campaign)."""
from typing import Optional

from sqlalchemy import select

from src.database.models import Campaign, CharacterMapping

DEFAULT_CAMPAIGN_NAME = "Default"
DM_LABEL = "DM"


def get_default_campaign_sync(db, guild_id: int) -> Optional[Campaign]:
    return db.execute(
        select(Campaign).where(Campaign.guild_id == guild_id, Campaign.name == DEFAULT_CAMPAIGN_NAME)
    ).scalar_one_or_none()


async def get_or_create_default_campaign(db, guild_id: int) -> Campaign:
    campaign = (await db.execute(
        select(Campaign).where(Campaign.guild_id == guild_id, Campaign.name == DEFAULT_CAMPAIGN_NAME)
    )).scalar_one_or_none()
    if campaign is None:
        campaign = Campaign(guild_id=guild_id, name=DEFAULT_CAMPAIGN_NAME)
        db.add(campaign)
        await db.flush()
    return campaign


async def _find_mapping(db, campaign_id, user_id: int) -> Optional[CharacterMapping]:
    return (await db.execute(
        select(CharacterMapping).where(
            CharacterMapping.campaign_id == campaign_id,
            CharacterMapping.discord_user_id == user_id,
        )
    )).scalar_one_or_none()


async def set_character(db, guild_id: int, user_id: int, character: str,
                        char_class: Optional[str] = None, race: Optional[str] = None,
                        is_dm: bool = False) -> CharacterMapping:
    campaign = await get_or_create_default_campaign(db, guild_id)
    mapping = await _find_mapping(db, campaign.id, user_id)
    if mapping is None:
        mapping = CharacterMapping(campaign_id=campaign.id, discord_user_id=user_id,
                                   character_name=character)
        db.add(mapping)
    mapping.character_name = character
    mapping.character_class = char_class
    mapping.character_race = race
    mapping.is_dm = is_dm
    await db.commit()
    return mapping


async def remove_character(db, guild_id: int, user_id: int) -> bool:
    campaign = await get_or_create_default_campaign(db, guild_id)
    mapping = await _find_mapping(db, campaign.id, user_id)
    if mapping is None:
        await db.commit()
        return False
    await db.delete(mapping)
    await db.commit()
    return True


async def list_characters(db, guild_id: int) -> list[CharacterMapping]:
    campaign = await get_or_create_default_campaign(db, guild_id)
    rows = (await db.execute(
        select(CharacterMapping).where(CharacterMapping.campaign_id == campaign.id)
    )).scalars().all()
    await db.commit()
    return list(rows)


def describe(mapping: CharacterMapping) -> str:
    """'Leon (Tiefling Wizard)' / 'DM'."""
    if mapping.is_dm:
        return DM_LABEL
    extra = " ".join(x for x in (mapping.character_race, mapping.character_class) if x)
    return f"{mapping.character_name} ({extra})" if extra else mapping.character_name


def roster_for_session_sync(db, guild_id: int) -> dict:
    """discord_user_id -> CharacterMapping for the server's default campaign (empty if none)."""
    campaign = get_default_campaign_sync(db, guild_id)
    if campaign is None:
        return {}
    rows = db.execute(
        select(CharacterMapping).where(CharacterMapping.campaign_id == campaign.id)
    ).scalars().all()
    return {r.discord_user_id: r for r in rows}
