import asyncio
import logging
import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import quiz_core
from cogs._common import QTYPE_CHOICES, packet_autocomplete, season_autocomplete

log = logging.getLogger(__name__)

MAX_QUESTION_NUMBER = 500


def _parse_question_range(text: str) -> list[int] | None:
    text = text.strip()
    if "-" in text:
        parts = text.split("-", 1)
        if len(parts) != 2:
            return None
        try:
            lo, hi = int(parts[0].strip()), int(parts[1].strip())
        except ValueError:
            return None
        if lo < 1 or hi > MAX_QUESTION_NUMBER or lo > hi:
            return None
        return list(range(lo, hi + 1))
    try:
        n = int(text)
    except ValueError:
        return None
    if n < 1 or n > MAX_QUESTION_NUMBER:
        return None
    return [n]


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
                           number="A specific question number or range (e.g. 5 or 5-15), across all packets if no packet is given",
                           season="Only ask from this season (e.g. 2324 = 2023-24)",
                           qtype="Multiple choice or free response")
    @app_commands.rename(qtype="type")
    @app_commands.choices(qtype=QTYPE_CHOICES)
    @app_commands.autocomplete(packet=packet_autocomplete, season=season_autocomplete)
    async def quiz(self, interaction: discord.Interaction, packet: str | None = None,
                   number: str | None = None, season: str | None = None,
                   qtype: app_commands.Choice[str] | None = None):
        lib = self.bot.library
        if packet is not None and await lib.packet(packet) is None:
            await interaction.response.send_message(f"I don't have a packet called `{packet}`.", ephemeral=True)
            return

        qt = qtype.value if qtype else None
        if number is not None:
            nums = _parse_question_range(number)
            if nums is None:
                await interaction.response.send_message(
                    "Invalid question number or range. Use a single number (e.g. `5`) or a range (e.g. `5-15`).",
                    ephemeral=True)
                return
            q = None
            for _ in range(min(8, len(nums))):
                n = random.choice(nums)
                if packet is not None:
                    candidate = await lib.get_question(packet, n)
                else:
                    candidates = await lib.questions_by_qid(n, season=season, qtype=qt)
                    candidate = random.choice(candidates) if candidates else None
                if candidate is not None and candidate.answer is not None and lib.image_paths(candidate):
                    q = candidate
                    break
            if q is None:
                if packet is not None:
                    await interaction.response.send_message(
                        f"`{packet}` has no available questions in that range.", ephemeral=True)
                else:
                    await interaction.response.send_message(
                        "No available questions with that number across packets.", ephemeral=True)
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
