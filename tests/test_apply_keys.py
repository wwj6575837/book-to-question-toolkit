import apply_keys


def test_parse_keys_supports_export_and_inline_comment(tmp_path):
    md = tmp_path / "keys.local.md"
    md.write_text(
        "```dotenv\nexport DEEPSEEK_API_KEY=abc  # 说明\nYUQUE_TOKEN='tok'\n```\n\n"
        "## MinerU token\n```text\nmineru-token\n```\n",
        encoding="utf-8",
    )

    values, token = apply_keys.parse_keys(md)

    assert values == {"DEEPSEEK_API_KEY": "abc", "YUQUE_TOKEN": "tok"}
    assert token == "mineru-token"


def test_merge_dotenv_preserves_export_quote_and_comment(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        'export DEEPSEEK_API_KEY="old"  # keep me\nYUQUE_REPO=team/db\n',
        encoding="utf-8",
    )

    apply_keys.merge_dotenv(env, {"DEEPSEEK_API_KEY": "new"})

    text = env.read_text(encoding="utf-8")
    assert 'export DEEPSEEK_API_KEY="new"  # keep me' in text
    assert "YUQUE_REPO=team/db" in text
    assert not list(tmp_path.glob("*.tmp"))


def test_backup_dirs_are_unique(tmp_path):
    first = apply_keys._make_backup_dir(tmp_path)
    second = apply_keys._make_backup_dir(tmp_path)

    assert first != second
    assert first.is_dir() and second.is_dir()
