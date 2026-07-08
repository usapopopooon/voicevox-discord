"""公開 status embed の presentation test。"""

from features.status import StatusSnapshot, build_status_embed


def test_status_embed_hides_internal_details():
    """公開 status が debug 専用の運用詳細を漏らさないこと。"""
    embed = build_status_embed(
        StatusSnapshot(
            connected=False,
            voice_channel_name="未接続",
            read_channel_id=None,
            queue_length=0,
            queue_maxlen=100,
            speaker_count=4,
            configured_engines=("VOICEVOX",),
            healthy_engines=(),
        )
    )

    values = "\n".join(field.value for field in embed.fields)
    footer = embed.footer.text or ""

    assert "DB:" not in values
    assert "trace" not in values
    assert "Bot instance" not in values
    assert "guild_id=" not in footer


def test_status_embed_formats_public_state():
    """status が公開可能な接続状態と engine 状態だけを表示すること。"""
    embed = build_status_embed(
        StatusSnapshot(
            connected=True,
            voice_channel_name="General",
            read_channel_id=123,
            queue_length=2,
            queue_maxlen=100,
            speaker_count=10,
            configured_engines=("VOICEVOX", "COEIROINK"),
            healthy_engines=("VOICEVOX",),
        )
    )

    values = "\n".join(field.value for field in embed.fields)

    assert "状態: 接続中" in values
    assert "VC: General" in values
    assert "<#123>" in values
    assert "VOICEVOX, COEIROINK" in values
