import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

TOP = 10


class LeaderboardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="leaderboard", description="Top point-getters (most correct answers)")
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall(
            "SELECT user_id, COUNT(*) AS points FROM answers WHERE correct = 1 "
            "GROUP BY user_id ORDER BY points DESC LIMIT ?",
            (TOP,))
        if not rows:
            await interaction.response.send_message(
                "No points yet. Take some quizzes with /quiz to show up here.", ephemeral=True)
            return
        lines = []
        for place, r in enumerate(rows, 1):
            suffix = "" if r["points"] == 1 else "s"
            lines.append(f"{place}. <@{r['user_id']}> - {r['points']} point{suffix}")
        e = discord.Embed(title="Leaderboard", description="\n".join(lines),
                          colour=discord.Colour.blurple())
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="stats", description="How many packets and questions the bot has indexed")
    async def stats(self, interaction: discord.Interaction):
        counts = await self.bot.library.counts()
        await interaction.response.send_message(
            f"{counts['packets']} packets, {counts['questions']} questions "
            f"({counts['quizzable']} quizzable)", ephemeral=True)


async def setup(bot):
    await bot.add_cog(LeaderboardCog(bot))
