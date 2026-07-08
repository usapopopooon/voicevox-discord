"""license embed の presentation test。"""

from features.license import build_license_embed, credit_for_speaker


def test_license_embed_contains_terms_and_current_credit():
    """embed に規約 link と現在の credit 候補が含まれること。"""
    current = credit_for_speaker(46, "ずんだもん", engine_name="VOICEVOX")

    embed = build_license_embed(current)
    values = "\n".join(field.value for field in embed.fields)

    assert "https://voicevox.hiroshiba.jp/term/" in values
    assert "https://coeiroink.com/terms" in values
    assert "クレジット候補" in values
    assert "VOICEVOX: ずんだもん" in values
