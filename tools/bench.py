import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from database import Database
from packets import Library

# WARNING - AI GENERATED CODE INBOUND

async def main(packets_dir):
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "bench.sqlite3")
        await db.connect()
        lib = Library(db, packets_dir)

        async def timed(label, coro):
            t = time.perf_counter()
            r = await coro
            print(f"{label:<44} {(time.perf_counter() - t) * 1000:9.1f} ms")
            return r

        print(await timed("first index (cold)", lib.reindex()))
        print(await lib.counts())
        print(await timed("re-index, nothing changed", lib.reindex()))
        await timed("pick random (all packets)", lib.pick("u:1"))
        await timed("pick random (MC only, one season)", lib.pick("u:1", season="1011", qtype="choice"))
        for i in range(2, 200):
            await lib.pick("u:1")
        await timed("pick after 200 questions already seen", lib.pick("u:1"))
        await timed("autocomplete 'jan' (25 results)", lib.autocomplete_packets("jan"))
        await timed("autocomplete empty (newest 25)", lib.autocomplete_packets(""))
        await timed("search page 50 of /packets", lib.search_packets(None, None, 10, 500))
        await db.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
