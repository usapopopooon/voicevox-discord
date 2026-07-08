"""migration runner の file discovery rule を検証する test。"""

from migrations import runner


def test_migration_files_only_returns_dated_scripts(tmp_path):
    """helper module や notes を migration として実行しないこと。"""
    for name in [
        "__init__.py",
        "runner.py",
        "notes.txt",
        "20260420_active_voice_sessions.py",
        "seed_without_date.py",
    ]:
        (tmp_path / name).write_text("", encoding="utf-8")

    assert [path.name for path in runner._migration_files(tmp_path)] == [
        "20260420_active_voice_sessions.py"
    ]
