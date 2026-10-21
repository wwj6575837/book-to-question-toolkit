import copy
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "补题" / "命题官批量生成.py"

spec = importlib.util.spec_from_file_location("mingtiguan_batch", SCRIPT)
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)


def choice(answer="A", parse="本题选 **A**。", keys="ABCD"):
    return {
        "type": "choice",
        "title": "题",
        "options": [{"key": k, "name": f"选项{k}"} for k in keys],
        "answer": answer,
        "parse": parse,
        "difficulty": 0.5,
        "know_id": 1,
        "tags": "t",
        "source": "《s》",
    }


def test_choice_letters_are_balanced():
    items = [choice() for _ in range(40)]

    stats = mb.balance_chapter(items, "1|第一章")

    counts = {k: 0 for k in "ABCD"}
    for q in items:
        counts[q["answer"]] += 1
    assert max(counts.values()) - min(counts.values()) <= 1
    assert sum(counts.values()) == 40
    assert stats["choice"] == 30
    assert stats["already"] == 10


def test_balance_is_idempotent():
    items = [choice() for _ in range(12)]
    mb.balance_chapter(items, "1|第一章")
    snapshot = [(q["answer"], [o["key"] for o in q["options"]]) for q in items]

    mb.balance_chapter(items, "1|第一章")

    assert [(q["answer"], [o["key"] for o in q["options"]]) for q in items] == snapshot


def test_parse_letters_are_remapped():
    mapping = {"A": "C", "B": "A", "C": "D", "D": "B"}
    text = "本题选 **B**。A 为干扰项，C 项错误，故选B。"
    assert mb._remap_parse(text, mapping) == "本题选 **A**。C 为干扰项，D 项错误，故选A。"


def test_unmappable_parse_is_skipped_and_unchanged():
    q = choice(parse="维生素C含量较高，故选A。")
    original = copy.deepcopy(q)

    stats = mb.balance_chapter([q], "1|第一章")

    assert stats["skipped"] == 1
    assert q == original


def test_two_option_questions_never_get_c_or_d():
    items = [choice(keys="AB") for _ in range(10)]

    mb.balance_chapter(items, "1|第一章")

    assert all(q["answer"] in "AB" for q in items)


def test_group_subquestions_are_balanced():
    group = {
        "type": "group",
        "title": "场景",
        "options": [],
        "answer": "",
        "parse": "",
        "difficulty": 0.7,
        "subs": [choice(), choice()],
    }

    mb.balance_chapter([group], "1|第一章")

    answers = [sub["answer"] for sub in group["subs"]]
    assert answers == ["A", "B"]


def test_multiple_choice_is_deterministic():
    def question():
        return {
            "type": "multiple",
            "title": "题",
            "options": [{"key": k, "name": f"选项{k}"} for k in "ABCD"],
            "answer": "ABC",
            "parse": "本题选 **ABC**。",
            "difficulty": 0.6,
            "know_id": 1,
            "tags": "t",
            "source": "《s》",
        }

    first = question()
    mb.balance_chapter([first], "1|第一章")
    result = (first["answer"], [o["key"] for o in first["options"]])

    second = question()
    mb.balance_chapter([second], "1|第一章")

    assert (second["answer"], [o["key"] for o in second["options"]]) == result
    assert len(first["answer"]) == 3
