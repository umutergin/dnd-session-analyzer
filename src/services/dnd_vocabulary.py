"""
D&D vocabulary for transcription boost.

These terms are used to improve recognition of English D&D terminology
when transcribing Turkish D&D sessions with AssemblyAI.

Add or remove terms based on your campaign's needs.
"""

# Core game mechanics
MECHANICS = [
    "hit points",
    "HP",
    "armor class",
    "AC",
    "spell slot",
    "spell slots",
    "saving throw",
    "saving throws",
    "skill check",
    "ability check",
    "attack roll",
    "damage roll",
    "initiative",
    "advantage",
    "disadvantage",
    "proficiency",
    "proficiency bonus",
    "modifier",
    "bonus action",
    "reaction",
    "action",
    "movement",
    "concentration",
    "short rest",
    "long rest",
    "death save",
    "death saving throw",
    "stabilize",
    "critical hit",
    "critical miss",
    "natural 20",
    "nat 20",
    "natural 1",
    "nat 1",
]

# Dice terminology
DICE = [
    "d4",
    "d6",
    "d8",
    "d10",
    "d12",
    "d20",
    "d100",
    "percentile",
    "roll",
    "reroll",
]

# Character classes
CLASSES = [
    "barbarian",
    "bard",
    "cleric",
    "druid",
    "fighter",
    "monk",
    "paladin",
    "ranger",
    "rogue",
    "sorcerer",
    "warlock",
    "wizard",
    "artificer",
    # Subclasses (common ones)
    "berserker",
    "champion",
    "assassin",
    "thief",
    "evocation",
    "necromancy",
    "divination",
]

# Races
RACES = [
    "human",
    "elf",
    "dwarf",
    "halfling",
    "gnome",
    "half-elf",
    "half-orc",
    "tiefling",
    "dragonborn",
    "aasimar",
    "goliath",
    "tabaxi",
    "kenku",
    "firbolg",
]

# Abilities
ABILITIES = [
    "strength",
    "dexterity",
    "constitution",
    "intelligence",
    "wisdom",
    "charisma",
    "STR",
    "DEX",
    "CON",
    "INT",
    "WIS",
    "CHA",
]

# Common spells
SPELLS = [
    # Cantrips
    "fire bolt",
    "eldritch blast",
    "sacred flame",
    "toll the dead",
    "minor illusion",
    "prestidigitation",
    "mage hand",
    "light",
    "guidance",
    "vicious mockery",
    # Level 1
    "magic missile",
    "shield",
    "cure wounds",
    "healing word",
    "guiding bolt",
    "bless",
    "hex",
    "hunter's mark",
    "thunderwave",
    "sleep",
    "charm person",
    "detect magic",
    "identify",
    "mage armor",
    "feather fall",
    # Level 2
    "spiritual weapon",
    "hold person",
    "misty step",
    "scorching ray",
    "shatter",
    "invisibility",
    "suggestion",
    "mirror image",
    "darkness",
    # Level 3
    "fireball",
    "lightning bolt",
    "counterspell",
    "dispel magic",
    "fly",
    "haste",
    "slow",
    "spirit guardians",
    "revivify",
    "hypnotic pattern",
    # Higher level
    "polymorph",
    "banishment",
    "dimension door",
    "greater invisibility",
    "wall of fire",
    "cone of cold",
    "hold monster",
    "mass cure wounds",
    "raise dead",
    "resurrection",
    "power word kill",
    "wish",
    "meteor swarm",
]

# Conditions
CONDITIONS = [
    "blinded",
    "charmed",
    "deafened",
    "frightened",
    "grappled",
    "incapacitated",
    "invisible",
    "paralyzed",
    "petrified",
    "poisoned",
    "prone",
    "restrained",
    "stunned",
    "unconscious",
    "exhaustion",
]

# Combat terms
COMBAT = [
    "melee",
    "ranged",
    "opportunity attack",
    "attack of opportunity",
    "flanking",
    "cover",
    "half cover",
    "three-quarters cover",
    "total cover",
    "surprise",
    "surprise round",
    "sneak attack",
    "smite",
    "divine smite",
    "rage",
    "wild shape",
    "lay on hands",
    "second wind",
    "action surge",
    "cunning action",
    "uncanny dodge",
    "evasion",
]

# Items and equipment
ITEMS = [
    "longsword",
    "shortsword",
    "greatsword",
    "rapier",
    "dagger",
    "handaxe",
    "battleaxe",
    "greataxe",
    "warhammer",
    "maul",
    "shortbow",
    "longbow",
    "crossbow",
    "light crossbow",
    "heavy crossbow",
    "quarterstaff",
    "spear",
    "javelin",
    "shield",
    "plate armor",
    "chain mail",
    "leather armor",
    "studded leather",
    "potion",
    "potion of healing",
    "scroll",
    "wand",
    "staff",
    "ring",
    "amulet",
    "cloak",
    "boots",
    "bag of holding",
]

# Monsters (common ones)
MONSTERS = [
    "goblin",
    "kobold",
    "orc",
    "ogre",
    "troll",
    "giant",
    "dragon",
    "wyvern",
    "beholder",
    "mind flayer",
    "illithid",
    "lich",
    "vampire",
    "werewolf",
    "zombie",
    "skeleton",
    "ghost",
    "wraith",
    "demon",
    "devil",
    "elemental",
    "golem",
    "mimic",
    "owlbear",
    "displacer beast",
    "gelatinous cube",
]

# DM/Game terms
GAME_TERMS = [
    "dungeon master",
    "DM",
    "game master",
    "GM",
    "player character",
    "PC",
    "non-player character",
    "NPC",
    "campaign",
    "session",
    "encounter",
    "dungeon",
    "quest",
    "loot",
    "treasure",
    "experience",
    "XP",
    "level up",
    "multiclass",
]


def get_all_vocabulary() -> list[str]:
    """Get all D&D vocabulary terms as a flat list."""
    all_terms = (
        MECHANICS
        + DICE
        + CLASSES
        + RACES
        + ABILITIES
        + SPELLS
        + CONDITIONS
        + COMBAT
        + ITEMS
        + MONSTERS
        + GAME_TERMS
    )
    # Remove duplicates while preserving order
    seen = set()
    unique_terms = []
    for term in all_terms:
        if term.lower() not in seen:
            seen.add(term.lower())
            unique_terms.append(term)
    return unique_terms


# Pre-computed list for easy import
DND_VOCABULARY = get_all_vocabulary()
