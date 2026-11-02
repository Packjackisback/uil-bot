import argparse
import json
import random
from pathlib import Path

# WARNING - AI GENERATED CODE INBOUND

def make_packet(root, pid, *, season="2324", date="January 27, 2024", n=40, answered=True,
                groups=((21, 22),), images=False, seed=0, tf=(5, 17), free=(39, 40)):
    rnd = random.Random(f"{pid}-{seed}")
    root = Path(root) / pid
    (root / "images").mkdir(parents=True, exist_ok=True)
    gmap = {q: f"{a}-{b}" for a, b in groups for q in range(a, b + 1)}
    questions = []
    for q in range(1, n + 1):
        if q in free:
            choices, ans = [], rnd.choice(["4", "-4,9,17", "01111001"])
        elif q in tf:
            choices, ans = ["A", "B"], rnd.choice("AB")
        else:
            choices, ans = list("ABCDE"), rnd.choice("ABCDE")
        questions.append({"id": q, "page": 4 + q // 8, "image": f"images/q{q:02d}.png", "group": gmap.get(q),
                          "text": f"What is output by question {q}?", "choices": choices,
                          "answer_type": "choice" if choices else "free", "answer": ans if answered else None})
    gl = [{"id": f"{a}-{b}", "image": f"images/group_{a}-{b}.png", "code_image": f"images/group_{a}-{b}_code.png",
           "questions": list(range(a, b + 1))} for a, b in groups if b <= n]
    manifest = {"source": f"{pid}_TEST.pdf", "dpi": 200, "question_count": n, "groups": gl, "questions": questions,
                "contest": pid, "date": date, "season": season, "title": f"A+ Computer Science Contest #{pid}",
                "packet_id": pid, "problems": []}
    (root / "manifest.json").write_text(json.dumps(manifest))
    if images:
        from PIL import Image
        files = [q["image"] for q in questions] + [g["code_image"] for g in gl] + [g["image"] for g in gl]
        for f in files:
            Image.new("RGB", (320, 120), "white").save(root / f)
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--out", required=True)
    ap.add_argument("--images", action="store_true")
    a = ap.parse_args()
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December"]
    for i in range(a.count):
        yy = 10 + i % 16
        n = i // 16 + 1
        season = f"{yy:02d}{yy + 1:02d}"
        make_packet(a.out, f"{season}-{n:03d}", season=season,
                    date=f"{months[n % 12]} {1 + n % 28}, 20{yy}", images=a.images, seed=i,
                    answered=(i % 25 != 0))
    print(f"wrote packets to {a.out}")


if __name__ == "__main__":
    main()
