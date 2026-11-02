
"""
Bulk-ingest UIL-style PDFs into data/packets/<packet id>/ (images + manifest.json).

    python ingest.py inbox/                         # a folder (searched recursively) of test and key files
    python ingest.py inbox/ --jobs 8                # process 8 packets in parallel
    python ingest.py inbox/ --dry-run               # show what would happen
    python ingest.py inbox/ --force                 # redo packets even if nothing changed
    python ingest.py inbox/ --meta meta.json        # override ids / titles / dates (see below)
    python ingest.py --add-key <packet id> key.pdf  # attach an answer key to a packet that has none yet

What it understands
  * A+ contests ("Contest #2324-10"), and packets with other covers (UIL study packets, JET, virtual
    challenge meets, ...). Headers like "QUESTION 7 xx" / "Question 7", choices written "A." or "A)",
    up to eight choices, multi-part questions that share code or a graph, and free-response questions.
  * Answer keys as a separate PDF/.txt/.csv/.json, OR already inside the test PDF (a "KEY" page at the end).
    Packets with no key are still stored; add the key later with --add-key.
  * The packet id is the contest number when there is one, otherwise a readable name taken from the cover
    (e.g. uil-2025-invitational-a). Rename the folder afterwards if you like, or pre-set ids with --meta.

How files are paired
  * Each PDF is read to decide if it is a test or a key, and which contest it belongs to. A+ files pair by
    contest number; others pair by file name (foo_TEST.pdf + foo_KEY.pdf, foo.pdf + foo_KEY.txt).

Metadata overrides (--meta meta.json): keys are a file name, a file stem or a contest number:
    { "CompSciWritten_StudyPacket_A_25.pdf": {"id": "uil-2425-inv-a", "title": "UIL Invitational A",
                                               "date": "January 2025", "season": "2425"} }

Safe to re-run: packets whose test/key files haven't changed are skipped. The running bot notices new or
changed packets by itself (or use /reindex).

Needs: pdfplumber, Pillow, poppler-utils (pdftoppm).  Writes ingest_report.json next to the output folder.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

KEY_TOKEN = re.compile(r"(?:^|[^a-z])key(?:[^a-z]|$)", re.I)
TEST_TOKEN = re.compile(r"(?:^|[^a-z])test(?:[^a-z]|$)", re.I)
TEXT_KEY_EXTS = {".txt", ".csv", ".json"}


def sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_stem(path):
    """File name without extension and without TEST/KEY tokens - used to pair files with no contest number."""
    stem = re.sub(r"(?i)(?<![a-z])(key|test)(?![a-z])", "", Path(path).stem)
    return re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")


def classify(path):
    """Worker: figure out what a file is. Never raises."""
    info = {"path": str(path), "kind": None, "contest": None, "stem": norm_stem(path), "meta": {}, "error": None,
            "mtime": os.path.getmtime(path)}
    ext = Path(path).suffix.lower()
    stem_name = Path(path).stem
    name_key, name_test = bool(KEY_TOKEN.search(stem_name)), bool(TEST_TOKEN.search(stem_name))
    if ext in TEXT_KEY_EXTS:
        info["kind"] = "key"
        return info
    try:
        import extract_uil as eu
        meta = eu.infer_meta(path)
        info["contest"] = meta.get("contest")
        info["meta"] = {k: v for k, v in meta.items() if k in ("title", "date", "season", "contest")}
        if eu.has_question_headers(path):
            info["kind"] = "test"
        else:
            is_key = bool(meta.get("is_key")) or (name_key and not name_test) or eu.is_key_pdf(path)
            info["kind"] = "key" if is_key else "test"
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def process_one(job):
    """Worker: extract one packet into a temp dir, then swap it into place."""
    import extract_uil
    pid, test, key, out_root, dpi = (job[k] for k in ("id", "test", "key", "out", "dpi"))
    out_root = Path(out_root)
    tmp = out_root / f".tmp-{pid}-{os.getpid()}"
    final = out_root / pid
    t0 = time.time()
    extra = {"packet_id": pid, "sources": job["sources"], **job["overrides"]}
    try:
        if tmp.exists():
            shutil.rmtree(tmp)
        manifest, problems = extract_uil.process(test, str(tmp), dpi, key, extra=extra)
        if final.exists():
            shutil.rmtree(final)
        os.replace(tmp, final)
        answered = sum(q["answer"] is not None for q in manifest["questions"])
        return {"id": pid, "status": "ok" if answered else "ok_no_key", "questions": manifest["question_count"],
                "groups": len(manifest["groups"]), "answered": answered, "key_source": manifest.get("key_source"),
                "title": manifest.get("title"), "problems": problems, "seconds": round(time.time() - t0, 1)}
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"id": pid, "status": "failed", "error": f"{type(e).__name__}: {e}", "problems": []}


def load_overrides(path):
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def overrides_for(table, info):
    for k in (Path(info["path"]).name, Path(info["path"]).stem, info.get("contest")):
        if k and k in table:
            return dict(table[k])
    return {}


def add_key(out_root, pid, key_path):
    import extract_uil
    pdir = Path(out_root) / pid
    if not (pdir / "manifest.json").exists():
        sys.exit(f"No packet '{pid}' in {out_root}. Existing ids start with: "
                 + ", ".join(sorted(p.name for p in Path(out_root).iterdir() if p.is_dir())[:8]))
    try:
        manifest, problems = extract_uil.attach_key(str(pdir), key_path)
    except ValueError as e:
        sys.exit(str(e))
    n = sum(q["answer"] is not None for q in manifest["questions"])
    print(f"{pid}: {n}/{manifest['question_count']} questions now have answers")
    for p in problems:
        print("  WARNING:", p)
    print("The running bot picks this up on its next rescan (or run /reindex).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inbox", nargs="?", help="folder containing the test and key files (searched recursively)")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "data" / "packets"))
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--force", action="store_true", help="reprocess even if sources are unchanged")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--meta", help="JSON file of id/title/date/season overrides")
    ap.add_argument("--add-key", nargs=2, metavar=("PACKET_ID", "KEYFILE"),
                    help="attach an answer key (pdf/txt/csv/json) to an already-ingested packet")
    ap.add_argument("--report", help="where to write the JSON report (default: <out>/../ingest_report.json)")
    args = ap.parse_args()

    out_root = Path(args.out)
    if args.add_key:
        add_key(out_root, *args.add_key)
        return
    if not args.inbox:
        ap.error("give an inbox folder (or use --add-key)")
    out_root.mkdir(parents=True, exist_ok=True)
    table = load_overrides(args.meta)

    files = sorted(p for p in Path(args.inbox).rglob("*")
                   if p.is_file() and p.suffix.lower() in {".pdf", *TEXT_KEY_EXTS} and not p.name.startswith("."))
    if not files:
        sys.exit(f"No .pdf files found under {args.inbox}")
    print(f"scanning {len(files)} files with {args.jobs} workers ...")

    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        infos = list(pool.map(classify, files, chunksize=4))

    report = {"unreadable": [], "key_without_test": [], "duplicates": [], "packets": []}
    tests, keys = {}, {}
    for i in infos:
        if i["error"]:
            report["unreadable"].append({"file": i["path"], "error": i["error"]})
            continue
        pair_key = i["contest"] or f"stem:{i['stem']}"
        bucket = tests if i["kind"] == "test" else keys
        if pair_key in bucket:
            older, newer = sorted([bucket[pair_key], i], key=lambda x: x["mtime"])
            report["duplicates"].append({"kept": newer["path"], "ignored": older["path"]})
            bucket[pair_key] = newer
        else:
            bucket[pair_key] = i
    stem_to_test = {t["stem"]: k for k, t in tests.items()}
    for k, kinfo in list(keys.items()):
        if k not in tests and kinfo["stem"] in stem_to_test:
            keys[stem_to_test[kinfo["stem"]]] = keys.pop(k)

    import extract_uil
    jobs, skipped, used_ids = [], 0, {}
    for pair_key, t in sorted(tests.items()):
        ov = overrides_for(table, t)
        base = ov.pop("id", None) or extract_uil.packet_id_from_meta(t["meta"], t["stem"])
        pid, n = base, 2
        while pid in used_ids:
            pid, n = f"{base}-{n}", n + 1
        used_ids[pid] = t["path"]
        k = keys.pop(pair_key, None)
        sources = {"test_sha1": sha1(t["path"]), "key_sha1": sha1(k["path"]) if k else None}
        manifest_path = out_root / pid / "manifest.json"
        if manifest_path.exists() and not args.force:
            try:
                with open(manifest_path) as f:
                    old = json.load(f)

                if old.get("sources", {}).get("test_sha1") == sources["test_sha1"] and \
                        (old["sources"].get("key_sha1") == sources["key_sha1"] or (k is None and old.get("key_source"))):
                    skipped += 1
                    continue
            except (OSError, ValueError, KeyError):
                pass
        jobs.append({"id": pid, "test": t["path"], "key": k["path"] if k else None, "out": str(out_root),
                     "dpi": args.dpi, "sources": sources, "overrides": ov})
    report["key_without_test"] = [v["path"] for v in keys.values()]

    print(f"{len(tests)} tests found: {len(jobs)} to process, {skipped} unchanged, "
          f"{len(report['key_without_test'])} keys without a test")
    if args.dry_run:
        for j in jobs:
            print(f"  would process {j['id']:<38} test={Path(j['test']).name}  key={Path(j['key']).name if j['key'] else '(none / embedded?)'}")
        return

    done, counts = 0, {}
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futs = [pool.submit(process_one, j) for j in jobs]
        for fut in as_completed(futs):
            r = fut.result()
            done += 1
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            report["packets"].append(r)
            if r["status"].startswith("ok"):
                extra = f"{r['questions']}q {r['groups']}g, {r['answered']} answered" + (f" ({r['key_source']})" if r["answered"] else "")
            else:
                extra = r.get("error")
            flag = f"  ⚠ {len(r['problems'])} warning(s)" if r["problems"] else ""
            print(f"[{done}/{len(jobs)}] {r['id']:<38} {r['status']:<9} {extra}{flag}")

    report_path = Path(args.report) if args.report else out_root.parent / "ingest_report.json"
    report["summary"] = {**counts, "skipped_unchanged": skipped, "unreadable": len(report["unreadable"]),
                         "key_without_test": len(report["key_without_test"])}
    report_path.write_text(json.dumps(report, indent=2))
    print("\nsummary:", report["summary"])
    nokey = [r["id"] for r in report["packets"] if r["status"] == "ok_no_key"]
    if nokey:
        print(f"no answer key yet for {len(nokey)} packet(s): {', '.join(nokey[:10])}" + (" ..." if len(nokey) > 10 else ""))
        print("  they are stored but can't be quizzed until you run:  python ingest.py --add-key <id> <keyfile>")
    needing_attention = [r for r in report["packets"] if r["problems"] or r["status"] == "failed"]
    for r in needing_attention[:20]:
        print(f"  {r['id']}: " + ("; ".join(r["problems"][:3]) if r["problems"] else r.get("error", "")))
    if len(needing_attention) > 20:
        print(f"  ... and {len(needing_attention) - 20} more (see {report_path})")
    print(f"full report: {report_path}")


if __name__ == "__main__":
    main()
