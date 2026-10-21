import json

import validate_questions as vq


def question(title, tag):
    return {
        "know_id": 999,
        "type": "judge",
        "tags": tag,
        "source": "《测试书》",
        "title": title,
        "parse": "解析",
        "difficulty": 0.5,
        "answer": "是",
    }


def test_cross_chapter_exact_duplicate_is_error():
    errors, warnings = [], []
    vq.cross_chapter_dups(
        [("第一章", "下列哪项说法正确？"), ("第二章", "下列哪项说法正确？")],
        errors,
        warnings,
    )
    assert any("跨章重复题" in e for e in errors)
    assert not warnings


def test_same_chapter_duplicate_not_reported():
    errors, warnings = [], []
    vq.cross_chapter_dups([("第一章", "题A"), ("第一章", "题A")], errors, warnings)
    assert not errors
    assert not warnings


def test_near_duplicate_across_chapters_warns():
    errors, warnings = [], []
    vq.cross_chapter_dups(
        [
            ("第一章", "下列哪一项关于投影的说法是正确的描述"),
            ("第二章", "下列哪一项关于投影的说法是正确的表述"),
        ],
        errors,
        warnings,
    )
    assert not errors
    assert any("跨章近似题" in w for w in warnings)


def test_book_json_state_is_written(tmp_path):
    qdir = tmp_path / "questions"
    qdir.mkdir()
    (qdir / "第一章.json").write_text(
        json.dumps([question("第一题", "测试书-第一章")], ensure_ascii=False),
        encoding="utf-8",
    )
    (qdir / "第二章.json").write_text(
        json.dumps([question("第二题", "测试书-第二章")], ensure_ascii=False),
        encoding="utf-8",
    )
    book = tmp_path / "book.json"
    book.write_text(json.dumps({"stages": {}}), encoding="utf-8")

    rc = vq.main(
        [
            "--id",
            "999",
            "--book",
            "测试书",
            "--questions-dir",
            str(qdir),
            "--book-json",
            str(book),
        ]
    )

    assert rc == 0
    stages = json.loads(book.read_text(encoding="utf-8"))["stages"]
    assert stages["questions"] == "done"
    assert stages["compact"] == "pending"
