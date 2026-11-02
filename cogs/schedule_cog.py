import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import quiz_core
from cogs._common import (QTYPE_CHOICES, fmt_duration, packet_autocomplete, parse_duration,
                          season_autocomplete, season_label)

log = logging.getLogger(__name__)
DISABLE_AFTER_FAILURES = 5
MANAGE = app_commands.checks.has_permissions(manage_guild=True)


class ScheduleCog(commands.Cog):
    schedule = app_commands.Group(name="schedule", description="Auto-post questions to a channel",
                                  guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot):
        self.bot = bot
        self.tick.start()

    async def cog_unload(self):
        self.tick.cancel()


    @tasks.loop(seconds=15)
    async def tick(self):
        try:
            rows = await self.bot.db.fetchall(
                "SELECT * FROM schedules WHERE enabled = 1 AND next_run <= ? ORDER BY next_run LIMIT 25", (time.time(),))
            for row in rows:
                await self._run(row)
                await asyncio.sleep(0.5)
        except Exception:
            log.exception("scheduler tick failed")

    @tick.before_loop
    async def _ready(self):
        await self.bot.wait_until_ready()

    async def _fail(self, row, reason):
        failures = row["failures"] + 1
        enabled = int(failures < DISABLE_AFTER_FAILURES)
        log.warning("schedule for channel %s failed (%s) [%s/%s]", row["channel_id"], reason, failures, DISABLE_AFTER_FAILURES)
        await self.bot.db.execute(
            "UPDATE schedules SET failures = ?, enabled = ?, next_run = ? WHERE channel_id = ?",
            (failures, enabled, time.time() + min(row["interval_s"], 300), row["channel_id"]))

    async def _run(self, row):
        bot = self.bot
        try:
            q = await quiz_core.pick_valid(bot, f"c:{row['channel_id']}", packet_id=row["packet_id"],
                                           season=row["season"], qtype=row["qtype"])
            if q is None:
                await self._fail(row, "no quizzable questions match this schedule's filters")
                return
            dest = bot.get_partial_messageable(row["channel_id"], guild_id=row["guild_id"])
            await quiz_core.send_to_messageable(bot, dest, q, window_s=row["window_s"],
                                                guild_id=row["guild_id"], channel_id=row["channel_id"])
            await bot.db.execute("UPDATE schedules SET next_run = ?, failures = 0 WHERE channel_id = ?",
                                 (time.time() + row["interval_s"], row["channel_id"]))
        except (discord.Forbidden, discord.NotFound) as e:
            await self._fail(row, f"{type(e).__name__}: missing channel or permission")
        except discord.HTTPException as e:
            await self._fail(row, f"HTTP {e.status}")
        except Exception:
            log.exception("unexpected error posting scheduled question")
            await self._fail(row, "unexpected error")


    @schedule.command(name="setup", description="Post a random question to a channel at a regular interval")
    @app_commands.describe(channel="Where to post",
                           interval="How often, e.g. 30m, 2h, 1d, 1h30m (a bare number means minutes)",
                           packet="Only use this packet", season="Only use this season",
                           qtype="Only multiple choice or only free response",
                           window="How long each question stays open (default: the interval)",
                           post_now="Post the first question right away (default: yes)")
    @app_commands.rename(qtype="type")
    @app_commands.choices(qtype=QTYPE_CHOICES)
    @app_commands.autocomplete(packet=packet_autocomplete, season=season_autocomplete)
    @MANAGE
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel, interval: str,
                    packet: str | None = None, season: str | None = None,
                    qtype: app_commands.Choice[str] | None = None, window: str | None = None,
                    post_now: bool = True):
        secs = parse_duration(interval)
        if secs is None:
            await interaction.response.send_message("I couldn't read that interval. Try `30m`, `2h`, `1d` or `1h30m`.", ephemeral=True)
            return
        if not config.MIN_INTERVAL <= secs <= config.MAX_INTERVAL:
            await interaction.response.send_message(
                f"The interval must be between {fmt_duration(config.MIN_INTERVAL)} and {fmt_duration(config.MAX_INTERVAL)}.", ephemeral=True)
            return
        win = secs
        if window:
            win = parse_duration(window)
            if win is None or not 60 <= win <= config.MAX_WINDOW:
                await interaction.response.send_message(f"`window` must be between 1m and {fmt_duration(config.MAX_WINDOW)}.", ephemeral=True)
                return

        me = interaction.guild.me
        perms = channel.permissions_for(me) if me else None
        if perms is not None and not (perms.view_channel and perms.send_messages and perms.attach_files):
            await interaction.response.send_message(
                f"I need **View Channel**, **Send Messages** and **Attach Files** in {channel.mention} first.", ephemeral=True)
            return

        qt = qtype.value if qtype else None
        lib = self.bot.library
        if packet is not None and await lib.packet(packet) is None:
            await interaction.response.send_message(f"I don't have a packet called `{packet}`.", ephemeral=True)
            return
        pool = await lib.count_pool(packet, season, qt)
        if pool == 0:
            await interaction.response.send_message("No quizzable questions match those filters (packets need an answer key).", ephemeral=True)
            return

        next_run = time.time() + (0 if post_now else secs)
        await self.bot.db.execute(
            "INSERT INTO schedules(channel_id, guild_id, interval_s, window_s, packet_id, season, qtype, next_run, "
            "failures, enabled, created_by) VALUES (?,?,?,?,?,?,?,?,0,1,?) "
            "ON CONFLICT(channel_id) DO UPDATE SET guild_id=excluded.guild_id, interval_s=excluded.interval_s, "
            "window_s=excluded.window_s, packet_id=excluded.packet_id, season=excluded.season, qtype=excluded.qtype, "
            "next_run=excluded.next_run, failures=0, enabled=1, created_by=excluded.created_by",
            (channel.id, interaction.guild_id, secs, win, packet, season, qt, next_run, interaction.user.id))
        await interaction.response.send_message(
            f"{channel.mention} will get a random question every **{fmt_duration(secs)}** "
            f"(open for {fmt_duration(win)}) from {self._filters(packet, season, qt)} - {pool} questions in the pool.\n"
            f"First one {'in a few seconds' if post_now else f'<t:{int(next_run)}:R>'}.", ephemeral=True)

    @schedule.command(name="stop", description="Stop auto-posting in a channel")
    @app_commands.describe(channel="The channel to stop")
    @MANAGE
    async def stop(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.execute("DELETE FROM schedules WHERE channel_id = ? AND guild_id = ?", (channel.id, interaction.guild_id))
        await interaction.response.send_message(f"Stopped scheduled questions in {channel.mention}.", ephemeral=True)

    @schedule.command(name="now", description="Post the next scheduled question in a channel right away")
    @app_commands.describe(channel="The scheduled channel")
    @MANAGE
    async def now(self, interaction: discord.Interaction, channel: discord.TextChannel):
        row = await self.bot.db.fetchone("SELECT 1 FROM schedules WHERE channel_id = ? AND guild_id = ?", (channel.id, interaction.guild_id))
        if row is None:
            await interaction.response.send_message(f"{channel.mention} has no schedule. Use `/schedule setup`.", ephemeral=True)
            return
        await self.bot.db.execute("UPDATE schedules SET next_run = ?, enabled = 1, failures = 0 WHERE channel_id = ?", (time.time(), channel.id))
        await interaction.response.send_message(f"Posting in {channel.mention} within a few seconds.", ephemeral=True)

    @schedule.command(name="list", description="Show this server's scheduled channels")
    @MANAGE
    async def list(self, interaction: discord.Interaction):
        rows = await self.bot.db.fetchall("SELECT * FROM schedules WHERE guild_id = ? ORDER BY channel_id", (interaction.guild_id,))
        if not rows:
            await interaction.response.send_message("No schedules yet. Use `/schedule setup`.", ephemeral=True)
            return
        lines = []
        for r in rows:
            state = f"next <t:{int(r['next_run'])}:R>" if r["enabled"] else "**disabled** after repeated errors - fix permissions, then /schedule now"
            lines.append(f"<#{r['channel_id']}> - every {fmt_duration(r['interval_s'])} - "
                         f"{self._filters(r['packet_id'], r['season'], r['qtype'])} - {state}")
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    @staticmethod
    def _filters(packet, season, qtype) -> str:
        bits = [f"packet `{packet}`" if packet else "", f"season {season_label(season)}" if season else "",
                {"choice": "multiple choice", "free": "free response"}.get(qtype, "")]
        return ", ".join(b for b in bits if b) or "all packets"


async def setup(bot):
    await bot.add_cog(ScheduleCog(bot))
