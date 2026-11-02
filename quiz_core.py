import logging
import math
import re
import time

import discord

import config
from packets import Q

log = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()



def _is_leading_zero(tok):
    return re.match(r"^[+-]?0\d", tok) is not None


def _same_token(given, key):
    given, key = given.replace("\u2212", "-").strip(), key.replace("\u2212", "-").strip()
    if given.casefold() == key.casefold():
        return True
    if _is_leading_zero(given) or _is_leading_zero(key):
        return False
    try:
        return math.isclose(float(given), float(key), rel_tol=0, abs_tol=1e-9)
    except ValueError:
        return False


def check_answer(answer_type: str, given: str, key: str) -> bool:
    given, key = (given or "").strip(), (key or "").strip()
    if not given or not key:
        return False
    if answer_type == "choice":
        return re.sub(r"[^A-Za-z]", "", given).upper() == key.upper()
    key_tokens = [t for t in re.split(r"[,\s]+", key) if t]
    given_tokens = [t for t in re.split(r"[,\s]+", given) if t]
    if len(key_tokens) == 1:
        return len(given_tokens) == 1 and _same_token(given_tokens[0], key_tokens[0])
    if len(given_tokens) == len(key_tokens) and all(_same_token(g, k) for g, k in zip(given_tokens, key_tokens)):
        return True
    if all(len(t) == 1 for t in key_tokens):
        return "".join(given_tokens).casefold() == "".join(key_tokens).casefold()
    return False



def render(q: Q, *, expires_at: float, solvers=(), revealed=False) -> str:
    kind = "multiple choice" if q.answer_type == "choice" else "free response"
    head = f"**Question {q.qid}** - `{q.packet_id}`" + (f" ({q.date})" if q.date else "") + f" - {kind}"
    lines = [head]
    if revealed:
        ans = f"**{q.answer}**" if q.answer_type == "choice" else f"`{q.answer}`"
        lines.append(f"Answer: {ans}")
    else:
        lines.append("Pick your answer below." if q.answer_type == "choice" else
                     "Press **Answer** to submit. If your answer has several values, separate them with commas.")
        lines.append(f"Closes <t:{int(expires_at)}:R>")
    solvers = list(solvers)
    if solvers:
        shown = " ".join(f"<@{u}>" for u in solvers[:10])
        more = f" and {len(solvers) - 10} more" if len(solvers) > 10 else ""
        lines.append(f"{'Solved by' if revealed else 'Solved by'}: {shown}{more}")
    elif revealed:
        lines.append("Nobody solved this one.")
    return "\n".join(lines)



class AnswerButton(discord.ui.DynamicItem[discord.ui.Button], template=r"uil:a:(?P<aid>\d+):(?P<c>[A-Z])"):
    def __init__(self, aid: int, choice: str):
        super().__init__(discord.ui.Button(label=choice, style=discord.ButtonStyle.primary,
                                           custom_id=f"uil:a:{aid}:{choice}"))
        self.aid, self.choice = aid, choice

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["aid"]), match["c"])

    async def callback(self, interaction: discord.Interaction):
        await submit_answer(interaction, self.aid, self.choice)


class FreeAnswerButton(discord.ui.DynamicItem[discord.ui.Button], template=r"uil:f:(?P<aid>\d+)"):
    def __init__(self, aid: int):
        super().__init__(discord.ui.Button(label="Answer", style=discord.ButtonStyle.success,
                                           custom_id=f"uil:f:{aid}"))
        self.aid = aid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["aid"]))

    async def callback(self, interaction: discord.Interaction):
        row = await _gate(interaction, self.aid)
        if row is not None:
            await interaction.response.send_modal(FreeAnswerModal(self.aid, row["qid"]))


class FreeAnswerModal(discord.ui.Modal):
    answer = discord.ui.TextInput(label="Your answer", max_length=200, required=True)

    def __init__(self, aid: int, qid: int):
        super().__init__(title=f"Question {qid}"[:45])
        self.aid = aid

    async def on_submit(self, interaction: discord.Interaction):
        await submit_answer(interaction, self.aid, self.answer.value)


def build_view(aid: int, q: Q) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if q.answer_type == "choice":
        for c in q.choices:
            view.add_item(AnswerButton(aid, c))
    else:
        view.add_item(FreeAnswerButton(aid))

    return view



async def _reply(interaction: discord.Interaction, text: str):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def _gate(interaction: discord.Interaction, aid: int):
    """Cooldown / open / already-solved checks. Replies and returns None if the user can't answer now."""
    bot = interaction.client
    uid, now = interaction.user.id, time.time()
    unlock = bot.cooldowns.get(uid, 0.0)
    if unlock > now:
        await _reply(interaction, f"Not so fast. You can answer again <t:{math.ceil(unlock)}:R>.")
        return None
    row = await bot.db.fetchone("SELECT * FROM active_questions WHERE aid = ?", (aid,))
    if row is None:
        await _reply(interaction, "This question isn't available any more.")
        return None
    if row["closed"] or row["expires_at"] <= now:
        await _reply(interaction, "This question has closed.")
        return None
    if await bot.db.fetchone("SELECT 1 FROM answers WHERE aid = ? AND user_id = ? AND correct = 1", (aid, uid)):
        await _reply(interaction, "You've already solved this one")
        return None
    return row


async def _solvers(bot, aid):
    rows = await bot.db.fetchall(
        "SELECT user_id FROM answers WHERE aid = ? AND correct = 1 GROUP BY user_id ORDER BY MIN(ts)", (aid,))
    return [r["user_id"] for r in rows]


def _message_of(bot, row):
    chan = bot.get_partial_messageable(row["channel_id"], guild_id=row["guild_id"])
    return chan.get_partial_message(row["message_id"])


async def submit_answer(interaction: discord.Interaction, aid: int, given: str):
    bot = interaction.client
    row = await _gate(interaction, aid)
    if row is None:
        return
    q = await bot.library.get_question(row["packet_id"], row["qid"])
    if q is None or q.answer is None:
        await _reply(interaction, "Sorry, I can't check that question right now.")
        return
    now = time.time()
    correct = check_answer(q.answer_type, given, q.answer)
    await bot.db.execute("INSERT INTO answers(aid, user_id, correct, submitted, ts) VALUES (?,?,?,?,?)",
                         (aid, interaction.user.id, int(correct), (given or "")[:200], now))
    if correct:
        await _reply(interaction, "**Correct!**")
        if row["message_id"]:
            try:
                content = render(q, expires_at=row["expires_at"], solvers=await _solvers(bot, aid))
                await _message_of(bot, row).edit(content=content, allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException as e:
                log.debug("couldn't update solver list: %s", e)
    else:
        unlock = now + config.WRONG_ANSWER_COOLDOWN
        bot.cooldowns[interaction.user.id] = unlock
        await _reply(interaction, f"**Incorrect.** You can try again <t:{math.ceil(unlock)}:R>.")


async def close_question(bot, row):
    """Reveal the answer and remove the buttons. Idempotent (marks the row closed first)."""
    await bot.db.execute("UPDATE active_questions SET closed = 1 WHERE aid = ?", (row["aid"],))
    if not row["message_id"]:
        return
    q = await bot.library.get_question(row["packet_id"], row["qid"])
    if q is None:
        return
    content = render(q, expires_at=row["expires_at"], solvers=await _solvers(bot, row["aid"]), revealed=True)
    try:
        await _message_of(bot, row).edit(content=content, view=None, allowed_mentions=discord.AllowedMentions(users=True))
    except (discord.NotFound, discord.Forbidden):
        pass
    except discord.HTTPException as e:
        log.warning("couldn't close question message %s: %s", row["message_id"], e)



async def pick_valid(bot, scope, **filters):
    """Pick a random question whose image files exist (skips entries with missing files)."""
    for _ in range(8):
        q = await bot.library.pick(scope, **filters)
        if q is None:
            return None
        if bot.library.image_paths(q):
            return q
        log.warning("images missing on disk for %s Q%s - skipping", q.packet_id, q.qid)
    return None


async def _prepare(bot, q: Q, window_s, guild_id, channel_id):
    files = [discord.File(p, filename=name) for name, p in bot.library.image_paths(q)]
    now = time.time()
    expires = now + window_s
    aid = await bot.db.execute(
        "INSERT INTO active_questions(guild_id, channel_id, packet_id, qid, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?)", (guild_id, channel_id, q.packet_id, q.qid, now, expires))
    return aid, render(q, expires_at=expires), files, build_view(aid, q)


async def send_to_messageable(bot, dest, q: Q, *, window_s, guild_id, channel_id):
    """Post to a channel (used by the scheduler)."""
    aid, content, files, view = await _prepare(bot, q, window_s, guild_id, channel_id)
    try:
        msg = await dest.send(content, files=files, view=view, allowed_mentions=NO_MENTIONS)
    except BaseException:
        await bot.db.execute("DELETE FROM active_questions WHERE aid = ?", (aid,))
        raise
    await bot.db.execute("UPDATE active_questions SET message_id = ? WHERE aid = ?", (msg.id, aid))
    return msg


async def send_via_interaction(interaction: discord.Interaction, q: Q, window_s):
    """Post as the reply to a slash command."""
    bot = interaction.client
    aid, content, files, view = await _prepare(bot, q, window_s, interaction.guild_id, interaction.channel_id)
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True)
        msg = await interaction.followup.send(content, files=files, view=view, wait=True,
                                              allowed_mentions=NO_MENTIONS)
    except BaseException:
        await bot.db.execute("DELETE FROM active_questions WHERE aid = ?", (aid,))
        raise
    await bot.db.execute("UPDATE active_questions SET message_id = ? WHERE aid = ?", (msg.id, aid))
    return msg
