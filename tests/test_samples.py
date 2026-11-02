import os
from pathlib import Path

import pytest

import extract_uil as eu

# WARNING - AI GENERATED CODE INBOUND


SAMPLES = Path(os.environ.get("UIL_SAMPLE_DIR", "/nonexistent"))
CASES = {
    "test_10_January_27_2024_TEST.pdf": dict(groups=["21-22", "23-24", "27-28", "32-33"], free=[39, 40], contest="2324-10",
                                             answered=0),
    "2021-2022_HS_VIRTUAL_CHALLENGE_MEET_2_-_COMPUTER_SCIENCE_-_TEST_ONLY.pdf": dict(
        groups=["22-23", "25-26", "27-28", "31-32", "33-35"], free=[37, 38, 39, 40], answered=0),
    "JETCompSciWritten-2026.pdf": dict(groups=["18-20", "21-23", "25-26", "27-32", "34-35"], free=[39, 40], answered=0,
                                       odd_choices={35: "ABCDEF"}),
    "CompSciWritten_StudyPacket_A_25.pdf": dict(groups=["18-19", "21-23", "24-27", "29-30", "31-34", "35-38"], free=[39, 40],
                                                answered=40, odd_choices={5: "ABC"}, answers={39: "-103", 40: "A D F C G A"}),
}


@pytest.mark.parametrize("name", list(CASES))
def test_real_packet(name, tmp_path):
    pdf = SAMPLES / name
    if not pdf.exists():
        pytest.skip(f"{name} not found in UIL_SAMPLE_DIR")
    want = CASES[name]
    m, problems = eu.process(str(pdf), str(tmp_path))
    assert m["question_count"] == 40 and [q["id"] for q in m["questions"]] == list(range(1, 41))
    assert [g["id"] for g in m["groups"]] == want["groups"]
    assert [q["id"] for q in m["questions"] if not q["choices"]] == want["free"]
    for qid, letters in want.get("odd_choices", {}).items():
        assert "".join(m["questions"][qid - 1]["choices"]) == letters
    assert sum(q["answer"] is not None for q in m["questions"]) == want["answered"]
    for qid, ans in want.get("answers", {}).items():
        assert m["questions"][qid - 1]["answer"] == ans
    if want["answered"]:
        assert problems == []
    if "contest" in want:
        assert m["contest"] == want["contest"]
    for g in m["groups"]:
        assert (tmp_path / g["code_image"]).is_file() and (tmp_path / g["image"]).is_file()
    for q in m["questions"]:
        assert (tmp_path / q["image"]).is_file()
