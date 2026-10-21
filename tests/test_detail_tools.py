import json

import clean_detail
import mdscan
import supplement_detail


def write_book_json(path, titles):
    path.write_text(
        json.dumps({"chapters": [{"title": t} for t in titles]}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_headings_skip_code_fence():
    lines = ["# 标题", "```", "# 代码注释", "```", "## 二级"]
    found = [(level, text) for _, level, text in mdscan.iter_headings(lines)]
    assert (1, "标题") in found
    assert (2, "二级") in found
    assert all("代码注释" not in text for _, text in found)


def test_clean_detail_refuses_unmatched_by_default(tmp_path):
    detail = tmp_path / "detail.md"
    original = "## 第1章 甲\n正文\n"
    detail.write_text(original, encoding="utf-8")

    rc = clean_detail.main(["--file", str(detail), "--apply"])

    assert rc == 3
    assert detail.read_text(encoding="utf-8") == original


def test_clean_detail_keeps_one_duplicate_chapter(tmp_path):
    detail = tmp_path / "detail.md"
    detail.write_text(
        "## 第1章 甲\n短\n## 第1章 甲\n更长的正文内容\n", encoding="utf-8"
    )
    book = tmp_path / "book.json"
    write_book_json(book, ["第1章 甲"])

    rc = clean_detail.main(["--file", str(detail), "--book-json", str(book), "--apply"])

    assert rc == 0
    text = detail.read_text(encoding="utf-8-sig")
    assert text.count("## 第1章 甲") == 1
    assert "更长的正文内容" in text


def test_supplement_sorts_sections_by_number(tmp_path):
    detail = tmp_path / "detail.md"
    detail.write_text(
        "## 第1章 测试\n## 1.3 丙\nC\n## 1.1 甲\nA\n", encoding="utf-8"
    )
    org = tmp_path / "organized"
    org.mkdir()
    (org / "第1章 测试.doubao_from_source.md").write_text(
        "## 1.1 甲\nA\n## 1.2 乙\nB\n## 1.3 丙\nC\n", encoding="utf-8"
    )
    book = tmp_path / "book.json"
    write_book_json(book, ["第1章 测试"])

    rc = supplement_detail.main(
        [
            "--file",
            str(detail),
            "--organized-dir",
            str(org),
            "--book-json",
            str(book),
            "--apply",
        ]
    )

    assert rc == 0
    text = detail.read_text(encoding="utf-8-sig")
    assert text.index("1.1 甲") < text.index("1.2 乙") < text.index("1.3 丙")
