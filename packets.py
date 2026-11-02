"""The packet library: indexes data/packets/*/manifest.json into SQLite and answers queries.

Designed for thousands of packets:
  * the index is incremental - only manifests whose mtime/size changed are re-read;
  * manifests are parsed in a worker thread and written in batched transactions;
  * nothing but small rows is kept in memory (images are opened only when a question is posted);
  * quiz picks, searches and autocomplete are indexed SQL queries.
"""
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from database import Database

log = logging.getLogger(__name__)
CHUNK = 200


@dataclass(slots=True)
class Q:
    packet_id: str
    qid: int
    group_id: str | None
    answer_type: str
    answer: str | None
    choices: str
    image: str
    code_image: str | None
    group_image: str | None
    date: str | None = None

    @classmethod
    def from_row(cls, r):
        return cls(r["packet_id"], r["qid"], r["group_id"], r["answer_type"], r["answer"], r["choices"],
                   r["image"], r["code_image"], r["group_image"], r["date"] if "date" in r.keys() else None)


def _date_iso(date, season=None):
    """Sortable date. 'January 27, 2024' and 'October 2026' parse directly; with no date we fall back to the
    season's start (2122 -> 2021-08-01) so undated packets still sort sensibly."""
    if date:
        text = " ".join(str(date).split()).title()
        for fmt in ("%B %d, %Y", "%B %Y"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                pass
    if season and re.fullmatch(r"\d{4}", season):
        return f"20{season[:2]}-08-01"
    return ""


def _like(text):
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _scan(packets_dir: Path, known: dict, bad: dict):
    """Cheap directory scan: which packet folders are new/changed, and which exist at all."""
    changed, seen = [], set()
    try:
        entries = list(os.scandir(packets_dir))
    except FileNotFoundError:
        return changed, seen
    for e in entries:
        if e.name.startswith(".") or not e.is_dir():
            continue
        try:
            st = os.stat(os.path.join(e.path, "manifest.json"))
        except OSError:
            continue
        seen.add(e.name)
        sig = (st.st_mtime, st.st_size)
        if known.get(e.name) != sig and bad.get(e.name) != sig:
            changed.append(e.name)
    return changed, seen


def _load_many(packets_dir: Path, ids):
    """Parse manifests (runs in a thread). Returns (good, bad) where bad maps id -> (sig, error)."""
    good, bad = [], {}
    for pid in ids:
        path = packets_dir / pid / "manifest.json"
        try:
            st = path.stat()
            with open(path, encoding="utf-8") as f:
                m = json.load(f)
            groups = {g["id"]: g for g in m.get("groups", [])}
            rows, answered, free = [], 0, 0
            for q in m["questions"]:
                choices = "".join(q.get("choices") or [])
                atype = q.get("answer_type") or ("choice" if choices else "free")
                if atype == "choice" and not choices:
                    atype = "free"
                ans = q.get("answer")
                ans = None if ans is None or not str(ans).strip() else str(ans).strip()
                answered += ans is not None
                free += atype == "free"
                gid = q.get("group")
                g = groups.get(gid) if gid else None
                rows.append((pid, int(q["id"]), gid, atype, ans, choices, q["image"],
                             g.get("code_image") if g else None, g.get("image") if g else None))
            season = m.get("season") or (pid[:4] if re.match(r"^\d{4}-", pid) else None)
            date = m.get("date")
            packet = (pid, m.get("title") or f"Packet {pid}", m.get("contest"), season, date, _date_iso(date, season),
                      len(rows), answered, len(groups), free, st.st_mtime, st.st_size)
            good.append((packet, rows))
        except Exception as exc:
            try:
                st = path.stat()
                sig = (st.st_mtime, st.st_size)
            except OSError:
                sig = None
            bad[pid] = (sig, f"{type(exc).__name__}: {exc}")
    return good, bad


class Library:
    def __init__(self, db: Database, packets_dir):
        self.db = db
        self.packets_dir = Path(packets_dir)
        self._bad: dict[str, tuple] = {}
        self._reindex_lock = asyncio.Lock()


    async def reindex(self) -> dict:
        """Bring the SQLite index in line with the packet folders. Safe to call repeatedly."""
        async with self._reindex_lock:
            t0 = time.perf_counter()
            known = {r["id"]: (r["manifest_mtime"], r["manifest_size"])
                     for r in await self.db.fetchall("SELECT id, manifest_mtime, manifest_size FROM packets")}
            bad_sigs = {k: v[0] for k, v in self._bad.items()}
            changed, seen = await asyncio.to_thread(_scan, self.packets_dir, known, bad_sigs)

            added = updated = 0
            for i in range(0, len(changed), CHUNK):
                good, bad = await asyncio.to_thread(_load_many, self.packets_dir, changed[i:i + CHUNK])
                for pid, (sig, err) in bad.items():
                    if pid not in self._bad or self._bad[pid][1] != err:
                        log.warning("skipping packet %s: %s", pid, err)
                    self._bad[pid] = (sig, err)
                async with self.db.transaction() as conn:
                    for packet, rows in good:
                        pid = packet[0]
                        added += pid not in known
                        updated += pid in known
                        self._bad.pop(pid, None)
                        await conn.execute("DELETE FROM questions WHERE packet_id = ?", (pid,))
                        await conn.execute("INSERT OR REPLACE INTO packets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", packet)
                        await conn.executemany("INSERT INTO questions VALUES (?,?,?,?,?,?,?,?,?)", rows)

            gone = [pid for pid in known if pid not in seen]
            for i in range(0, len(gone), 500):
                batch = gone[i:i + 500]
                marks = ",".join("?" * len(batch))
                async with self.db.transaction() as conn:
                    await conn.execute(f"DELETE FROM questions WHERE packet_id IN ({marks})", batch)
                    await conn.execute(f"DELETE FROM packets WHERE id IN ({marks})", batch)
            for pid in list(self._bad):
                if pid not in seen:
                    del self._bad[pid]

            stats = {"added": added, "updated": updated, "removed": len(gone), "skipped": len(self._bad),
                     "seconds": round(time.perf_counter() - t0, 2)}
            if added or updated or gone:
                log.info("reindex: %s", stats)
            return stats


    async def counts(self):
        r = await self.db.fetchone("SELECT COUNT(*) AS p, COALESCE(SUM(question_count),0) AS q, "
                                   "COALESCE(SUM(answered_count),0) AS a FROM packets")
        return {"packets": r["p"], "questions": r["q"], "quizzable": r["a"]}

    async def packet(self, pid):
        return await self.db.fetchone("SELECT * FROM packets WHERE id = ?", (pid,))

    async def search_packets(self, text=None, season=None, limit=10, offset=0):
        where, params = [], []
        if season:
            where.append("season = ?")
            params.append(season)
        if text:
            where.append("(id LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' OR date LIKE ? ESCAPE '\\')")
            params += [_like(text)] * 3
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = (await self.db.fetchone(f"SELECT COUNT(*) AS n FROM packets {clause}", params))["n"]
        rows = await self.db.fetchall(
            f"SELECT * FROM packets {clause} ORDER BY date_iso DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset])
        return rows, total

    async def autocomplete_packets(self, current, limit=25):
        rows, _ = await self.search_packets(current.strip() or None, limit=limit)
        return rows

    async def seasons(self, current=""):
        rows = await self.db.fetchall(
            "SELECT DISTINCT season FROM packets WHERE season IS NOT NULL AND season LIKE ? ESCAPE '\\' "
            "ORDER BY season DESC LIMIT 25", (_like(current.strip().replace("-", "")),))
        return [r["season"] for r in rows]


    async def get_question(self, packet_id, qid):
        r = await self.db.fetchone(
            "SELECT q.*, p.date AS date FROM questions q JOIN packets p ON p.id = q.packet_id "
            "WHERE q.packet_id = ? AND q.qid = ?", (packet_id, qid))
        return Q.from_row(r) if r else None

    async def count_pool(self, packet_id=None, season=None, qtype=None):
        r = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM questions q JOIN packets p ON p.id = q.packet_id "
            "WHERE q.answer IS NOT NULL AND (:pid IS NULL OR q.packet_id = :pid) "
            "AND (:season IS NULL OR p.season = :season) AND (:qtype IS NULL OR q.answer_type = :qtype)",
            {"pid": packet_id, "season": season, "qtype": qtype})
        return r["n"]

    _POOL = ("SELECT q.*, p.date AS date FROM questions q JOIN packets p ON p.id = q.packet_id "
             "WHERE q.answer IS NOT NULL AND (:pid IS NULL OR q.packet_id = :pid) "
             "AND (:season IS NULL OR p.season = :season) AND (:qtype IS NULL OR q.answer_type = :qtype)")

    async def pick(self, scope, packet_id=None, season=None, qtype=None):
        """Random answered question matching the filters, preferring ones `scope` hasn't seen yet."""
        f = {"pid": packet_id, "season": season, "qtype": qtype}
        row = await self.db.fetchone(
            self._POOL + " AND NOT EXISTS (SELECT 1 FROM seen s WHERE s.scope = :scope "
                         "AND s.packet_id = q.packet_id AND s.qid = q.qid) ORDER BY RANDOM() LIMIT 1",
            {**f, "scope": scope})
        if row is None:
            row = await self.db.fetchone(self._POOL + " ORDER BY RANDOM() LIMIT 1", f)
            if row is not None:
                await self.db.execute("DELETE FROM seen WHERE scope = ?", (scope,))
        if row is None:
            return None
        await self.db.execute("INSERT OR REPLACE INTO seen(scope, packet_id, qid, ts) VALUES (?,?,?,?)",
                              (scope, row["packet_id"], row["qid"], time.time()))
        return Q.from_row(row)

    def image_paths(self, q: Q):
        """[(filename, Path)] to attach for this question, or [] if any file is missing on disk."""
        base = self.packets_dir / q.packet_id
        if q.code_image:
            files = [(f"code_{q.group_id}.png", base / q.code_image), (f"q{q.qid:02d}.png", base / q.image)]
        elif q.group_id and q.group_image:
            files = [(f"group_{q.group_id}.png", base / q.group_image)]
        else:
            files = [(f"q{q.qid:02d}.png", base / q.image)]
        return files if all(p.is_file() for _, p in files) else []
