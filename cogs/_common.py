import re

from discord import app_commands

QTYPE_CHOICES = [app_commands.Choice(name="Multiple choice", value="choice"),
                 app_commands.Choice(name="Free response", value="free")]


def season_label(season: str) -> str:
    """'2324' -> '2023-24'"""
    return f"20{season[:2]}-{season[2:]}" if re.fullmatch(r"\d{4}", season or "") else str(season)


def packet_label(row) -> str:
    return f"{row['id']} - {row['date']}" if row["date"] else str(row["id"])


async def packet_autocomplete(interaction, current: str):
    rows = await interaction.client.library.autocomplete_packets(current)
    return [app_commands.Choice(name=packet_label(r)[:100], value=r["id"]) for r in rows]


async def season_autocomplete(interaction, current: str):
    seasons = await interaction.client.library.seasons(current)
    return [app_commands.Choice(name=season_label(s), value=s) for s in seasons]


_DURATION = re.compile(r"\s*(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m)\s*(?:,|and)?\s*", re.I)
_UNIT = {"d": 86400, "h": 3600, "m": 60}


def parse_duration(text: str):
    text = (text or "").strip().lower()
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        return int(text) * 60
    pos, total = 0, 0.0
    while pos < len(text):
        m = _DURATION.match(text, pos)
        if not m:
            return None
        total += float(m.group(1)) * _UNIT[m.group(2)[0].lower()]
        pos = m.end()
    return int(total) or None


def fmt_duration(seconds: int) -> str:
    seconds = int(seconds)
    parts = []
    for name, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            parts.append(f"{seconds // size}{name}")
            seconds %= size
    return " ".join(parts) or f"{seconds}s"
