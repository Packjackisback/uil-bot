import asyncio
import json
from types import SimpleNamespace

import pytest

import extract_uil as eu
import quiz_core
from packets import _date_iso
from tools.make_fake_packets import make_packet

# WARNING - AI GENERATED CODE INBOUND


def test_fix_ligatures_handles_cid_and_unicode_forms():
    assert eu.fix_ligatures("Ques(cid:415)on wri(cid:425)en a(cid:332)er") == "Question written after"
    assert eu.fix_ligatures("Ques\u019fon wri\u01a9en a\u014cer") == "Question written after"
    assert eu.fix_ligatures("x(cid:99999)y") == "xy"
    assert eu.fix_ligatures("B(cid:3364)") == "B\u0304"
    assert eu.fix_ligatures("") == "" and eu.fix_ligatures(None) is None


@pytest.mark.parametrize("text,expected", [
    ("A. 9 B. 18 C. 15 D. 13 E. 14", "ABCDE"),
    ("A) null B) [] C) [67] D) x E) y", "ABCDE"),
    ("A) a B) b\nC) c D) d\nE) Both A and B F) Both C and D", "ABCDEF"),
    ("D) A and B.\nE) Any of the above.", ""),
    ("A) x B) y C) z D) Either A or B. E) None of the above.", "ABCDE"),
    ("A) true B) false", "AB"),
    ("A) yes", ""),
    ("How many leaves are in the tree? 6 35 86 9", ""),
    ("Answer is super.A(7, s) here", ""),
])
def test_parse_choices(text, expected):
    assert "".join(eu.parse_choices(text)) == expected



def test_key_parser_footnote_stars_watermark_and_spaced_answers(tmp_path):
    k = tmp_path / "key.txt"
    k.write_text("1) E        11) D        21) A\n"
                 "2) D        12) A        22) B\n"
                 "3) A        K13) A       23) C\n"
                 "29) D*      *39) -103\n"
                 "30) D       *40) A D F C G A\n"
                 "Explanations:\n1. E All of the values are equivalent\n")
    ans, warn = eu.parse_key(str(k))
    assert ans["13"] == "A" and ans["29"] == "D" and ans["39"] == "-103" and ans["40"] == "A D F C G A"
    assert "1" in ans and ans["1"] == "E"
    assert len([w for w in warn if "duplicate" in w]) == 0


def test_key_parser_accepts_letters_up_to_h():
    assert eu.norm_answer("(F)") == "F" and eu.norm_answer("h.") == "H" and eu.norm_answer("D*") == "D"
    assert eu.norm_answer("-4, -9, 9") == "-4,-9,9"


def test_attach_key_merges_answers_and_flags_problems(tmp_path):
    make_packet(tmp_path, "p1", answered=False, tf=(5,), free=(39, 40))
    key = {str(i): "A" for i in range(1, 41)}
    key["39"], key["40"], key["5"] = "7", "4", "D"
    (tmp_path / "key.json").write_text(json.dumps(key))
    manifest, problems = eu.attach_key(str(tmp_path / "p1"), str(tmp_path / "key.json"))
    assert sum(q["answer"] is not None for q in manifest["questions"]) == 40
    assert any("Q5" in p for p in problems)
    saved = json.loads((tmp_path / "p1" / "manifest.json").read_text())
    assert saved["key_source"] == "key.json" and saved["questions"][38]["answer"] == "7"
    with pytest.raises(ValueError):
        (tmp_path / "empty.txt").write_text("nothing here")
        eu.attach_key(str(tmp_path / "p1"), str(tmp_path / "empty.txt"))



def test_season_helpers():
    assert eu.season_from_text("UIL Written Test - 2024-2025 Invitational A") == "2425"
    assert eu.season_from_text("2021 - 2022") == "2122"
    assert eu.season_from_text("2020-2030") is None
    assert eu.season_from_date("October 2026") == "2627"
    assert eu.season_from_date("January 2025") == "2425"
    assert eu.season_from_date("January 27, 2024") == "2324"
    assert eu.season_from_date("August 2025") == "2526" and eu.season_from_date("July 2025") == "2425"
    assert eu.season_from_date("no date here") is None


def test_smart_title_and_packet_ids():
    assert eu.smart_title("UIL COMPUTER SCIENCE WRITTEN TEST") == "UIL Computer Science Written Test"
    assert eu.smart_title("2025 INVITATIONAL A") == "2025 Invitational A"
    assert eu.smart_title("HS Virtual Challenge Meet #2") == "HS Virtual Challenge Meet #2"
    pid = eu.packet_id_from_meta
    assert pid({"contest": "2324-10"}, "x") == "2324-10"
    assert pid({"title": "UIL Computer Science Written Test \u2013 2025 Invitational A"}, "x") == "uil-2025-invitational-a"
    assert pid({"title": "JET Computer Science Written Test \u2013 Halloween 2026"}, "x") == "jet-halloween-2026"
    assert pid({"title": "2021-2022 \u2013 HS Virtual Challenge Meet #2 \u2013 Computer Science"}, "x") == "2021-2022-virtual-challenge-meet-2"
    assert pid({}, "My Weird File (1)") == "My_Weird_File_1"


def test_date_iso_formats():
    assert _date_iso("January 27, 2024") == "2024-01-27"
    assert _date_iso("October 2026") == "2026-10-01" and _date_iso("OCTOBER 2026") == "2026-10-01"
    assert _date_iso(None, "2122") == "2021-08-01"
    assert _date_iso(None, None) == "" and _date_iso("garbage", None) == ""



@pytest.mark.parametrize("given,ok", [("A D F C G A", True), ("ADFCGA", True), ("a,d,f,c,g,a", True), ("A D F C G", False),
                                      ("A D F G C A", False)])
def test_spaced_letter_key_accepts_run_together(given, ok):
    assert quiz_core.check_answer("free", given, "A D F C G A") is ok


@pytest.mark.parametrize("given,key,ok", [("10, 20", "10 20", True), ("-103", "-103", True), ("-103.0", "-103", True),
                                          ("4 5", "4", False), ("f", "F", True)])
def test_more_answer_forms(given, key, ok):
    kind = "choice" if key.isalpha() else "free"
    assert quiz_core.check_answer(kind, given, key) is ok


def test_six_choice_question_gets_six_buttons_in_two_rows():
    async def go():
        view = quiz_core.build_view(1, SimpleNamespace(answer_type="choice", choices="ABCDEF"))
        assert [b.item.label for b in view.children] == list("ABCDEF") + ["Report"]
        assert [len(r["components"]) for r in view.to_components()] == [5, 2]
    asyncio.run(go())
