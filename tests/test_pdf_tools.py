import json

import fitz
import pytest

import pdf_tools


def make_pdf(path, pages=5):
    doc = fitz.open()
    for i in range(1, pages + 1):
        page = doc.new_page()
        page.insert_text((72, 72), str(i))
    doc.save(path)
    doc.close()


def write_titles(path, titles, ranges):
    path.write_text(
        "\n".join([*titles, ",".join(ranges)]) + "\n", encoding="utf-8"
    )


def test_duplicate_title_rejected(tmp_path):
    titles_txt = tmp_path / "titles.txt"
    write_titles(titles_txt, ["第1章 甲", "第1章 甲"], ["1-2", "3-4"])
    with pytest.raises(ValueError, match="重复标题"):
        pdf_tools.parse_titles(titles_txt)


def test_split_reopens_and_checks_page_counts(tmp_path):
    pdf = tmp_path / "book.pdf"
    make_pdf(pdf, pages=5)
    titles_txt = tmp_path / "titles.txt"
    write_titles(titles_txt, ["第1章 甲", "第2章 乙"], ["1-2", "3-5"])
    outdir = tmp_path / "out"

    rc = pdf_tools.main(
        [
            "split",
            "--pdf",
            str(pdf),
            "--titles-txt",
            str(titles_txt),
            "--outdir",
            str(outdir),
            "--apply",
        ]
    )

    assert rc == 0
    for name, expected in (("第1章 甲.pdf", 2), ("第2章 乙.pdf", 3)):
        with fitz.open(outdir / name) as doc:
            assert doc.page_count == expected


def test_inspect_accepts_explicit_offset(tmp_path):
    pdf = tmp_path / "book.pdf"
    make_pdf(pdf, pages=5)
    out_json = tmp_path / "book.json"

    rc = pdf_tools.main(
        ["inspect", "--pdf", str(pdf), "--offset", "3", "--out-json", str(out_json)]
    )

    assert rc == 0
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["offset_suggestion"] == 3
    assert data["offset_confidence"] == 1.0
