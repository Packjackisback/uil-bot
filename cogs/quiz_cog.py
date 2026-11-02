import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import quiz_core
from cogs._common import QTYPE_CHOICES, packet_autocomplete, season_autocomplete

log = logging.getLogger(__name__)


class QuizCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.expire_loop.start()

    async def cog_unload(self):
        self.expire_loop.cancel()

    @tasks.loop(seconds=20)
    async def expire_loop(self):
        try:
            now = time.time()
            for uid in [u for u, t in self.bot.cooldowns.items() if t < now]:
                self.bot.cooldowns.pop(uid, None)
            rows = await self.bot.db.fetchall(
                "SELECT * FROM active_questions WHERE closed = 0 AND expires_at <= ? ORDER BY expires_at LIMIT 50",
                (now,))
            for row in rows:
                await quiz_core.close_question(self.bot, row)
                await asyncio.sleep(0.3)
        except Exception:
            log.exception("expiry loop failed")

    @expire_loop.before_loop
    async def _ready(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="quiz", description="Get a practice question (random, or pick a packet / question)")
    @app_commands.describe(packet="Only ask from this packet",
                           number="A specific question number in that packet",
                           season="Only ask from this season (e.g. 2324 = 2023-24)",
                           qtype="Multiple choice or free response")
    @app_commands.rename(qtype="type")
    @app_commands.choices(qtype=QTYPE_CHOICES)
    @app_commands.autocomplete(packet=packet_autocomplete, season=season_autocomplete)
    async def quiz(self, interaction: discord.Interaction, packet: str | None = None,
                   number: app_commands.Range[int, 1, 500] | None = None, season: str | None = None,
                   qtype: app_commands.Choice[str] | None = None):
        lib = self.bot.library
        if number is not None and packet is None:
            await interaction.response.send_message("Pick a `packet` to go with that question `number`.", ephemeral=True)
            return
        if packet is not None and await lib.packet(packet) is None:
            await interaction.response.send_message(f"I don't have a packet called `{packet}`.", ephemeral=True)
            return

        qt = qtype.value if qtype else None
        if number is not None:
            q = await lib.get_question(packet, number)
            if q is None:
                await interaction.response.send_message(f"`{packet}` has no question {number}.", ephemeral=True)
                return
            if q.answer is None or not lib.image_paths(q):
                await interaction.response.send_message("That question isn't available to quiz yet "
                                                        "(no answer key or image).", ephemeral=True)
                return
        else:
            q = await quiz_core.pick_valid(self.bot, f"u:{interaction.user.id}", packet_id=packet, season=season, qtype=qt)
            if q is None:
                hint = " That packet may not have its answer key loaded yet." if packet else ""
                await interaction.response.send_message(f"No quizzable questions match those filters.{hint}", ephemeral=True)
                return

        await quiz_core.send_via_interaction(interaction, q, config.QUIZ_WINDOW)


async def setup(bot):
    await bot.add_cog(QuizCog(bot))
