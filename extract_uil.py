
import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

import pdfplumber
from PIL import Image

LIGATURES = {"\u019f": "ti", "\u01a9": "tt", "\u014c": "ft", "\ufb01": "fi", "\ufb02": "fl", "\ufb00": "ff",
             "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}
CID_MAP = {415: "ti", 425: "tt", 332: "ft", 3364: "\u0304"}
_CID_RE = re.compile(r"\(cid:(\d+)\)")


def fix_ligatures(text):
    if not text:
        return text
    text = _CID_RE.sub(lambda m: CID_MAP.get(int(m.group(1)), ""), text)
    return "".join(LIGATURES.get(ch, ch) for ch in text)


HEADER_RE = re.compile(r"^question$", re.I)
HEADER_NUM_RE = re.compile(r"^(\d{1,3})(?:x+)?$", re.I)
HEADER_GLUED_RE = re.compile(r"^question(\d{1,3})(?:x+)?$", re.I)
GROUP_TEXT_RE = re.compile(r"questions?\s+(\d+)\s*(?:-|–|—|to|and|&)\s*(\d+)", re.I)
CHOICE_RE = re.compile(r"(?<!\S)([A-H])[\.\)](?=\s*\S)")
HEADER_TEXT_RE = re.compile(r"^\s*Q\s*UESTION\s*\d+\s*(?:x+)?\s*", re.I)
EDGE_TOL = 1.5
BORDER_FRACTION = 0.6


def parse_choices(text):
    found = set(CHOICE_RE.findall(text or ""))
    run = ""
    for ch in "ABCDEFGH":
        if ch in found:
            run += ch
        else:
            break
    return list(run) if len(run) >= 2 else []


def find_headers(page):
    words = page.extract_words()
    found = []
    for i, w in enumerate(words):
        if w["x0"] >= page.width * 0.2:
            continue
        text = fix_ligatures(w["text"])
        g = HEADER_GLUED_RE.match(text)
        if g:
            found.append((int(g.group(1)), w["top"]))
        elif HEADER_RE.match(text) and i + 1 < len(words):
            nxt = words[i + 1]
            m = HEADER_NUM_RE.match(nxt["text"])
            if m and abs(nxt["top"] - w["top"]) < 4:
                found.append((int(m.group(1)), w["top"]))
    return sorted(found, key=lambda t: t[1])


def _cluster(items, tol=EDGE_TOL):
    out = []
    for k, seg in sorted(items, key=lambda t: t[0]):
        if out and abs(out[-1]["k"] - k) <= tol:
            out[-1]["segs"].append(seg)
            out[-1]["k"] = (out[-1]["k"] * (len(out[-1]["segs"]) - 1) + k) / len(out[-1]["segs"])
        else:
            out.append({"k": k, "segs": [seg]})
    return out


def _covered(segs, lo=None, hi=None, gap=1.5):
    spans = []
    for a, b in segs:
        if lo is not None:
            a = max(a, lo)
        if hi is not None:
            b = min(b, hi)
        if b > a:
            spans.append((a, b))
    spans.sort()
    total, cur = 0.0, None
    for a, b in spans:
        if cur is None:
            cur = [a, b]
        elif a <= cur[1] + gap:
            cur[1] = max(cur[1], b)
        else:
            total += cur[1] - cur[0]
            cur = [a, b]
    if cur:
        total += cur[1] - cur[0]
    return total


def analyze_page(page, headers):
    H = [(e["top"], (e["x0"], e["x1"])) for e in page.edges if e["orientation"] == "h" and e["x1"] - e["x0"] >= 2]
    V = [((e["x0"] + e["x1"]) / 2, (e["top"], e["bottom"])) for e in page.edges if e["orientation"] == "v"]
    hcl = _cluster(H)
    long_h = [c for c in hcl if _covered(c["segs"]) >= 150]
    if long_h:
        left = min(a for c in long_h for a, _ in c["segs"]) - 3
        right = max(b for c in long_h for _, b in c["segs"]) + 3
    else:
        left, right = 33.0, page.width - 33.0
    width = right - left

    divider, best = None, 80.0
    for c in _cluster(V):
        if left + 0.25 * width <= c["k"] <= left + 0.75 * width:
            length = _covered(c["segs"], gap=3.0)
            if length > best:
                divider, best = c["k"], length

    band = [c for c in _cluster(V) if divider is not None and abs(c["k"] - divider) <= 20]

    def border_near(y, above=8.0, below=2.0):
        cands = [c for c in long_h if y - above <= c["k"] <= y + below]
        if not cands:
            return None, None
        c = min(cands, key=lambda c: abs(c["k"] - y))
        if divider is None:
            return ("full" if _covered(c["segs"]) >= BORDER_FRACTION * width else None), c["k"]
        cov_l = _covered(c["segs"], left, divider) / max(divider - left, 1)
        cov_r = _covered(c["segs"], divider, right) / max(right - divider, 1)
        if cov_l >= BORDER_FRACTION and cov_r >= BORDER_FRACTION:
            return "full", c["k"]
        if cov_l >= BORDER_FRACTION and cov_r < 0.25:
            return "left", c["k"]
        return None, c["k"]

    words = page.extract_words()
    body_bottom = max((w["bottom"] for w in words if w["top"] < page.height * 0.93), default=page.height - 40)
    last_top = headers[-1][1]
    below = [c for c in long_h if c["k"] > last_top + 12 and border_near(c["k"], 0.5, 0.5)[0] == "full"]
    content_bottom = (min(c["k"] for c in below) + 1.5) if below else body_bottom + 4

    qs, merged_with_next = {}, set()
    for i, (qn, top) in enumerate(headers):
        bottom = headers[i + 1][1] - 1 if i + 1 < len(headers) else content_bottom
        row_div, full_width = (left + right) / 2, True
        if divider is not None and band:
            best = max(band, key=lambda c: _covered(c["segs"], top, bottom, gap=3.0))
            if _covered(best["segs"], top, bottom, gap=3.0) >= 0.3 * (bottom - top):
                row_div, full_width = best["k"], False
            else:
                row_div = divider
        qs[qn] = dict(top=top - 2, bottom=bottom, left=left, right=right, divider=row_div, full_width=full_width)
        if i + 1 < len(headers) and border_near(headers[i + 1][1])[0] == "left":
            merged_with_next.add(qn)
    return qs, merged_with_next


def text_groups(page, present):
    text = fix_ligatures(page.extract_text() or "")
    out = []
    for m in GROUP_TEXT_RE.finditer(text):
        a, b = int(m.group(1)), int(m.group(2))
        if a < b and a in present and b in present:
            out.append((a, b))
    return out


def build_groups(merged_with_next, text_based):
    spans = []
    for q in sorted(merged_with_next):
        if spans and spans[-1][1] == q:
            spans[-1][1] = q + 1
        else:
            spans.append([q, q + 1])
    groups = {(a, b) for a, b in spans}
    for a, b in text_based:
        if not any(ga <= a and b <= gb for ga, gb in groups):
            groups.add((a, b))
    return sorted(groups)



MARKER_FULL = re.compile(r"^(?:q\.?)?(\d{1,3})([\)\.:\-–])$", re.I)
MARKER_JUNK = re.compile(r"^[A-Za-z]{1,2}(\d{1,3})([\)\.:\-–])$")
MARKER_INLINE = re.compile(r"^(?:q\.?)?(\d{1,3})(?:([\)\:])(\S.*)|\.([A-Ea-e]))$", re.I)
LABEL_RE = re.compile(r"^(?:q|question)\.?$", re.I)
GAP_PT = 16


def _lines_from_pdf(path, pages=None):
    lines = []
    with pdfplumber.open(path) as pdf:
        for pi, page in enumerate(pdf.pages):
            if pages is not None and pi not in pages:
                continue
            words = sorted(page.extract_words(), key=lambda w: (w["top"], w["x0"]))
            cur, cur_top = [], None
            for w in words:
                if cur_top is None or abs(w["top"] - cur_top) <= 3:
                    cur.append(w)
                    cur_top = w["top"] if cur_top is None else cur_top
                else:
                    lines.append(sorted(cur, key=lambda x: x["x0"]))
                    cur, cur_top = [w], w["top"]
            if cur:
                lines.append(sorted(cur, key=lambda x: x["x0"]))
    return [[dict(text=fix_ligatures(w["text"]), x0=w["x0"], x1=w["x1"]) for w in ln] for ln in lines]


def _lines_from_text(path):
    lines = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.expandtabs(8)
            toks = [dict(text=m.group(), x0=m.start() * 5, x1=m.end() * 5) for m in re.finditer(r"\S+", raw)]
            if toks:
                lines.append(toks)
    return lines


STAR_RE = re.compile(r"^[\*\u2020\u2021\u2605\u2606]+$")


def norm_answer(a):
    a = re.sub(r"\s+", " ", str(a)).strip()
    a = re.sub(r"\s*[\*\u2020\u2021\u2605]+$", "", a)
    m = re.fullmatch(r"\(?([A-Ha-h])[\)\.]?", a)
    if m:
        return m.group(1).upper()
    return re.sub(r"\s*,\s*", ",", a)


def parse_key(path, pages=None):
    warnings = []
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            data = {str(i + 1): v for i, v in enumerate(data)}
        return {str(k): norm_answer(",".join(map(str, v)) if isinstance(v, list) else v)
                for k, v in data.items()}, warnings

    lines = _lines_from_pdf(path, pages) if ext == ".pdf" else _lines_from_text(path)
    def scan(lines, permissive=False):
        answers, warnings = {}, []
        for toks in lines:
            toks = [t for t in toks if not LABEL_RE.match(t["text"]) and not STAR_RE.match(t["text"])]
            toks = [dict(t, text=t["text"].lstrip("*\u2605\u2020\u2021") or t["text"]) for t in toks]
            merged_toks = []
            for t in toks:
                if merged_toks and re.fullmatch(r"[\)\.:\-–]", t["text"]) and re.fullmatch(r"\d{1,3}", merged_toks[-1]["text"]):
                    merged_toks[-1] = dict(text=merged_toks[-1]["text"] + t["text"], x0=merged_toks[-1]["x0"], x1=t["x1"])
                else:
                    merged_toks.append(t)
            toks = merged_toks
            if toks and re.match(r"^(?:notes?|explanations?)\b", toks[0]["text"], re.I):
                break
            markers = []
            for i, t in enumerate(toks):
                inline = None
                m = MARKER_FULL.match(t["text"]) or (MARKER_JUNK.match(t["text"]) if permissive else None)
                if m:
                    num = int(m.group(1))
                else:
                    m = MARKER_INLINE.match(t["text"])
                    if not m:
                        continue
                    num = int(m.group(1))
                    inline = m.group(3) or m.group(4)
                starts_entry = (i == 0 or t["x0"] - toks[i - 1]["x1"] > GAP_PT
                                or (markers and num == markers[-1][1] + 1))
                if starts_entry:
                    markers.append((i, num, inline))
            for j, (idx, num, inline) in enumerate(markers):
                end = markers[j + 1][0] if j + 1 < len(markers) else len(toks)
                parts = ([inline] if inline else []) + [t["text"] for t in toks[idx + 1:end]]
                ans = norm_answer(" ".join(parts)) if parts else ""
                if str(num) in answers:
                    warnings.append(f"duplicate entry for question {num} (kept the first)")
                elif ans:
                    answers[str(num)] = ans
                else:
                    warnings.append(f"question {num} has no answer text")

        return answers, warnings

    answers, warnings = scan(lines)
    if not answers:
        relaxed = []
        for toks in lines:
            if toks and re.fullmatch(r"\d{1,3},?", toks[0]["text"]):
                toks = [dict(toks[0], text=toks[0]["text"].rstrip(",") + ")")] + toks[1:]
            elif toks and re.match(r"^\d{1,3},\S", toks[0]["text"]):
                n, rest = toks[0]["text"].split(",", 1)
                toks = [dict(toks[0], text=n + ")"), dict(toks[0], text=rest)] + toks[1:]
            relaxed.append(toks)
        answers, warnings = scan(relaxed)

    if answers:
        nums = sorted(int(k) for k in answers)
        if any(n not in nums for n in range(1, nums[-1] + 1)):
            more, more_w = scan(lines, permissive=True)
            if len(more) > len(answers):
                answers, warnings = more, more_w

    if not answers:
        warnings.append("no answers found - unsupported key layout (scanned image?)")
    else:
        nums = sorted(int(k) for k in answers)
        gaps = [n for n in range(1, nums[-1] + 1) if n not in nums]
        if gaps:
            warnings.append(f"key has no entry for question(s): {gaps}")
    return answers, warnings


def validate_answers(manifest):
    """Cross-check the key against what each question actually offers."""
    warns = []
    ids = {q["id"] for q in manifest["questions"]}
    for q in manifest["questions"]:
        a = q["answer"]
        if a is None:
            warns.append(f"Q{q['id']}: no answer in key")
        elif q["choices"] and a not in q["choices"]:
            warns.append(f"Q{q['id']}: answer '{a}' is not one of its choices {''.join(q['choices'])}")
        elif not q["choices"] and re.fullmatch(r"[A-E]", a):
            warns.append(f"Q{q['id']}: looks free-response but key says letter '{a}'")
    return warns, ids


def render_pages(pdf_path, dpi, first, last, tmpdir):
    subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(first), "-l", str(last),
                    pdf_path, f"{tmpdir}/pg"], check=True)
    return {int(re.search(r"-(\d+)\.png$", f).group(1)): f for f in glob.glob(f"{tmpdir}/pg-*.png")}


KEY_PAGE_RE = re.compile(r"(?im)^\s*(?:[\*\u2605\s]*ANSWER\s+KEY\b.*|KEY)\s*$")


def find_embedded_key(pdf_path, question_count=None):
    """Look for an answer-key page inside the test PDF itself (some packets append their key).
    Returns (answers, warnings, page_numbers) or None. Only pages headed 'KEY' / 'ANSWER KEY' are read,
    so explanation pages that follow the key table are never mistaken for it."""
    with pdfplumber.open(pdf_path) as pdf:
        pages = []
        for pi, page in enumerate(pdf.pages):
            text = fix_ligatures(page.extract_text() or "")
            if KEY_PAGE_RE.search(text) and not find_headers(page):
                pages.append(pi)
    if not pages:
        return None
    answers, warnings = parse_key(pdf_path, set(pages))
    need = max(5, int(0.5 * question_count)) if question_count else 5
    if len(answers) < need:
        return None
    return answers, warnings, [p + 1 for p in pages]


MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
CONTEST_RE = re.compile(r"Contest\s*#\s*([A-Za-z0-9][\w-]*)")
CONTEST_KEY_RE = re.compile(r"Contest\s*#\s*[A-Za-z0-9][\w-]*\s+KEY\b", re.I)
DATE_RE = re.compile(rf"\b(?:{MONTHS})\s+\d{{1,2}},\s*\d{{4}}\b")


def read_contest_meta(pdf_path, max_pages=8):
    """Pull 'Contest #2324-10', the date and whether it's a KEY from the first few pages. {} if absent."""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:max_pages]:
            text = page.extract_text() or ""
            m = CONTEST_RE.search(text)
            if not m:
                continue
            cid = m.group(1)
            d = DATE_RE.search(text, m.end())
            season = cid[:4] if re.match(r"^\d{4}-", cid) else None
            return {"contest": cid, "date": d.group(0) if d else None, "season": season,
                    "title": f"A+ Computer Science Contest #{cid}",
                    "is_key": bool(CONTEST_KEY_RE.search(text))}
    return {}


MONTH_NUM = {m.lower(): i for i, m in enumerate(MONTHS.split("|"), 1)}
MONTH_YEAR_RE = re.compile(rf"\b({MONTHS})\s+(\d{{4}})\b", re.I)
FULL_DATE_RE = re.compile(rf"\b({MONTHS})\s+(\d{{1,2}}),\s*(\d{{4}})\b", re.I)
SEASON_RANGE_RE = re.compile(r"\b(20\d\d)\s*[-\u2013]\s*(20\d\d|\d\d)\b")
SMALL_WORDS = {"of", "the", "and", "for", "to", "in", "on", "at", "an", "vs"}
SLUG_FILLER = {"computer", "science", "written", "test", "contest", "exam", "hs", "the", "of", "and", "only"}


def smart_title(line):
    """ALL-CAPS cover lines -> Title Case, keeping short acronyms (UIL, JET) and single letters."""
    line = re.sub(r"\s+", " ", line).strip()
    if line != line.upper():
        return line
    out = []
    for i, w in enumerate(line.split()):
        if re.fullmatch(r"[A-Z]{1,3}", w) and (w.lower() not in SMALL_WORDS or i == 0):
            out.append(w)
        elif w.lower() in SMALL_WORDS and i:
            out.append(w.lower())
        else:
            out.append(w.capitalize() if w.isalpha() else w)
    return " ".join(out)


def season_from_text(text):
    """'2024-2025' -> '2425'. None if there is no consecutive-year range."""
    for m in SEASON_RANGE_RE.finditer(text or ""):
        y1, y2 = int(m.group(1)), int(m.group(2)[-2:])
        if y2 == (y1 + 1) % 100:
            return f"{y1 % 100:02d}{y2:02d}"
    return None


def season_from_date(date_text):
    """Aug-Dec start a school year, Jan-Jul finish it: 'October 2026' -> '2627', 'January 2025' -> '2425'."""
    m = FULL_DATE_RE.search(date_text or "")
    month, year = (m.group(1), int(m.group(3))) if m else (None, None)
    if not m:
        m = MONTH_YEAR_RE.search(date_text or "")
        month, year = (m.group(1), int(m.group(2))) if m else (None, None)
    if not month:
        return None
    start = year if MONTH_NUM[month.lower()] >= 8 else year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def baseline_lines(page, tol=1.0):
    """Text lines grouped by character baseline (not top), so small-caps and mixed-size headings stay together."""
    chars = [c for c in page.chars if c.get("matrix")]
    chars.sort(key=lambda c: -c["matrix"][5])
    rows, cur, cur_y = [], [], None
    for c in chars:
        y = c["matrix"][5]
        if cur_y is None or abs(y - cur_y) <= tol:
            cur.append(c)
            cur_y = y if cur_y is None else cur_y
        else:
            rows.append(cur)
            cur, cur_y = [c], y
    if cur:
        rows.append(cur)
    lines = []
    for row in rows:
        row.sort(key=lambda c: c["x0"])
        out, prev = [], None
        for c in row:
            if prev is not None and c["x0"] - prev["x1"] > 0.25 * max(prev.get("size", 10), 1) and not c["text"].isspace() \
                    and not out[-1].isspace():
                out.append(" ")
            out.append(c["text"])
            prev = c
        line = re.sub(r"\s+", " ", fix_ligatures("".join(out))).strip()
        if line:
            lines.append(line)
    return lines


def _cover_lines(pages_text):
    """Title block of a packet: the lines that sit above 'General Directions', minus running headers/junk."""
    first_lines = [next((l.strip() for l in t.splitlines() if l.strip()), "") for t in pages_text]
    for i, text in enumerate(pages_text):
        m = re.search(r"General\s+Directions", text, re.I)
        if not m:
            continue
        lines = [l.strip() for l in text[:m.start()].splitlines() if l.strip()]
        header = None
        if lines and sum(f.lower() == lines[0].lower() for f in first_lines) >= 2:
            header = lines.pop(0)
        keep = []
        for l in lines:
            alnum = sum(c.isalnum() for c in l)
            if l.startswith("\u00a9") or "www." in l.lower() or re.search(r"do not open", l, re.I) or alnum < 0.5 * len(l):
                continue
            keep.append(l)
        return keep, header
    return [], None


def infer_meta(pdf_path):
    """Title / date / season for a packet. A+ contests are identified by their contest number; other
    packets by their cover page. Returns {} when nothing usable is found (caller falls back to the file name)."""
    meta = read_contest_meta(pdf_path)
    if meta.get("contest"):
        return meta
    with pdfplumber.open(pdf_path) as pdf:
        texts = ["\n".join(baseline_lines(p)) for p in pdf.pages[:6]]
    lines, header = _cover_lines(texts)
    if not lines:
        return {}
    date, title_lines = None, []
    for l in lines:
        if date is None and (FULL_DATE_RE.fullmatch(l) or MONTH_YEAR_RE.fullmatch(l)):
            date = smart_title(l)
        else:
            title_lines.append(smart_title(l))
    title = " \u2013 ".join(title_lines[:3]) or None
    season = (season_from_text(" ".join(lines + ([header] if header else [])))
              or season_from_text("\n".join(texts[:3])) or season_from_date(date))
    out = {"title": title, "date": date, "season": season, "contest": None}
    return {k: v for k, v in out.items() if v}


def packet_id_from_meta(meta, fallback):
    """Readable, stable folder name: the contest number if there is one, else a slug of the cover title."""
    if meta.get("contest"):
        return re.sub(r"[^A-Za-z0-9._-]+", "_", meta["contest"]).strip("_")
    if meta.get("title"):
        tokens = re.findall(r"\d{4}-\d{2,4}|[a-z0-9]+", meta["title"].lower())
        tokens = [t for t in tokens if t not in SLUG_FILLER]
        if tokens:
            return "-".join(tokens)[:60].strip("-")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", fallback).strip("_") or "packet"


def has_question_headers(pdf_path, max_pages=8):
    with pdfplumber.open(pdf_path) as pdf:
        return any(find_headers(p) for p in pdf.pages[:max_pages])


def is_key_pdf(pdf_path):
    """True if the file is a standalone answer key: headed KEY / ANSWER KEY (or 'Contest #... KEY') and has no
    'QUESTION n' headers of its own."""
    with pdfplumber.open(pdf_path) as pdf:
        pages = pdf.pages[:6]
        if any(find_headers(p) for p in pages):
            return False
        text = "\n".join(fix_ligatures(p.extract_text() or "") for p in pages[:2])
    return bool(CONTEST_KEY_RE.search(text) or KEY_PAGE_RE.search(text))


def attach_key(packet_dir, key_path):
    """Merge an answer key (pdf / txt / csv / json) into an already-extracted packet folder, no re-render.
    Returns (manifest, problems)."""
    mpath = os.path.join(packet_dir, "manifest.json")
    with open(mpath, encoding="utf-8") as f:
        manifest = json.load(f)
    answers, warnings = parse_key(key_path)
    if not answers:
        raise ValueError("no answers could be read from that key: " + "; ".join(warnings))
    for q in manifest["questions"]:
        q["answer"] = answers.get(str(q["id"]))
    v, ids = validate_answers(manifest)
    extra = sorted(int(k) for k in answers if int(k) not in ids)
    problems = list(warnings) + v + ([f"key has answers for questions not in the test: {extra}"] if extra else [])
    manifest["problems"] = problems
    manifest["key_source"] = os.path.basename(key_path)
    if isinstance(manifest.get("sources"), dict):
        h = hashlib.sha1()
        with open(key_path, "rb") as f:
            h.update(f.read())
        manifest["sources"]["key_sha1"] = h.hexdigest()
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    with open(os.path.join(packet_dir, "answer_key.json"), "w") as f:
        json.dump(dict(sorted(answers.items(), key=lambda kv: int(kv[0]))), f, indent=2)
    return manifest, problems


def trim_whitespace(img, pad=16, inset=14):
    """Crop away the blank margins (and the cell border) around content."""
    from PIL import ImageChops
    rgb = img.convert("RGB")
    inner = rgb.crop((inset, inset, rgb.width - inset, rgb.height - inset))
    bbox = ImageChops.difference(inner, Image.new("RGB", inner.size, (255, 255, 255))).point(
        lambda p: 255 if p > 40 else 0).getbbox()
    if not bbox:
        return img
    l, t, r, b = bbox
    return rgb.crop((max(0, l + inset - pad), max(0, t + inset - pad),
                     min(rgb.width, r + inset + pad), min(rgb.height, b + inset + pad)))


def process(pdf_path, out_dir, dpi=200, answers_path=None, extra=None):
    """Extract one test PDF into out_dir (images/ + manifest.json).

    Returns (manifest, problems). Raises ValueError if the PDF has no 'QUESTION n' headers.
    `extra` is a dict merged into the manifest (the ingester stores source hashes here).
    """
    img_dir = os.path.join(out_dir, "images")
    os.makedirs(img_dir, exist_ok=True)
    scale = dpi / 72

    answers, key_warnings, key_source = {}, [], None
    if answers_path:
        answers, key_warnings = parse_key(answers_path)
        key_source = os.path.basename(answers_path)

    questions, page_of, merged, text_based = {}, {}, set(), []
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            headers = find_headers(page)
            if not headers:
                continue
            qs, mwn = analyze_page(page, headers)
            for qn, d in qs.items():
                questions[qn], page_of[qn] = d, pno
            merged |= mwn
            text_based += text_groups(page, {q for q, _ in headers})
        if not questions:
            raise ValueError("No 'QUESTION n' / 'Question n' headers found in the left margin of any page - is this a scanned "
                             "(image-only) PDF or a different layout?")

        groups = build_groups(merged, text_based)
        group_of = {q: g for g in groups for q in range(g[0], g[1] + 1)}

        if not answers_path:
            emb = find_embedded_key(pdf_path, len(questions))
            if emb:
                answers, key_warnings, kpages = emb
                key_source = "embedded (page%s %s)" % ("s" if len(kpages) > 1 else "", ", ".join(map(str, kpages)))

        with tempfile.TemporaryDirectory() as tmp:
            rendered = render_pages(pdf_path, dpi, min(page_of.values()), max(page_of.values()), tmp)

            def crop(pno, top, bottom, x0, x1):
                with Image.open(rendered[pno]) as im:
                    return im.crop((int(x0 * scale), int(top * scale), int(x1 * scale), int(bottom * scale)))

            manifest = {"source": os.path.basename(pdf_path), "dpi": dpi,
                        "question_count": len(questions), "groups": [], "questions": []}
            meta = infer_meta(pdf_path)
            for k in ("contest", "date", "season", "title"):
                if meta.get(k):
                    manifest[k] = meta[k]
            if extra:
                manifest.update(extra)

            for qn in sorted(questions):
                d, pno = questions[qn], page_of[qn]
                grouped = qn in group_of
                x1 = d["divider"] + 1 if grouped else d["right"]
                fname = f"q{qn:02d}.png"
                crop(pno, d["top"], d["bottom"], d["left"], x1).save(os.path.join(img_dir, fname))


                tx1 = d["right"] if d["full_width"] else d["divider"]
                text = fix_ligatures(pdf.pages[pno - 1].crop((d["left"], d["top"], tx1, d["bottom"])).extract_text() or "")
                text = HEADER_TEXT_RE.sub("", text.strip())
                choices = parse_choices(text)

                manifest["questions"].append({
                    "id": qn, "page": pno, "image": f"images/{fname}", "group": None,
                    "text": text, "choices": choices, "answer_type": "choice" if choices else "free",
                    "answer": answers.get(str(qn)),
                })

            for a, b in groups:
                da, db = questions[a], questions[b]
                gname, cname = f"group_{a}-{b}.png", f"group_{a}-{b}_code.png"
                crop(page_of[a], da["top"], db["bottom"], da["left"], da["right"]).save(os.path.join(img_dir, gname))

                trim_whitespace(crop(page_of[a], da["top"], db["bottom"], da["divider"] - 1, da["right"])).save(
                    os.path.join(img_dir, cname))
                members = list(range(a, b + 1))
                manifest["groups"].append({"id": f"{a}-{b}", "image": f"images/{gname}",
                                           "code_image": f"images/{cname}", "questions": members})
                for q in manifest["questions"]:
                    if q["id"] in members:
                        q["group"] = f"{a}-{b}"

    problems = []
    nums = sorted(questions)
    gaps = [n for n in range(nums[0], nums[-1] + 1) if n not in questions]
    if gaps:
        problems.append(f"missing question numbers: {gaps}")
    manifest["key_source"] = key_source
    if answers:
        problems += key_warnings
        v, ids = validate_answers(manifest)
        problems += v
        extra_ids = sorted(int(k) for k in answers if int(k) not in ids)
        if extra_ids:
            problems.append(f"key has answers for questions not in the test: {extra_ids}")
        with open(os.path.join(out_dir, "answer_key.json"), "w") as f:
            json.dump(dict(sorted(answers.items(), key=lambda kv: int(kv[0]))), f, indent=2)
    manifest["problems"] = problems

    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    return manifest, problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="?", help="the test PDF (omit when only using --parse-key)")
    ap.add_argument("-o", "--out", help="output directory (default: out/<pdf name>)")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--answers", help="answer key to merge: .pdf, .txt/.csv, or .json")
    ap.add_argument("--parse-key", metavar="KEY", help="just parse an answer key, print it, and write answer_key.json")
    ap.add_argument("--strict", action="store_true", help="exit with an error if key validation finds problems")
    args = ap.parse_args()

    if args.parse_key:
        ans, warns = parse_key(args.parse_key)
        out_dir = args.out or os.path.join("out", os.path.splitext(os.path.basename(args.parse_key))[0])
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "answer_key.json"), "w") as f:
            json.dump(dict(sorted(ans.items(), key=lambda kv: int(kv[0]))), f, indent=2)
        for k in sorted(ans, key=int):
            print(f"{int(k):>3}) {ans[k]}")
        print(f"{len(ans)} answers -> {os.path.join(out_dir, 'answer_key.json')}")
        for w in warns:
            print("WARNING:", w)
        return
    if not args.pdf:
        ap.error("provide the test PDF (or use --parse-key)")

    out_dir = args.out or os.path.join("out", os.path.splitext(os.path.basename(args.pdf))[0])
    try:
        manifest, problems = process(args.pdf, out_dir, args.dpi, args.answers)
    except ValueError as e:
        sys.exit(str(e))

    print(f"{manifest['question_count']} questions, {len(manifest['groups'])} groups -> {out_dir}")
    if manifest.get("contest"):
        print(f"contest {manifest['contest']}" + (f" ({manifest['date']})" if manifest.get("date") else ""))
    print("groups:", [g["id"] for g in manifest["groups"]] or "none")
    n = sum(q["answer"] is not None for q in manifest["questions"])
    if n:
        print(f"answers merged: {n}/{manifest['question_count']}  (key: {manifest['key_source']})")
    else:
        print("no answer key found. Add one with --answers key.pdf (or .txt/.json)")
    for p in problems:
        print("WARNING:", p)
    if args.strict and problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
