import asyncio
import json
import time
from types import SimpleNamespace

import discord
import pytest

import config
import quiz_core
from cogs._common import fmt_duration, parse_duration
from database import Database
from packets import Library
from tools.make_fake_packets import make_packet


def run(coro):
    return asyncio.run(coro)

# WARNING - AI GENERATED CODE INBOUND


@pytest.mark.parametrize("text,secs", [("30m", 1800), ("2h", 7200), ("1d", 86400), ("1h30m", 5400), ("90", 5400),
                                       ("1 hour", 3600), ("2 hours, 15 minutes", 8100), ("45 min", 2700),
                                       ("1.5h", 5400), ("1d 2h", 93600)])
def test_parse_duration_ok(text, secs):
    assert parse_duration(text) == secs


@pytest.mark.parametrize("text", ["", "abc", "5x", "1 month", "-5m", "0m", "m5"])
def test_parse_duration_bad(text):
    assert parse_duration(text) is None


def test_fmt_duration():
    assert fmt_duration(5400) == "1h 30m" and fmt_duration(86400) == "1d" and fmt_duration(30) == "30s"


@pytest.mark.parametrize("kind,given,key,ok", [
    ("choice", "C", "C", True), ("choice", "c", "C", True), ("choice", " C. ", "C", True),
    ("choice", "B", "C", False), ("choice", "", "C", False), ("choice", "CC", "C", False),
    ("free", "4", "4", True), ("free", "4.0", "4", True), ("free", " 4 ", "4", True), ("free", "5", "4", False),
    ("free", "-4,-9,9", "-4,-9,9", True), ("free", "-4, -9, 9", "-4,-9,9", True), ("free", "-4 -9 9", "-4,-9,9", True),
    ("free", "9,-4,-9", "-4,-9,9", False),
    ("free", "-4,-9", "-4,-9,9", False),
    ("free", "\u22124,-9,9", "-4,-9,9", True),
    ("free", "01111001", "01111001", True), ("free", "1111001", "01111001", False),
    ("free", "TRUE", "true", True), ("free", "3.5", "3.50", True),
])
def test_check_answer(kind, given, key, ok):
    assert quiz_core.check_answer(kind, given, key) is ok


def test_dynamic_item_templates():
    assert quiz_core.AnswerButton.__discord_ui_compiled_template__.fullmatch("uil:a:12:C")
    assert quiz_core.FreeAnswerButton.__discord_ui_compiled_template__.fullmatch("uil:f:12")
    assert not quiz_core.AnswerButton.__discord_ui_compiled_template__.fullmatch("uil:a:12:c")


def test_render_states():
    q = SimpleNamespace(qid=22, packet_id="2324-10", date="January 27, 2024", answer_type="choice", answer="B")
    live = quiz_core.render(q, expires_at=2_000_000_000, solvers=[1, 2])
    assert "Question 22" in live and "<t:2000000000:R>" in live and "<@1>" in live and "**B**" not in live
    done = quiz_core.render(q, expires_at=0, solvers=[], revealed=True)
    assert "**B**" in done and "Nobody solved" in done



@pytest.fixture
def packets(tmp_path):
    root = tmp_path / "packets"
    make_packet(root, "2324-10", season="2324", date="January 27, 2024", images=True)
    make_packet(root, "2324-09", season="2324", date="December 02, 2023", images=True)
    make_packet(root, "2526-04", season="2526", date="November 08, 2025", images=True)
    make_packet(root, "2526-05", season="2526", date="December 06, 2025", answered=False)
    return root


async def make_lib(root, tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    await db.connect()
    lib = Library(db, root)
    await lib.reindex()
    return db, lib


def test_index_search_and_filters(packets, tmp_path):
    async def go():
        db, lib = await make_lib(packets, tmp_path)
        c = await lib.counts()
        assert c["packets"] == 4 and c["questions"] == 160 and c["quizzable"] == 120
        rows, total = await lib.search_packets()
        assert total == 4 and [r["id"] for r in rows][0] == "2526-05"
        rows, total = await lib.search_packets(season="2324")
        assert total == 2
        rows, _ = await lib.search_packets("december")
        assert {r["id"] for r in rows} == {"2324-09", "2526-05"}
        rows, _ = await lib.search_packets("100%")
        assert rows == []
        page2, _ = await lib.search_packets(None, None, 3, 3)
        assert len(page2) == 1
        assert await lib.seasons() == ["2526", "2324"] and await lib.seasons("23-") == ["2324"]
        assert [r["id"] for r in await lib.autocomplete_packets("2526")] == ["2526-05", "2526-04"]
        await db.close()
    run(go())


def test_pick_respects_filters_and_never_picks_unkeyed(packets, tmp_path):
    async def go():
        db, lib = await make_lib(packets, tmp_path)
        for _ in range(60):
            q = await lib.pick("u:1")
            assert q.packet_id != "2526-05" and q.answer is not None
        for _ in range(30):
            q = await lib.pick("u:2", packet_id="2324-10", qtype="free")
            assert q.packet_id == "2324-10" and q.answer_type == "free" and q.qid in (39, 40)
        assert await lib.pick("u:3", packet_id="2526-05") is None
        assert await lib.pick("u:3", season="9999") is None
        assert await lib.count_pool("2324-10", None, "choice") == 38
        await db.close()
    run(go())


def test_pick_avoids_repeats_until_pool_exhausted(packets, tmp_path):
    async def go():
        db, lib = await make_lib(packets, tmp_path)
        seen = [(q.packet_id, q.qid) for q in [await lib.pick("c:9", packet_id="2324-10") for _ in range(40)]]
        assert len(set(seen)) == 40
        again = await lib.pick("c:9", packet_id="2324-10")
        assert again is not None
        other = [(q.packet_id, q.qid) for q in [await lib.pick("c:10", packet_id="2324-10") for _ in range(5)]]
        assert len(set(other)) == 5
        await db.close()
    run(go())


def test_incremental_reindex_update_remove_and_corrupt(packets, tmp_path):
    async def go():
        db, lib = await make_lib(packets, tmp_path)
        s = await lib.reindex()
        assert (s["added"], s["updated"], s["removed"]) == (0, 0, 0)

        p = packets / "2526-05" / "manifest.json"
        m = json.loads(p.read_text())
        for q in m["questions"]:
            q["answer"] = q["answer"] or "A"
        p.write_text(json.dumps(m))
        assert (await lib.reindex())["updated"] == 1
        assert (await lib.packet("2526-05"))["answered_count"] == 40

        make_packet(packets, "2627-01", season="2627", date="October 01, 2026")
        assert (await lib.reindex())["added"] == 1

        (packets / "2627-02").mkdir()
        (packets / "2627-02" / "manifest.json").write_text("{ not json")
        s = await lib.reindex()
        assert s["skipped"] == 1 and (await lib.counts())["packets"] == 5

        import shutil
        shutil.rmtree(packets / "2324-09")
        assert (await lib.reindex())["removed"] == 1
        assert await lib.get_question("2324-09", 1) is None
        await db.close()
    run(go())


def test_image_paths(packets, tmp_path):
    async def go():
        db, lib = await make_lib(packets, tmp_path)
        names = lambda q: [n for n, _ in lib.image_paths(q)]
        assert names(await lib.get_question("2324-10", 5)) == ["q05.png"]
        assert names(await lib.get_question("2324-10", 22)) == ["code_21-22.png", "q22.png"]
        (packets / "2324-10" / "images" / "q05.png").unlink()
        assert lib.image_paths(await lib.get_question("2324-10", 5)) == []
        await db.close()
    run(go())



class FakeResponse:
    def __init__(self):
        self.done, self.sent, self.modal = False, [], None

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kw):
        self.done = True
        self.sent.append(content)

    async def send_modal(self, modal):
        self.done, self.modal = True, modal

    async def defer(self, **kw):
        self.done = True


class FakeInteraction:
    def __init__(self, bot, uid):
        self.client, self.user, self.response = bot, SimpleNamespace(id=uid), FakeResponse()
        self.followup = self
        self.guild_id, self.channel_id = 1, 2
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content)

    @property
    def last(self):
        return (self.response.sent + self.sent)[-1]


class FakeMessage:
    def __init__(self, mid):
        self.id, self.edits = mid, []

    def get_partial_message(self, mid):
        return self

    async def edit(self, **kw):
        self.edits.append(kw)


class FakeDest(FakeMessage):
    def __init__(self):
        super().__init__(555)
        self.posts = []

    async def send(self, content, **kw):
        self.posts.append((content, kw))
        return SimpleNamespace(id=555)


def fake_bot(db, lib, dest):
    return SimpleNamespace(db=db, library=lib, cooldowns={}, get_partial_messageable=lambda cid, guild_id=None: dest)


def test_answer_flow_cooldown_and_reveal(packets, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WRONG_ANSWER_COOLDOWN", 10.0)

    async def go():
        db, lib = await make_lib(packets, tmp_path)
        dest = FakeDest()
        bot = fake_bot(db, lib, dest)
        q = await lib.get_question("2324-10", 5)
        await quiz_core.send_to_messageable(bot, dest, q, window_s=600, guild_id=1, channel_id=2)
        content, kw = dest.posts[0]
        assert "Question 5" in content and len(kw["view"].children) == 2
        assert [b.item.label for b in kw["view"].children] == ["A", "B"]
        assert len(kw["files"]) == 1
        row = await db.fetchone("SELECT * FROM active_questions")
        assert row["message_id"] == 555
        aid, key = row["aid"], q.answer
        wrong = "A" if key == "B" else "B"


        t = time.time()
        i1 = FakeInteraction(bot, 111)
        await quiz_core.submit_answer(i1, aid, wrong)
        assert "Incorrect" in i1.last
        assert 9 <= bot.cooldowns[111] - t <= 11

        i2 = FakeInteraction(bot, 111)
        await quiz_core.submit_answer(i2, aid, key)
        assert "Not so fast" in i2.last
        assert (await db.fetchone("SELECT COUNT(*) n FROM answers WHERE user_id=111"))["n"] == 1

        i3 = FakeInteraction(bot, 222)
        await quiz_core.submit_answer(i3, aid, key)
        assert "Correct" in i3.last
        assert "<@222>" in dest.edits[-1]["content"]

        bot.cooldowns[111] = time.time() - 1
        i4 = FakeInteraction(bot, 111)
        await quiz_core.submit_answer(i4, aid, key.lower())
        assert "Correct" in i4.last
        i5 = FakeInteraction(bot, 111)
        await quiz_core.submit_answer(i5, aid, key)
        assert "already solved" in i5.last

        await db.execute("UPDATE active_questions SET expires_at = ?", (time.time() - 1,))
        row = await db.fetchone("SELECT * FROM active_questions")
        i6 = FakeInteraction(bot, 333)
        await quiz_core.submit_answer(i6, aid, key)
        assert "closed" in i6.last
        await quiz_core.close_question(bot, row)
        edit = dest.edits[-1]
        assert edit["view"] is None and f"**{key}**" in edit["content"] and "<@111>" in edit["content"]
        assert (await db.fetchone("SELECT closed FROM active_questions"))["closed"] == 1
        await db.close()
    run(go())


def test_free_response_flow(packets, tmp_path):
    async def go():
        db, lib = await make_lib(packets, tmp_path)
        dest = FakeDest()
        bot = fake_bot(db, lib, dest)
        q = await lib.get_question("2324-10", 40)
        await quiz_core.send_to_messageable(bot, dest, q, window_s=600, guild_id=1, channel_id=2)
        view = dest.posts[0][1]["view"]
        assert len(view.children) == 1 and isinstance(view.children[0], quiz_core.FreeAnswerButton)
        aid = (await db.fetchone("SELECT aid FROM active_questions"))["aid"]

        i = FakeInteraction(bot, 7)
        await quiz_core.FreeAnswerButton(aid).callback(i)
        assert isinstance(i.response.modal, quiz_core.FreeAnswerModal)

        await quiz_core.submit_answer(FakeInteraction(bot, 7), aid, "nope")
        i = FakeInteraction(bot, 7)
        await quiz_core.FreeAnswerButton(aid).callback(i)
        assert i.response.modal is None and "Not so fast" in i.last
        bot.cooldowns[7] = 0
        i = FakeInteraction(bot, 7)
        await quiz_core.submit_answer(i, aid, q.answer.replace(",", ", "))
        assert "Correct" in i.last
        await db.close()
    run(go())


def test_scheduler_posts_reschedules_and_disables(packets, tmp_path):
    from cogs.schedule_cog import ScheduleCog, DISABLE_AFTER_FAILURES

    async def go():
        db, lib = await make_lib(packets, tmp_path)
        dest = FakeDest()
        bot = fake_bot(db, lib, dest)
        cog = ScheduleCog.__new__(ScheduleCog)
        cog.bot = bot
        await db.execute("INSERT INTO schedules(channel_id,guild_id,interval_s,window_s,packet_id,season,qtype,next_run,"
                         "failures,enabled) VALUES (2,1,3600,3600,'2324-10',NULL,'free',?,0,1)", (time.time() - 5,))
        row = await db.fetchone("SELECT * FROM schedules")
        await cog._run(row)
        assert len(dest.posts) == 1 and "free response" in dest.posts[0][0]
        r = await db.fetchone("SELECT * FROM schedules")
        assert 3590 < r["next_run"] - time.time() <= 3600 and r["failures"] == 0
        active = await db.fetchone("SELECT * FROM active_questions")
        assert active["channel_id"] == 2 and active["expires_at"] - active["created_at"] == 3600


        async def boom(*a, **k):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Access")
        dest.send = boom
        for n in range(DISABLE_AFTER_FAILURES):
            await cog._run(await db.fetchone("SELECT * FROM schedules"))
            r = await db.fetchone("SELECT * FROM schedules")
            assert r["failures"] == n + 1 and r["next_run"] > time.time()
        assert r["enabled"] == 0
        assert (await db.fetchone("SELECT COUNT(*) n FROM active_questions"))["n"] == 1
        await db.close()
    run(go())


def test_leaderboard_ranks_top_scorers(packets, tmp_path):
    from cogs.leaderboard_cog import LeaderboardCog

    async def go():
        db, lib = await make_lib(packets, tmp_path)
        bot = SimpleNamespace(db=db, get_user=lambda uid: None)
        i = FakeInteraction(bot, 1)

        captured = {}
        async def cap(content=None, **kw):
            captured["content"], captured["kw"] = content, kw
            i.response.done = True
        i.response.send_message = cap

        for uid, n in ((1, 5), (2, 3), (3, 3), (4, 1)):
            for _ in range(n):
                await db.execute("INSERT INTO answers(aid, user_id, correct, submitted, ts) "
                                 "VALUES (1, ?, 1, 'x', ?)", (uid, time.time()))
        cog = LeaderboardCog.__new__(LeaderboardCog)
        cog.bot = bot
        await cog.leaderboard.callback(cog, i)
        assert i.response.done
        desc = captured["kw"]["embed"].description
        assert "1. <@1> - 5 points" in desc
        assert "<@2> - 3 points" in desc and "<@3> - 3 points" in desc
        assert "4. <@4> - 1 point" in desc
        assert desc.count("\n") == 3
        await db.close()
    run(go())


def test_leaderboard_empty(tmp_path):
    from cogs.leaderboard_cog import LeaderboardCog

    async def go():
        db = Database(tmp_path / "t.sqlite3")
        await db.connect()
        bot = SimpleNamespace(db=db, get_user=lambda uid: None)
        i = FakeInteraction(bot, 1)
        captured = {}
        async def cap(content=None, **kw):
            captured["content"] = content
            i.response.done = True
        i.response.send_message = cap
        cog = LeaderboardCog.__new__(LeaderboardCog)
        cog.bot = bot
        await cog.leaderboard.callback(cog, i)
        assert "No points yet" in captured["content"]
        await db.close()
    run(go())


def test_stats_reports_counts(packets, tmp_path):
    from cogs.leaderboard_cog import LeaderboardCog

    async def go():
        db, lib = await make_lib(packets, tmp_path)
        bot = SimpleNamespace(db=db, library=lib)
        i = FakeInteraction(bot, 1)
        captured = {}
        async def cap(content=None, **kw):
            captured["content"] = content
            i.response.done = True
        i.response.send_message = cap
        cog = LeaderboardCog.__new__(LeaderboardCog)
        cog.bot = bot
        await cog.stats.callback(cog, i)
        assert i.response.done
        assert "4 packets, 160 questions (120 quizzable)" in captured["content"]
        await db.close()
    run(go())



def test_commands_register_and_validate():
    import bot as botmod

    async def go():
        b = botmod.UILBot()
        for ext in botmod.EXTENSIONS:
            await b.load_extension(ext)
        b.add_dynamic_items(botmod.AnswerButton, botmod.FreeAnswerButton)
        payload = {c.name: c.to_dict(b.tree) for c in b.tree.get_commands()}
        assert set(payload) == {"packets", "packet", "reindex", "quiz", "schedule", "leaderboard", "stats"}
        opts = {o["name"]: o for o in payload["quiz"]["options"]}
        assert set(opts) == {"packet", "number", "season", "type"}
        assert opts["packet"]["autocomplete"] and [c["value"] for c in opts["type"]["choices"]] == ["choice", "free"]
        subs = {o["name"] for o in payload["schedule"]["options"]}
        assert subs == {"setup", "stop", "now", "list"}
        assert payload["schedule"].get("dm_permission", True) is False
        await b.close()
    run(go())
