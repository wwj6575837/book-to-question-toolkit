import json

import check_formulas


def codes(text):
    return {issue.code for issue in check_formulas.check_text(text)}


def test_clean_file_has_no_issues():
    text = "# 标题\n\n行内 $a+b$ 和块级：\n\n$$\n\\frac{a}{b}\n$$\n"
    assert check_formulas.check_text(text) == []


def test_odd_block_dollar_is_error():
    issues = check_formulas.check_text("$$\na+b\n")
    assert any(x.level == "ERROR" and x.code == "block-pair" for x in issues)


def test_inline_dollar_truncated_is_warning():
    issues = check_formulas.check_text("公式 $x+1 后面没有闭合。\n")
    assert any(x.level == "WARN" and x.code == "inline-pair" for x in issues)


def test_paren_pair_mismatch_is_error():
    issues = check_formulas.check_text("\\( a+b\n")
    assert any(x.level == "ERROR" and x.code == "paren-pair" for x in issues)


def test_left_right_and_environment_errors():
    issues = check_formulas.check_text("$$\n\\left( a + b\n$$\n")
    assert any(x.code == "left-right" for x in issues)

    issues = check_formulas.check_text("$$\n\\begin{aligned} a \n\\end{matrix}\n$$\n")
    assert any(x.code == "environment" for x in issues)


def test_unbalanced_braces_is_error():
    issues = check_formulas.check_text("$$\n\\frac{a}{b\n$$\n")
    assert any(x.code == "brace" for x in issues)


def test_tag_is_warning():
    issues = check_formulas.check_text("$$\na=b \\tag{1}\n$$\n")
    assert any(x.level == "WARN" and x.code == "tag" for x in issues)


def test_fence_content_is_ignored():
    text = "```\n$$\n$x\n```\n\n正文 $ok$。\n"
    assert check_formulas.check_text(text) == []


def test_unclosed_fence_is_error():
    issues = check_formulas.check_text("```\ncode\n")
    assert any(x.code == "fence" for x in issues)


def test_escaped_dollar_is_ignored():
    assert check_formulas.check_text("价格 \\$5，公式 $x$。\n") == []


def test_cross_line_block_does_not_trigger_inline_warning():
    text = "$$\n\\frac{a}{b}\n$$\n"
    assert not any(x.code == "inline-pair" for x in check_formulas.check_text(text))


def test_json_output(tmp_path, capsys):
    md = tmp_path / "a.md"
    md.write_text("$$\nx\n", encoding="utf-8")
    rc = check_formulas.main(["--file", str(md), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["errors"] == 1
    assert payload["issues"][0]["code"] == "block-pair"
