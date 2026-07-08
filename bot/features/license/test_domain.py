"""license credit 生成の domain test。"""

from features.license import (
    credit_for_speaker,
    normalize_speaker_display_name,
)


def test_credit_for_speaker_removes_engine_prefix():
    """内部 engine prefix が credit の話者名へ漏れないこと。"""
    current = credit_for_speaker(
        10000,
        "[COEIROINK] つくよみちゃん（れいせい）",
    )

    assert current.speaker_name == "つくよみちゃん（れいせい）"
    assert current.credit.startswith("COEIROINK: つくよみちゃん")
    assert "[COEIROINK]" not in current.credit


def test_credit_for_speaker_prefers_resolved_engine_name():
    """runtime engine metadata が表示名 prefix より優先されること。"""
    current = credit_for_speaker(
        10000,
        "[COEIROINK] つくよみちゃん",
        engine_name="VOICEVOX",
    )

    assert current.credit == "VOICEVOX: つくよみちゃん"


def test_normalize_speaker_display_name_keeps_plain_name():
    """plain な話者名はそのまま維持されること。"""
    assert normalize_speaker_display_name("ずんだもん") == "ずんだもん"
