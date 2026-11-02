import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS packets (
    id              TEXT PRIMARY KEY,
    title           TEXT,
    contest         TEXT,
    season          TEXT,
    date            TEXT,
    date_iso        TEXT NOT NULL DEFAULT '',
    question_count  INTEGER NOT NULL,
    answered_count  INTEGER NOT NULL,
    group_count     INTEGER NOT NULL,
    free_count      INTEGER NOT NULL,
    manifest_mtime  REAL NOT NULL,
    manifest_size   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_packets_season ON packets(season);
CREATE INDEX IF NOT EXISTS idx_packets_order  ON packets(date_iso DESC, id DESC);

CREATE TABLE IF NOT EXISTS questions (
    packet_id   TEXT    NOT NULL,
    qid         INTEGER NOT NULL,
    group_id    TEXT,
    answer_type TEXT    NOT NULL,
    answer      TEXT,
    choices     TEXT    NOT NULL DEFAULT '',
    image       TEXT    NOT NULL,
    code_image  TEXT,
    group_image TEXT,
    PRIMARY KEY (packet_id, qid)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_questions_type ON questions(answer_type) WHERE answer IS NOT NULL;

CREATE TABLE IF NOT EXISTS active_questions (
    aid         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    packet_id   TEXT    NOT NULL,
    qid         INTEGER NOT NULL,
    created_at  REAL    NOT NULL,
    expires_at  REAL    NOT NULL,
    closed      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_active_open ON active_questions(expires_at) WHERE closed = 0;

CREATE TABLE IF NOT EXISTS answers (
    aid       INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    correct   INTEGER NOT NULL,
    submitted TEXT,
    ts        REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_answers_aid_user ON answers(aid, user_id);
CREATE INDEX IF NOT EXISTS idx_answers_user     ON answers(user_id, ts);

CREATE TABLE IF NOT EXISTS schedules (
    channel_id INTEGER PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    interval_s INTEGER NOT NULL,
    window_s   INTEGER NOT NULL,
    packet_id  TEXT,
    season     TEXT,
    qtype      TEXT,
    next_run   REAL    NOT NULL,
    failures   INTEGER NOT NULL DEFAULT 0,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(next_run) WHERE enabled = 1;

CREATE TABLE IF NOT EXISTS seen (
    scope     TEXT    NOT NULL,
    packet_id TEXT    NOT NULL,
    qid       INTEGER NOT NULL,
    ts        REAL    NOT NULL,
    PRIMARY KEY (scope, packet_id, qid)
) WITHOUT ROWID;
"""


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def fetchone(self, sql, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def execute(self, sql, params=()):
        async with self._write_lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cur.lastrowid

    @asynccontextmanager
    async def transaction(self):
        async with self._write_lock:
            try:
                yield self.conn
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise
