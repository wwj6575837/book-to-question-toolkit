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


def option_question(title, tag, typ="choice", answer="A", count=4):
    keys = "ABCDEFGH"[:count]
    return {
        "know_id": 999,
        "type": typ,
        "tags": tag,
        "source": "《测试书》",
        "title": title,
        "options": [{"key": k, "name": f"选项{k}"} for k in keys],
        "answer": answer,
        "parse": f"本题选 **{answer}**。",
        "difficulty": 0.5,
    }


def answer_pairs(typ, answers, count=4):
    return [(typ, answer, count) for answer in answers]


def test_single_choice_concentration_warns():
    warnings = []
    data = answer_pairs("choice", ["A"] * 10)

    vq.check_answer_balance({"第一章": data}, data, warnings)

    assert any("单选答案集中" in w for w in warnings)
    assert any("缺失" in w for w in warnings)


def test_balanced_single_choice_has_no_warning():
    warnings = []
    data = answer_pairs("choice", list("ABCD") * 5)

    vq.check_answer_balance({"第一章": data}, data, warnings)

    assert not any("单选答案" in w or "多选答案" in w for w in warnings)


def test_multiple_combo_concentration_warns():
    warnings = []
    data = answer_pairs("multiple", ["ABC"] * 12)

    vq.check_answer_balance({}, data, warnings)

    assert any("多选答案组合集中" in w for w in warnings)


def test_main_prints_answer_distribution(tmp_path, capsys):
    qdir = tmp_path / "questions"
    qdir.mkdir()
    items = [
        option_question(f"单选题{i}", "测试书-第一章", "choice", answer)
        for i, answer in enumerate("ABCD")
    ]
    items += [
        option_question(f"多选题{i}", "测试书-第一章", "multiple", "AB")
        for i in range(2)
    ]
    chapter = qdir / "第一章.json"
    chapter.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")

    rc = vq.main(
        ["--id", "999", "--book", "测试书", "--questions-dir", str(qdir)]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "单选答案：" in out
    assert "多选组合：" in out
    saved = json.loads(chapter.read_text(encoding="utf-8"))
    assert [q["answer"] for q in saved[:4]] == list("ABCD")


def test_expected_difficulty_ratios(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "counts": {"判断题": {"简单": 5, "中等": 3, "困难": 2}},
                "rounds": 2,
            }
        ),
        encoding="utf-8",
    )
    ratios = vq.expected_diff_ratios(cfg)
    assert ratios == {"简单": 0.5, "中等": 0.3, "困难": 0.2}


def test_difficulty_distribution_flags_single_band():
    warnings, errors = [], []
    values = [0.3] * 20
    snapshot = list(values)

    vq.check_difficulty_distribution({"第一章": values}, values, None, errors, warnings, False)

    assert any("分布单一" in w for w in warnings)
    assert not errors
    assert values == snapshot


def test_difficulty_target_deviation_strict():
    warnings, errors = [], []
    vq.check_difficulty_distribution(
        {"第一章": [0.3] * 20},
        [0.3] * 100,
        {"简单": 0.5, "中等": 0.3, "困难": 0.2},
        errors,
        warnings,
        True,
    )
    assert any("全书难度" in e for e in errors)


def test_strict_difficulty_changes_exit_code(tmp_path):
    qdir = tmp_path / "questions"
    qdir.mkdir()
    for name in ("第一章", "第二章"):
        items = [question(f"{name}-第{i}题", f"测试书-{name}") for i in range(50)]
        (qdir / f"{name}.json").write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8"
        )
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "counts": {"判断题": {"简单": 50, "中等": 30, "困难": 20}},
                "rounds": 1,
            }
        ),
        encoding="utf-8",
    )

    rc = vq.main(
        [
            "--id",
            "999",
            "--book",
            "测试书",
            "--questions-dir",
            str(qdir),
            "--config",
            str(cfg),
            "--strict-difficulty",
        ]
    )

    assert rc == 1


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
