import asyncio
import logging
import math

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from cogs._common import packet_autocomplete, season_autocomplete, season_label

log = logging.getLogger(__name__)
PAGE_SIZE = 10


def _line(r) -> str:
    quiz = f"{r['answered_count']}/{r['question_count']} answered" if r["answered_count"] else "no key yet"
    return f"`{r['id']}` - {r['date'] or '-'} - {r['question_count']} questions - {quiz}"


class PacketPager(discord.ui.View):
    def __init__(self, library, owner_id, search, season):
        super().__init__(timeout=180)
        self.library, self.owner_id, self.search, self.season = library, owner_id, search, season
        self.page, self.total, self.message = 0, 0, None

    async def embed(self) -> discord.Embed:
        rows, self.total = await self.library.search_packets(self.search, self.season, PAGE_SIZE, self.page * PAGE_SIZE)
        pages = max(1, math.ceil(self.total / PAGE_SIZE))
        self.page = min(self.page, pages - 1)
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= pages - 1
        desc = "\n".join(_line(r) for r in rows) or "No packets match."
        e = discord.Embed(title="Packets", description=desc, colour=discord.Colour.blurple())
        filt = " - ".join(x for x in (f'search "{self.search}"' if self.search else "",
                                      f"season {season_label(self.season)}" if self.season else "") if x)
        e.set_footer(text=f"Page {self.page + 1}/{pages} - {self.total} packets" + (f" - {filt}" if filt else ""))
        return e

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Run `/packets` yourself to browse.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="<", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await interaction.response.edit_message(embed=await self.embed(), view=self)

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await interaction.response.edit_message(embed=await self.embed(), view=self)


class PacketsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.rescan.start()

    async def cog_unload(self):
        self.rescan.cancel()

    @tasks.loop(minutes=config.REINDEX_MINUTES)
    async def rescan(self):
        try:
            await self.bot.library.reindex()
        except Exception:
            log.exception("periodic reindex failed")

    @rescan.before_loop
    async def _wait(self):
        await asyncio.sleep(config.REINDEX_MINUTES * 60)

    @app_commands.command(name="packets", description="Browse the available packets")
    @app_commands.describe(search="Text to look for in the packet id, title or date",
                           season="Only this season (e.g. 2324 = 2023-24)")
    @app_commands.autocomplete(season=season_autocomplete)
    async def packets(self, interaction: discord.Interaction, search: str | None = None, season: str | None = None):
        view = PacketPager(self.bot.library, interaction.user.id, search, season)
        await interaction.response.send_message(embed=await view.embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @app_commands.command(name="packet", description="Show details for one packet")
    @app_commands.describe(packet="Packet id or date")
    @app_commands.autocomplete(packet=packet_autocomplete)
    async def packet(self, interaction: discord.Interaction, packet: str):
        r = await self.bot.library.packet(packet)
        if r is None:
            await interaction.response.send_message(f"I don't have a packet called `{packet}`. "
                                                    "Try `/packets` to browse.", ephemeral=True)
            return
        e = discord.Embed(title=r["title"] or f"Packet {r['id']}", colour=discord.Colour.blurple())
        e.add_field(name="Packet id", value=f"`{r['id']}`")
        e.add_field(name="Date", value=r["date"] or "-")
        if r["season"]:
            e.add_field(name="Season", value=season_label(r["season"]))
        e.add_field(name="Questions", value=str(r["question_count"]))
        e.add_field(name="Multiple choice / free", value=f"{r['question_count'] - r['free_count']} / {r['free_count']}")
        e.add_field(name="Multi-part groups", value=str(r["group_count"]))
        if r["answered_count"]:
            e.add_field(name="Answer key", value=f"{r['answered_count']}/{r['question_count']} questions", inline=False)
            e.set_footer(text=f"/quiz packet:{r['id']} - /quiz packet:{r['id']} number:<n> for a specific question")
        else:
            e.add_field(name="Answer key", value="Not loaded yet, so this packet can't be quizzed.", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="reindex", description="(Bot owner) Rescan the packets folder now")
    @app_commands.default_permissions(administrator=True)
    async def reindex(self, interaction: discord.Interaction):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("Only the bot owner can do that.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        s = await self.bot.library.reindex()
        c = await self.bot.library.counts()
        await interaction.followup.send(
            f"Reindexed in {s['seconds']}s: +{s['added']} new, {s['updated']} updated, {s['removed']} removed, "
            f"{s['skipped']} unreadable.\nLibrary: {c['packets']} packets, {c['questions']} questions "
            f"({c['quizzable']} quizzable).", ephemeral=True)


async def setup(bot):
    await bot.add_cog(PacketsCog(bot))
