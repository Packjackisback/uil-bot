
import argparse
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from database import Database
from packets import Library
from quiz_core import AnswerButton, FreeAnswerButton, ReportButton

log = logging.getLogger("uilbot")
EXTENSIONS = ("cogs.packets_cog", "cogs.quiz_cog", "cogs.schedule_cog", "cogs.leaderboard_cog")


def allowed_guild_ids() -> set[int]:
    raw = config.ALLOWED_GUILD_IDS or ""
    return {int(g) for g in raw.split(",") if g.strip().isdigit()}


class UILBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default(),
                         allowed_mentions=discord.AllowedMentions.none())
        self.db = Database(config.DB_PATH)
        self.library = Library(self.db, config.PACKETS_DIR)
        self.cooldowns: dict[int, float] = {}
        self.tree.on_error = self.on_tree_error

    async def setup_hook(self):
        await self.db.connect()
        stats = await self.library.reindex()
        counts = await self.library.counts()
        log.info("library: %s packets, %s questions (%s quizzable); index took %ss",
                 counts["packets"], counts["questions"], counts["quizzable"], stats["seconds"])
        self.add_dynamic_items(AnswerButton, FreeAnswerButton, ReportButton)
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        if config.SYNC_COMMANDS:
            if config.DEV_GUILD_ID:
                guild = discord.Object(id=int(config.DEV_GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()

    async def on_ready(self):
        log.info("logged in as %s in %s servers", self.user, len(self.guilds))
        await self._leave_disallowed_guilds()

    async def on_guild_join(self, guild: discord.Guild):
        allowed = allowed_guild_ids()
        if allowed and guild.id not in allowed:
            log.info("leaving disallowed guild %s (%s)", guild.name, guild.id)
            try:
                await guild.leave()
            except discord.HTTPException:
                log.exception("couldn't leave disallowed guild %s", guild.id)

    async def _leave_disallowed_guilds(self):
        """Leave any guilds already joined that aren't in the allow-list."""
        allowed = allowed_guild_ids()
        if not allowed:
            return
        for guild in [g for g in self.guilds if g.id not in allowed]:
            log.info("leaving disallowed guild %s (%s)", guild.name, guild.id)
            try:
                await guild.leave()
            except discord.HTTPException:
                log.exception("couldn't leave disallowed guild %s", guild.id)

    async def close(self):
        await super().close()
        await self.db.close()

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need the **Manage Server** permission to do that."
        else:
            log.exception("command error", exc_info=error)
            msg = "Something went wrong. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


async def reindex_only():
    db = Database(config.DB_PATH)
    await db.connect()
    lib = Library(db, config.PACKETS_DIR)
    print(await lib.reindex())
    print(await lib.counts())
    await db.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reindex-only", action="store_true", help="scan data/packets, print stats and exit")
    args = ap.parse_args()
    discord.utils.setup_logging(level=logging.INFO)
    if args.reindex_only:
        asyncio.run(reindex_only())
        return
    if not config.TOKEN:
        raise SystemExit("Set DISCORD_TOKEN (in the environment or a .env file). See README.md.")
    UILBot().run(config.TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
